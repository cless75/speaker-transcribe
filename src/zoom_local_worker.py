#!/usr/bin/env python3
"""Local Zoom MP4+VTT ASR; publish only verified text and quality to Drive.

zoomrs owns the immutable source repository. This worker never writes there,
never uploads media, and shares hub_worker's node-local publishing lock.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

import audio_inbox_watch as watcher
from hub_intake import Drive
from hub_worker import atomic_json, remote_verified, single_worker
from zoom_dispatch import _local_file, _meeting_start, _meetings, _filename, project_id


def fingerprint(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def select(config: dict) -> list[dict]:
    """Only complete, unambiguous and stable local pairs; never the Hub VTT route."""
    repository = pathlib.Path(config["repository"]).expanduser()
    settle = int(config.get("min_stable_age_seconds", 120))
    if settle < 0:
        raise ValueError("negative settle period")
    dispatch_config = pathlib.Path(config["dispatch_config"]).expanduser()
    dispatch = json.loads(dispatch_config.read_text(encoding="utf-8"))
    delivered = pathlib.Path(dispatch["state_file"]).expanduser()
    dispatched = json.loads(delivered.read_text(encoding="utf-8")) if delivered.is_file() else {}
    approved = set(dispatch.get("approved_meetings", []))
    items = []
    for meeting in _meetings(pathlib.Path(config["database"]).expanduser(), include_video=True):
        if meeting["uuid"] in approved or meeting["uuid"] in dispatched:
            continue  # the VTT-only route owns this meeting; no duplicate ASR
        video = []
        for kind in ("shared_screen_with_speaker_view", "shared_screen_with_gallery_view",
                     "active_speaker"):
            video = [r for r in meeting["records"] if r["type"] == kind]
            if video:
                break
        vtts = [r for r in meeting["records"] if r["type"] == "audio_transcript"]
        if len(video) != 1 or len(vtts) != 1:
            continue  # do not choose one of multiple video/transcript revisions
        sources = [_local_file(record, repository, settle, config.get("container_repository"))
                   for record in (video[0], vtts[0])]
        if any(source is None for source in sources):
            continue
        if sources[0].suffix.lower() != ".mp4" or sources[1].suffix.lower() != ".vtt":
            continue
        pid = project_id(meeting["topic"] or "", config)
        if pid.startswith("_"):
            continue
        stem = _filename(meeting, vtts[0])
        week = f"w{_meeting_start(meeting).isocalendar().week}"
        items.append({"uuid": meeting["uuid"], "pid": pid, "stem": stem,
                      "week": week, "video": sources[0], "vtt": sources[1],
                      "start": _meeting_start(meeting)})
    return items


def check_dispatch(config: dict, item: dict) -> None:
    dispatch = json.loads(pathlib.Path(config["dispatch_config"]).expanduser().read_text(encoding="utf-8"))
    delivered = pathlib.Path(dispatch["state_file"]).expanduser()
    saved = json.loads(delivered.read_text(encoding="utf-8")) if delivered.is_file() else {}
    if item["uuid"] in saved or item["uuid"] in dispatch.get("approved_meetings", []):
        raise RuntimeError("meeting belongs to VTT-only dispatch; no local ASR")


def check_hub(drive: Drive, item: dict) -> None:
    """Do not create an ASR session for the exact VTT dispatched to Hub."""
    rel = f"_{item['pid']}_inbox/{item['week']}/{item['stem']}.transcript.vtt"
    entries = drive.list(item["pid"], recursive=True)
    if any(entry.get("Path") in {rel, rel + ".state.json"}
           for entry in entries):
        raise RuntimeError("matching VTT already in Hub; manual duplicate review")


def require_dispatch_inactive(config: dict) -> None:
    timer = config.get("dispatch_timer", "zoom-dispatch@clessd.timer")
    service = timer.removesuffix(".timer") + ".service"
    for unit in (timer, service):
        state = subprocess.run(["systemctl", "show", "--value", "-p", "ActiveState", unit],
                               capture_output=True, text=True, check=False)
        if state.returncode or state.stdout.strip() not in {"inactive", "failed"}:
            raise RuntimeError("Zoom VTT-only dispatcher is active or cannot be checked")


def process_one(config: dict, item: dict, drive: Drive, *, transcribe=None) -> dict:
    root = pathlib.Path(config["local_root"]).expanduser().resolve()
    repository = pathlib.Path(config["repository"]).expanduser().resolve()
    hub = pathlib.Path(config["hub_root"]).expanduser().resolve()
    if (root.is_relative_to(repository) or repository.is_relative_to(root)
            or root.is_relative_to(hub)):
        raise ValueError("local output overlaps zoomrs repository or Hub mount")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(item["uuid"].encode()).hexdigest()[:16]
    job_dir = root / "jobs" / key
    journal = job_dir / "job.json"
    job = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else {}
    if job and (job.get("uuid_digest"), job.get("pid")) != (key, item["pid"]):
        raise ValueError("local Zoom journal does not match selected meeting")
    if job.get("phase") == "done":
        return job
    relative = f"{item['pid']}/sessions/{item['start']:%Y-%m}/S{item['start']:%Y%m%dT%H%M}-zoom-{key}"
    if job and job.get("output") != relative:
        raise ValueError("meeting route changed; explicit reconciliation required")
    if not job:
        check_dispatch(config, item)
        check_hub(drive, item)
        job = {"uuid_digest": key, "pid": item["pid"], "output": relative,
               "phase": "copying", "source_fingerprints": {
                   "video": fingerprint(item["video"]), "vtt": fingerprint(item["vtt"])}}
        atomic_json(journal, job)
    video = job_dir / "recording.mp4"
    vtt = job_dir / "recording.transcript.vtt"
    if job["phase"] == "copying":
        for source, target, name in ((item["video"], video, "video"),
                                     (item["vtt"], vtt, "vtt")):
            if not target.is_file() or fingerprint(target) != job["source_fingerprints"][name]:
                temp = target.with_name(target.name + ".tmp")
                try:
                    shutil.copyfile(source, temp)
                    if fingerprint(temp) != job["source_fingerprints"][name]:
                        raise RuntimeError("Zoom source changed during local copy")
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        job["phase"] = "staged"
        atomic_json(journal, job)
    if any(fingerprint(path) != job["source_fingerprints"][name]
           for path, name in ((video, "video"), (vtt, "vtt"))):
        raise RuntimeError("local Zoom bundle changed; refusing to publish")
    transcripts = root / "outputs" / relative / "transcripts"
    if job["phase"] == "staged":
        transcripts.mkdir(parents=True, exist_ok=True)
        if transcribe is None:
            cfg = json.loads(pathlib.Path(config["node_config"]).read_text(encoding="utf-8"))
            cfg["voiceprint_mode"] = "off"
            log = job_dir / "asr-worker.log"
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as output:
                with contextlib.redirect_stderr(output):
                    ok, _reason, transcript, meta = watcher.run_asr(
                        video, cfg, item["pid"], job_dir, transcripts,
                        pathlib.Path(config["node_config"]))
            if not ok:
                raise RuntimeError("local Zoom ASR did not finish; see private ASR log")
        else:
            transcript, meta = transcribe(video, vtt, transcripts)
        if (not transcript or not transcript.is_file() or not transcript.stat().st_size
                or not isinstance(meta, dict) or meta.get("status") not in {"ok", "degraded"}
                or (meta.get("zoom_vtt") or {}).get("status") != "ok"):
            raise RuntimeError("Zoom ASR has no complete media transcript with VTT speakers")
        if meta["status"] == "degraded" and (meta.get("quality") or {}).get("status") != "degraded":
            raise RuntimeError("degraded ASR missing quality details")
        job.update({"phase": "processed", "transcript": transcript.name,
                    "quality": {"status": meta["status"],
                                "gaps_sec": (meta.get("quality") or {}).get("gaps_sec"),
                                "thresholds": (meta.get("quality") or {}).get("thresholds")},
                    "speakers_detected": meta["zoom_vtt"].get("speakers_detected")})
        atomic_json(journal, job)
    publication = root / "publication" / relative
    if job["phase"] == "processed":
        publication.mkdir(parents=True, exist_ok=True)
        source = transcripts / job["transcript"]
        if not source.is_file() or not source.stat().st_size:
            raise RuntimeError("local transcript missing")
        text = source.read_text(encoding="utf-8")
        text = text.replace(str(video), "local Zoom recording (not uploaded)")
        text = text.replace(str(vtt), "local Zoom VTT (not uploaded)")
        (publication / "transcript.md").write_text(text, encoding="utf-8")
        atomic_json(publication / "quality.json", {"quality": job["quality"],
                    "speaker_source": "zoom_vtt", "speakers_detected": job["speakers_detected"],
                    "source_kind": "local_zoom_media"})
        job["phase"] = "publishing"
        atomic_json(journal, job)
    require_dispatch_inactive(config)
    check_dispatch(config, item)
    check_hub(drive, item)
    if any(fingerprint(path) != job["source_fingerprints"][name]
           for path, name in ((item["video"], "video"), (item["vtt"], "vtt"))):
        raise RuntimeError("zoomrs source changed after staging")
    drive.call("copy", str(publication), drive.path(relative))
    published = remote_verified(drive, relative, publication)
    transcript_info = published.get("transcript.md") or {}
    if not transcript_info.get("ID"):
        raise RuntimeError("verified transcript has no Drive ID")
    state = {"status": "asr-done", "publication_verified": True,
             "source_kind": "local_zoom_media", "uuid_digest": key,
             "transcript_quality": job["quality"],
             "speaker_source": "zoom_vtt", "speakers_detected": job["speakers_detected"],
             "transcript_url": "https://drive.google.com/file/d/" + transcript_info["ID"] + "/view",
             "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    atomic_json(job_dir / "published-state.json", state)
    drive.call("copyto", str(job_dir / "published-state.json"), drive.path(relative, "pipeline", "state.json"))
    confirmed = json.loads(drive.call("cat", drive.path(relative, "pipeline", "state.json")))
    if confirmed != state:
        raise RuntimeError("published pipeline state differs from local receipt")
    atomic_json(job_dir / "state.json", state)  # last: only after verified publication
    job["phase"] = "done"
    atomic_json(journal, job)
    return job


def run(config: dict, *, dry_run: bool = True, transcribe=None, drive=None) -> dict:
    drive = drive or Drive(config["drive_remote"], config.get("rclone_config"))
    report = {"eligible": 0, "processed": 0, "held": 0}
    items = select(config)
    if dry_run:
        report["eligible"] = len(items)
        return report
    require_dispatch_inactive(config)
    with single_worker(pathlib.Path(config["hub_worker_root"])):
        for item in items:
            journal = pathlib.Path(config["local_root"]) / "jobs" / hashlib.sha256(item["uuid"].encode()).hexdigest()[:16] / "job.json"
            if journal.is_file() and json.loads(journal.read_text()).get("phase") == "done":
                continue
            if report["eligible"] >= min(int(config.get("max_meetings_per_run", 3)), 3):
                break
            report["eligible"] += 1
            try:
                result = process_one(config, item, drive, transcribe=transcribe)
                report["processed"] += result["phase"] == "done"
            except (OSError, ValueError, RuntimeError) as exc:
                report["held"] += 1
                print(f"HOLD local Zoom job {hashlib.sha256(item['uuid'].encode()).hexdigest()[:8]}: {type(exc).__name__}", file=sys.stderr)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("--run", action="store_true", help="otherwise inspect eligible local pairs only")
    args = parser.parse_args()
    report = run(json.loads(args.config.read_text(encoding="utf-8")), dry_run=not args.run)
    print(json.dumps(report))
    return 1 if report["held"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
