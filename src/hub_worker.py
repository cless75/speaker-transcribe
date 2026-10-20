#!/usr/bin/env python3
"""Explicit Drive → local worker → verified Drive output canary.

Never run alongside the legacy watcher for real project folders. A read-only
whole-Hub queue inspector and bundle delivery are included, but unattended
whole-Hub execution is disabled until peer exclusion and cutover are proven.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable

import audio_inbox_watch as watcher
from hub_intake import (Drive, MediaFile, MEDIA, media_inventory, partition_entries,
                        projects, publish_page, safe_parts, scannable, snapshot, stage)
from zoom_vtt_import import import_zoom_vtt

ZOOM_TIMESTAMP = re.compile(r"(?:GMT)?(\d{8}[-_]\d{6})")
ZOOM_WEEK = re.compile(r"w(?:[1-9]|[1-4]\d|5[0-3])")


def atomic_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".job-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, indent=2)
        os.replace(name, path)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def file_md5(path: pathlib.Path) -> str:
    checksum = hashlib.md5()  # nosec: Drive transfer checksum, not a security primitive
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def code_digest() -> str:
    return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


def worker_config(cfg: dict) -> dict:
    """Distinct claimant on the same node: legacy must yield, not reclaim 'self'."""
    result = copy.deepcopy(cfg)
    node = dict(result.get("node") or {})
    node["host_label"] = watcher.host_label_of(cfg) + "-DRIVE"
    result["node"] = node
    return result


@contextlib.contextmanager
def single_worker(root: pathlib.Path):
    """No two local canaries can publish the same result concurrently (Linux)."""
    if os.name != "posix":
        raise RuntimeError("worker requires a POSIX host with flock")
    import fcntl

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    with (root / ".worker.lock").open("w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another Hub worker is active on this node") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def legacy_inactive() -> None:
    """No project prefix bypasses this gate: the old watcher scans _test* too."""
    if os.name != "posix":
        raise RuntimeError("cannot prove the old watcher is off on this host")
    for unit in ("speaker-transcribe-watch.timer", "speaker-transcribe-watch.service"):
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
        if result.returncode == 0:
            raise RuntimeError(f"legacy consumer still active: {unit}")


def isolated_vtt_canary(item: MediaFile, legacy_config: pathlib.Path) -> None:
    """Only bypass the timer gate if its REAL config cannot discover this VTT."""
    if not item.project.startswith("_test") or item.project == "_test" or item.path[-4:].lower() != ".vtt":
        raise ValueError("isolated canary requires VTT in a reserved _test* project")
    unit = subprocess.run(["systemctl", "cat", "speaker-transcribe-watch.service"],
                          capture_output=True, text=True, check=False)
    if unit.returncode or ("--config " + str(legacy_config.resolve())) not in unit.stdout:
        raise RuntimeError("cannot prove which config the legacy watcher uses")
    old = json.loads(legacy_config.read_text(encoding="utf-8"))
    extensions = {str(ext).lower() for ext in old.get("scan_extensions", [])}
    if old.get("zoom_vtt_only_input") or ".vtt" in extensions:
        raise RuntimeError("legacy watcher can discover VTT; stop it before this E2E")
    if not any(source.get("discover") == "project-inboxes" and
                source.get("root") == "{hub_root}" for source in old.get("sources", [])):
        raise RuntimeError("cannot prove legacy watcher source coverage from config")


def zoom_dispatch_vtt(project: str, path: str) -> bool:
    """Only the canonical weekly project inbox is a VTT-only automatic input."""
    parts = safe_parts(path)
    return (len(parts) == 3 and parts[0] == f"_{project}_inbox" and
            ZOOM_WEEK.fullmatch(parts[1]) is not None and
            pathlib.PurePosixPath(parts[2]).suffix.lower() == ".vtt")


def require_vtt_route(item: MediaFile, cfg: dict, *, manual: bool = False,
                      isolated: bool = False) -> None:
    if pathlib.PurePosixPath(item.path).suffix.lower() != ".vtt":
        return
    if not cfg.get("zoom_vtt_only_input"):
        raise ValueError("VTT-only input is disabled")
    if not (zoom_dispatch_vtt(item.project, item.path) or manual or isolated):
        raise ValueError("standalone VTT outside weekly Zoom inbox needs --manual-vtt")


def choose_input(drive: Drive, project: str, rel: str, *, allow_vtt: bool) -> MediaFile:
    """Refuse ambiguous ID/path and multi-track recordings, not just duplicates."""
    safe_parts(project)
    safe_parts(rel)
    entries = drive.list(project, recursive=True)
    clean, duplicated = partition_entries(project, entries)
    if rel in duplicated or rel + ".state.json" in duplicated:
        raise ValueError("input or its marker has ambiguous Drive IDs; quarantine")
    matches = [entry for entry in entries if entry.get("Path") == rel and not entry.get("IsDir")]
    if len(matches) != 1:
        raise ValueError(f"input must resolve to exactly one Drive ID: {project}/{rel}")
    item = matches[0]
    suffix = pathlib.PurePosixPath(rel).suffix.lower()
    if suffix == ".vtt" and not allow_vtt:
        raise ValueError("Zoom VTT-only input is disabled in node config")
    if suffix not in MEDIA | {".vtt"}:
        raise ValueError(f"unsupported input: {rel}")
    token = ZOOM_TIMESTAMP.search(pathlib.PurePosixPath(rel).name)
    parent = pathlib.PurePosixPath(rel).parent
    if token:
        siblings = [entry for entry in entries if entry.get("Path") != rel and
                    pathlib.PurePosixPath(entry.get("Path", "")).parent == parent and
                    pathlib.PurePosixPath(entry.get("Path", "")).suffix.lower() in MEDIA and
                    (match := ZOOM_TIMESTAMP.search(entry.get("Name", ""))) and
                    match.group(1) == token.group(1)]
        if siblings:
            raise ValueError("meeting bundle requires grouped delivery; not safe as a single file")
    if suffix == ".vtt":
        hashes = item.get("Hashes") or {}
        if not item.get("ID"):
            raise ValueError("VTT has no Drive ID")
        return MediaFile(project, rel, item["ID"], int(item["Size"]),
                         item.get("ModTime", ""), hashes.get("md5"),
                         rel + ".state.json" in {e.get("Path") for e in entries})
    # media_inventory also validates scannability and checksum metadata.
    return next(x for x in media_inventory(project, clean) if x.path == rel)


def choose_group(drive: Drive, project: str, rel: str, *, allow_vtt: bool
                 ) -> tuple[MediaFile, list[MediaFile]]:
    """One ASR primary for a timestamped meeting, including video/VTT/chat peers."""
    entries = drive.list(project, recursive=True)
    clean, duplicates = partition_entries(project, entries)
    if rel in duplicates or rel + ".state.json" in duplicates:
        raise ValueError("meeting input or marker has duplicate Drive IDs; quarantine")
    by_path = {entry.get("Path"): entry for entry in clean if not entry.get("IsDir")}
    selected = by_path.get(rel)
    if not selected:
        raise ValueError("file not found unambiguously in project")
    extension = pathlib.PurePosixPath(rel).suffix.lower()
    token = ZOOM_TIMESTAMP.search(pathlib.PurePosixPath(rel).name)
    if extension == ".vtt" or token is None:
        return choose_input(drive, project, rel, allow_vtt=allow_vtt), []
    parent = pathlib.PurePosixPath(rel).parent
    for path in duplicates:
        candidate = pathlib.PurePosixPath(path)
        stamp = ZOOM_TIMESTAMP.search(candidate.name)
        if (candidate.parent == parent and stamp and stamp.group(1) == token.group(1)
                and (candidate.suffix.lower() in MEDIA | {".vtt", ".txt"} or
                     path.endswith(".state.json"))):
            raise ValueError("meeting peer has duplicate Drive IDs; quarantine entire bundle")
    collisions = Counter(entry.get("Path") for entry in entries if not entry.get("IsDir"))
    if any(count > 1 and pathlib.PurePosixPath(path).parent == parent and
           pathlib.PurePosixPath(path).suffix.lower() == ".txt" and
           (stamp := ZOOM_TIMESTAMP.search(path)) and stamp.group(1) == token.group(1)
           for path, count in collisions.items()):
        raise ValueError("meeting chat has duplicate Drive IDs; quarantine entire bundle")
    members = [item for item in media_inventory(project, clean)
               if pathlib.PurePosixPath(item.path).parent == parent and
               (stamp := ZOOM_TIMESTAMP.search(pathlib.PurePosixPath(item.path).name)) and
               stamp.group(1) == token.group(1)]
    if not members or rel not in {item.path for item in members}:
        raise ValueError("media is not a scannable meeting input")
    if len(members) > 1 and any(pathlib.PurePosixPath(item.path).suffix.lower() not in
                                watcher.AUDIO_EXTS | watcher.VIDEO_EXTS for item in members):
        raise ValueError("bundle includes a media type not classified by the legacy watcher")
    if len(members) == 1:
        primary = members[0]
    else:
        audio = sorted((item for item in members if pathlib.PurePosixPath(item.path).suffix.lower()
                        in watcher.AUDIO_EXTS), key=lambda item: item.path)
        videos = sorted((item for item in members if pathlib.PurePosixPath(item.path).suffix.lower()
                         in watcher.VIDEO_EXTS), key=lambda item: item.path)
        primary = next((item for item in audio if watcher.PRIMARY_NAME_PATTERN.match(
            pathlib.PurePosixPath(item.path).name)), None)
        if primary is None and videos:
            eligible = [item for item in audio if not pathlib.PurePosixPath(item.path).name.lower(
                ).startswith("audio_only_")]
            if eligible:
                primary = max(eligible, key=lambda item: item.size)
        if primary is None and len(videos) >= 2:
            primary = next((item for item in videos if "_avo_" in item.path.lower()), None)
            if primary is None:
                primary = min(videos, key=lambda item: item.size)
        if primary is None:
            raise ValueError("meeting has multiple media but no safe primary; quarantine")
    companions = [item for item in members if item != primary]
    for path, item in by_path.items():
        if pathlib.PurePosixPath(path).parent != parent:
            continue
        suffix = pathlib.PurePosixPath(path).suffix.lower()
        if suffix not in {".vtt", ".txt"}:
            continue
        stamp = ZOOM_TIMESTAMP.search(pathlib.PurePosixPath(path).name)
        if not stamp or stamp.group(1) != token.group(1):
            continue
        if path in duplicates:
            raise ValueError("meeting sidecar has duplicate Drive IDs; quarantine")
        hash_value = (item.get("Hashes") or {}).get("md5")
        companions.append(MediaFile(project, path, item["ID"], int(item["Size"]),
                                    item.get("ModTime", ""), hash_value, False))
    return primary, companions


def pending_inputs(drive: Drive, project: str, cfg: dict, cache: pathlib.Path,
                   worker_root: pathlib.Path | None = None
                   ) -> tuple[list[tuple[MediaFile, list[MediaFile]]], list[str]]:
    """Read a project's marker state; duplicate paths and unreadable states quarantine."""
    entries = drive.list(project, recursive=True)
    clean, duplicated = partition_entries(project, entries)
    quarantine = [f"duplicate Drive path: {path}" for path in sorted(duplicated)]
    inputs = media_inventory(project, clean)
    if cfg.get("zoom_vtt_only_input"):
        media_paths = [pathlib.PurePosixPath(item.path) for item in inputs]
        markers = {entry.get("Path") for entry in clean if not entry.get("IsDir")}
        for entry in clean:
            path = entry.get("Path", "")
            vtt = pathlib.PurePosixPath(path)
            if (entry.get("IsDir") or vtt.suffix.lower() != ".vtt" or
                    vtt.name.endswith("-segments.vtt") or
                    not zoom_dispatch_vtt(project, path) or not scannable(project, path)):
                continue
            stamp = ZOOM_TIMESTAMP.search(vtt.name)
            if any(peer.parent == vtt.parent and (peer.stem == vtt.stem or
                   stamp and (match := ZOOM_TIMESTAMP.search(peer.name)) and
                   match.group(1) == stamp.group(1)) for peer in media_paths):
                continue  # a media sidecar is not a second job
            hashes = entry.get("Hashes") or {}
            inputs.append(MediaFile(project, path, entry["ID"], int(entry["Size"]),
                                    entry.get("ModTime", ""), hashes.get("md5"),
                                    path + ".state.json" in markers))
    if any(item.marker for item in inputs):
        drive.states_bulk(project, cache)
    markers_by_path = {entry["Path"]: entry for entry in clean
                       if entry.get("Path", "").endswith(".state.json")}
    work: list[tuple[MediaFile, list[MediaFile]]] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(inputs, key=lambda x: x.modified, reverse=True):
        if worker_root is not None:
            journal = (worker_root / "intake" / project / item.file_id /
                       item.revision / "job.json")
            if journal.is_file():
                try:
                    local_state = json.loads(journal.read_text(encoding="utf-8"))
                    due_at = local_state.get("retry_not_before")
                    if due_at and dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(due_at):
                        continue
                except (OSError, ValueError, TypeError):
                    quarantine.append(f"unreadable local journal: {item.path}")
                    continue
        if item.marker:
            marker = markers_by_path[item.path + ".state.json"]
            cached = cache.joinpath(*safe_parts(item.path + ".state.json"))
            expected = (marker.get("Hashes") or {}).get("md5", "")
            try:
                raw = cached.read_bytes()
                if not expected or hashlib.md5(raw).hexdigest() != expected.lower():
                    raise ValueError("marker checksum mismatch")
                state = json.loads(raw)
                if not isinstance(state, dict):
                    raise ValueError("marker is not a JSON object")
                status = state.get("status")
                if state.get("source_revision") and state["source_revision"] != item.revision:
                    quarantine.append(f"source revision differs from marker: {item.path}")
                    continue
                if status == "queued" and state.get("retry_not_before"):
                    due = dt.datetime.fromisoformat(state["retry_not_before"])
                    now = dt.datetime.now(due.tzinfo)
                    if now < due:
                        continue
            except (OSError, ValueError, TypeError) as exc:
                quarantine.append(f"unreadable marker: {item.path} ({type(exc).__name__})")
                continue
            if status in {"copying", "in-progress", "awaiting-publication"}:
                journal = (worker_root / "intake" / project / item.file_id /
                           item.revision / "job.json") if worker_root else None
                if (state.get("source_revision") != item.revision or
                        state.get("host") != watcher.host_label_of(worker_config(cfg)) or
                        journal is None or not journal.is_file()):
                    quarantine.append(f"explicit resume required for {status}: {item.path}")
                    continue
            elif status != "queued":
                continue
        try:
            primary, companions = choose_group(
                drive, project, item.path, allow_vtt=bool(cfg.get("zoom_vtt_only_input")))
        except (ValueError, StopIteration) as exc:
            quarantine.append(f"cannot route {item.path}: {exc}")
            continue
        if primary != item and primary.marker:
            current = drive.state(primary.project, primary.path)
            if current.get("status") != "queued":
                quarantine.append(f"bundle sibling without queued primary: {item.path}")
                continue
        key = (primary.file_id, primary.revision)
        if key not in seen:
            seen.add(key)
            work.append((primary, companions))
    return work, quarantine


def defer_failed_job(root: pathlib.Path, item: MediaFile, exc: Exception) -> None:
    """A repeatedly failing source must not starve every other Hub project."""
    path = root / "intake" / item.project / item.file_id / item.revision / "job.json"
    if not path.is_file():
        return
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("phase") in {"done", "pruned"}:
        return
    count = min(int(job.get("failure_count", 0)) + 1, 30)
    delay_min = min(60, 2 ** min(count - 1, 6))
    job["failure_count"] = count
    job["retry_not_before"] = (dt.datetime.now(dt.timezone.utc) +
                               dt.timedelta(minutes=delay_min)).isoformat()
    job["last_error"] = f"{type(exc).__name__}: {exc}"[-300:]
    atomic_json(path, job)


def log_private_failure(root: pathlib.Path, source_id: str, exc: Exception) -> None:
    """No recording title or worker output in the systemd journal."""
    fd = os.open(root / "queue-errors.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} "
                  f"id={source_id} {type(exc).__name__}: {str(exc)[-300:]}\n")


def place_companion(drive: Drive, item: MediaFile, root: pathlib.Path,
                    target_dir: pathlib.Path) -> pathlib.Path:
    downloaded = stage(drive, item, root / "intake")
    target = target_dir / downloaded.name
    if target == downloaded:
        return target
    if target.exists():
        md5 = file_md5(target)
        if target.stat().st_size != item.size or md5 != item.md5:
            raise ValueError(f"local sibling differs from Drive: {item.path}")
        return target
    try:
        os.link(downloaded, target)
    except OSError:
        shutil.copyfile(downloaded, target)
    digest = file_md5(target)
    if target.stat().st_size != item.size or digest != item.md5:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"local companion copy failed verification: {item.path}")
    return target


def remote_verified(drive: Drive, target: str, local_dir: pathlib.Path) -> dict[str, dict]:
    """Every local output must be present in Drive with the same size and MD5."""
    remote = drive.list(*safe_parts(target), recursive=True)
    counts = Counter(entry.get("Path") for entry in remote if not entry.get("IsDir"))
    by_name = {entry.get("Path"): entry for entry in remote if not entry.get("IsDir")}
    files = [path for path in local_dir.rglob("*") if path.is_file()]
    if not files:
        raise RuntimeError("no local output to verify")
    for path in files:
        rel = path.relative_to(local_dir).as_posix()
        if counts[rel] > 1:
            raise RuntimeError(f"published output has duplicate Drive IDs: {rel}")
        found = by_name.get(rel)
        if not found or int(found.get("Size", -1)) != path.stat().st_size:
            raise RuntimeError(f"missing or truncated published output: {rel}")
        digest = file_md5(path)
        if (found.get("Hashes") or {}).get("md5", "").lower() != digest:
            raise RuntimeError(f"published checksum differs: {rel}")
    return by_name


def rewrite_output_paths(output: pathlib.Path, audio: pathlib.Path,
                         published_output: str, published_audio: str,
                         related: list[tuple[str, str]] | None = None) -> None:
    """Local paths must not leak into Hub metadata or the final transcript."""
    replacements = [(str(output), published_output), (str(audio), published_audio)]
    replacements.extend(related or [])

    def rewrite(value):
        if isinstance(value, str):
            for before, after in replacements:
                value = value.replace(before, after)
            return value
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    for path in output.rglob("*"):
        if path.suffix == ".json" and path.is_file():
            atomic_json(path, rewrite(json.loads(path.read_text(encoding="utf-8"))))
        elif path.suffix in {".md", ".txt", ".vtt", ".srt", ".jsonl"} and path.is_file():
            text = path.read_text(encoding="utf-8")
            rewritten = rewrite(text)
            if rewritten != text:
                temp = path.with_name(path.name + ".tmp")
                try:
                    temp.write_text(rewritten, encoding="utf-8")
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)


def source_unchanged(drive: Drive, item: MediaFile) -> None:
    """A newly uploaded revision cannot inherit an old run's green marker."""
    source = pathlib.PurePosixPath(item.path)
    parent = () if str(source.parent) == "." else source.parent.parts
    candidates = [entry for entry in drive.list(item.project, *parent)
                  if entry.get("Name") == source.name and not entry.get("IsDir")]
    if len(candidates) != 1:
        raise RuntimeError("source changed or became ambiguous during processing")
    current = candidates[0]
    if (current.get("ID"), int(current.get("Size", -1)), current.get("ModTime", ""),
            (current.get("Hashes") or {}).get("md5")) != (
            item.file_id, item.size, item.modified, item.md5):
        raise RuntimeError("source revision changed during processing")


def marker_unique_id(drive: Drive, item: MediaFile) -> str:
    """Drive permits same-name siblings; an ambiguous state is never trusted."""
    marker = pathlib.PurePosixPath(item.path + ".state.json")
    parent = () if str(marker.parent) == "." else marker.parent.parts
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        matches = [entry for entry in drive.list(item.project, *parent)
                   if entry.get("Name") == marker.name and not entry.get("IsDir")]
        if len(matches) > 1:
            raise RuntimeError("source state path has duplicate Drive IDs")
        if len(matches) == 1 and matches[0].get("ID"):
            return matches[0]["ID"]
    raise RuntimeError("source state Drive ID did not become visible")


def _default_transcribe(audio: pathlib.Path, output_dir: pathlib.Path, cfg: dict,
                        config_path: pathlib.Path, pid: str
                        ) -> tuple[pathlib.Path, dict]:
    if audio.suffix.lower() == ".vtt":
        return import_zoom_vtt(audio, output_dir)
    # The worker receives a LOCAL input and writes LOCAL outputs. Voiceprint
    # registries are still resolved from node config. The legacy runner streams
    # the worker's full JSON (including speech) to stderr: keep that on the
    # private node disk rather than emitting it into systemd's public journal.
    fd = os.open(audio.parent / "asr-worker.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", buffering=1) as output:
        with contextlib.redirect_stderr(output):
            ok, reason, transcript, meta = watcher.run_asr(audio, cfg, pid, audio.parent,
                                                            output_dir, config_path)
    if not ok or transcript is None:
        raise RuntimeError(f"ASR did not finish: {reason[-250:]}")
    return transcript, meta or {}


def publish_progress(drive: Drive, item: MediaFile, job_dir: pathlib.Path,
                     job: dict, pid: str, status: str, cfg: dict) -> None:
    """A progress marker is checked before a costly local step begins."""
    if status not in {"copying", "in-progress", "awaiting-publication"}:
        raise ValueError("unsupported nonterminal status")
    progress = {"status": status, "pid": pid, "session_id": job["session_id"],
                "source_id": item.file_id, "source_revision": item.revision,
                "host": watcher.host_label_of(cfg),
                "started_at": job["started_at"],
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "worker_completed": status == "awaiting-publication"}
    local = job_dir / "progress-state.json"
    atomic_json(local, progress)
    remote = drive.path(item.project, item.path + ".state.json")
    drive.call("copyto", str(local), remote)
    confirmed = json.loads(drive.call("cat", remote))
    if (confirmed.get("status"), confirmed.get("source_revision")) != (status, item.revision):
        raise RuntimeError("progress marker was not published")
    marker_id = marker_unique_id(drive, item)
    if job.get("marker_id") and job["marker_id"] != marker_id:
        raise RuntimeError("source state Drive ID changed during processing")
    job["marker_id"] = marker_id
    atomic_json(job_dir / "job.json", job)


def refresh_status(drive: Drive, project: str, root: pathlib.Path) -> None:
    """Refresh only the affected project; a page failure cannot lose ASR output."""
    try:
        page, _report = snapshot(drive, project,
                                 state_cache=root / "status-cache" / project)
        local = root / "status" / project / "index.html"
        local.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        local.write_text(page, encoding="utf-8")
        publish_page(drive, project, local)
    except Exception as exc:  # status page is optional, never abort a verified ASR result
        try:
            fd = os.open(root / "status-errors.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as out:
                out.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {project}: "
                          f"{type(exc).__name__}: {exc}\n")
        except OSError:
            pass


def output_project(item: MediaFile, cfg: dict, config_path: pathlib.Path
                   ) -> tuple[str, bool]:
    """Use the existing watcher's routing, including shared Hub/_inbox."""
    if item.project != "_inbox":
        return item.project, False
    hub = pathlib.Path(cfg["hub_root"])
    root = hub / "_inbox"
    mapper_cfg = {**cfg, "_config_dir": str(config_path.parent)}
    mapper = watcher.load_mapper(mapper_cfg)
    pid, needs_class = watcher.route_pid_for_audio(
        root.joinpath(*safe_parts(item.path)), root, {"route": "mapper"},
        mapper_cfg, mapper)
    if not pid or ("/" in pid or "\\" in pid):
        raise ValueError("shared inbox has no safe project route")
    if not mapper and len(safe_parts(item.path)) > 1:
        raise ValueError("nested shared inbox needs an explicit mapper")
    return pid, needs_class


def process_one(drive: Drive, item: MediaFile, root: pathlib.Path, cfg: dict,
                config_path: pathlib.Path, *,
                transcribe: Callable | None = None,
                companions: list[MediaFile] | None = None,
                claim_check: Callable[[], None] | None = None) -> dict:
    """Resume from local receipt; only publish after all outputs verify in Drive."""
    pid, needs_classification = output_project(item, cfg, config_path)
    if item.marker:
        state = drive.state(item.project, item.path)
        owner = watcher.host_label_of(cfg)
        if state.get("status") in {"copying", "in-progress", "awaiting-publication"}:
            journal = root / "intake" / item.project / item.file_id / item.revision / "job.json"
            if (state.get("host"), state.get("source_revision")) != (owner, item.revision) or not journal.is_file():
                raise ValueError("in-progress marker belongs to another node or has no journal")
        elif state.get("status") != "queued":
            raise ValueError(f"source marker is not safely queued: {state.get('status')}")
    companions = companions or []
    companions_key = [{"id": part.file_id, "revision": part.revision, "path": part.path}
                      for part in companions]
    for part in companions:
        if part.project != item.project or pathlib.PurePosixPath(part.path).parent != pathlib.PurePosixPath(item.path).parent:
            raise ValueError("companion outside meeting directory")
        if part.marker:
            old = drive.state(part.project, part.path)
            if (old.get("status"), old.get("source_id"), old.get("source_revision"),
                    old.get("bundle_primary")) != (
                    "bundle-sibling", part.file_id, part.revision, item.path):
                raise ValueError(f"companion has another claim or terminal state: {part.path}")
    job_dir = root / "intake" / item.project / item.file_id / item.revision
    job_path = job_dir / "job.json"
    prior = json.loads(job_path.read_text(encoding="utf-8")) if job_path.exists() else {}
    if prior and (prior.get("id"), prior.get("revision")) != (item.file_id, item.revision):
        raise ValueError("journal belongs to another Drive revision")
    started = watcher.parse_started_from_name(pathlib.PurePosixPath(item.path).stem)
    if started is None:
        started = dt.datetime.fromisoformat(item.modified.replace("Z", "+00:00")).replace(tzinfo=None)
    sid = watcher.generate_session_id(pathlib.Path(pathlib.PurePosixPath(item.path).name), started)
    sid = sid[:65] + "-" + hashlib.sha256(f"{item.file_id}:{item.revision}".encode()).hexdigest()[:8]
    period = started.strftime("%Y-%m")
    relative = f"{pid}/sessions/{period}/{sid}"
    out_dir = root / "outputs" / relative / "transcripts"
    job = prior or {"id": item.file_id, "revision": item.revision,
                    "source": f"{item.project}/{item.path}", "session_id": sid,
                    "phase": "copying", "output": relative, "companions": companions_key,
                    "code_sha256": code_digest(),
                    "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if prior and (prior.get("session_id"), prior.get("output")) != (sid, relative):
        raise ValueError("journal output changed for this Drive revision")
    if prior and prior.get("companions", []) != companions_key:
        raise ValueError("meeting companions changed; refusing to reuse old ASR output")
    atomic_json(job_path, job)
    if job["phase"] in {"done", "pruned"}:
        published_state = drive.state(item.project, item.path)
        if (job["phase"] == "done" and published_state.get("status") == "asr-done" and
                published_state.get("source_revision") == item.revision and
                published_state.get("publication_verified") is True):
            return published_state
        raise ValueError("terminal local journal and Drive state disagree; manual reconciliation")

    if job["phase"] == "copying":
        if claim_check:
            claim_check()
        publish_progress(drive, item, job_dir, job, pid, "copying", cfg)
        refresh_status(drive, item.project, root)
    local = stage(drive, item, root / "intake")
    for part in companions:
        place_companion(drive, part, root, local.parent)
    if job["phase"] == "copying":
        job["phase"] = "staged"
        atomic_json(job_path, job)

    if job["phase"] not in {"processed", "publishing", "done"}:
        if claim_check:
            claim_check()
        publish_progress(drive, item, local.parent, job, pid, "in-progress", cfg)
        refresh_status(drive, item.project, root)
        executor = transcribe or _default_transcribe
        transcript, meta = executor(local, out_dir, cfg, config_path, pid)
        if not transcript.is_file() or transcript.stat().st_size == 0 or meta.get("status") != "ok":
            raise RuntimeError("transcriber returned no complete transcript")
        job.update({"phase": "processed", "transcript": transcript.name})
        atomic_json(job_path, job)
    publish_progress(drive, item, local.parent, job, pid, "awaiting-publication", cfg)
    refresh_status(drive, item.project, root)
    transcript = out_dir / job["transcript"]
    if not transcript.is_file() or transcript.stat().st_size == 0:
        raise RuntimeError("local result missing; not marking source done")
    job["phase"] = "publishing"
    atomic_json(job_path, job)
    if claim_check:
        claim_check()
    local_session = out_dir.parent
    hub = pathlib.Path(cfg["hub_root"])
    remote_transcript = str(hub / relative / "transcripts" / transcript.name)
    rewrite_output_paths(out_dir, local, str(hub / relative / "transcripts"),
                         str(hub / item.project / item.path),
                         related=[(str(local.parent / safe_parts(part.path)[-1]),
                                   str(hub / part.project / pathlib.Path(*safe_parts(part.path))))
                                  for part in companions])
    drive.call("copy", str(local_session), drive.path(relative))
    published = remote_verified(drive, relative, local_session)
    source_unchanged(drive, item)
    for part in companions:
        source_unchanged(drive, part)

    transcript_info = published.get("transcripts/" + transcript.name) or {}
    if not transcript_info.get("ID"):
        raise RuntimeError("published transcript has no Drive ID")
    state = {"status": "asr-done", "pid": pid, "session_id": sid,
             "source_id": item.file_id, "source_revision": item.revision,
             "host": watcher.host_label_of(cfg),
             "started_at": job.get("started_at") or dt.datetime.now(dt.timezone.utc).isoformat(),
             "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "transcript_path": remote_transcript,
             "transcript_url": "https://drive.google.com/file/d/" + transcript_info["ID"] + "/view",
             "publication_verified": True, "worker_completed": True,
             "last_error": None}
    if needs_classification:
        state["needs_classification"] = True
    state_file = local.parent / "published-state.json"
    atomic_json(state_file, state)
    # Mirror first, source-sidecar LAST: older consumers never see a green source
    # marker before the pipeline output and its state exist in Drive.
    drive.call("copyto", str(state_file), drive.path(relative, "pipeline/state.json"))
    confirmed = json.loads(drive.call("cat", drive.path(relative, "pipeline/state.json")))
    if confirmed.get("source_revision") != item.revision:
        raise RuntimeError("pipeline state was not published")
    if claim_check:
        claim_check()
    for part in companions:
        if pathlib.PurePosixPath(part.path).suffix.lower() not in MEDIA:
            continue
        source_unchanged(drive, part)
        sibling_state = {"status": "bundle-sibling", "pid": pid,
                         "source_id": part.file_id, "source_revision": part.revision,
                         "bundle_primary": item.path, "session_id": sid,
                         "host": state["host"], "finished_at": state["finished_at"]}
        sibling_file = local.parent / (part.file_id + "-sibling-state.json")
        atomic_json(sibling_file, sibling_state)
        drive.call("copyto", str(sibling_file), drive.path(part.project, part.path + ".state.json"))
        confirmed = json.loads(drive.call("cat", drive.path(part.project, part.path + ".state.json")))
        if confirmed.get("source_revision") != part.revision or confirmed.get("status") != "bundle-sibling":
            raise RuntimeError("sibling state publication was not verified")
        marker_unique_id(drive, part)
    source_unchanged(drive, item)
    if claim_check:
        claim_check()
    drive.call("copyto", str(state_file), drive.path(item.project, item.path + ".state.json"))
    confirmed = json.loads(drive.call("cat", drive.path(item.project, item.path + ".state.json")))
    if confirmed.get("status") != "asr-done" or confirmed.get("source_revision") != item.revision:
        raise RuntimeError("source state publication was not verified")
    if marker_unique_id(drive, item) != job.get("marker_id"):
        raise RuntimeError("source state Drive ID changed at completion")
    job["phase"] = "done"
    job["published_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(job_path, job)
    refresh_status(drive, item.project, root)
    return state


def run_claimed(drive: Drive, item: MediaFile, root: pathlib.Path, cfg: dict,
                config_path: pathlib.Path, companions: list[MediaFile]) -> dict:
    """Use the legacy cross-node claim + heartbeat for exactly the same source."""
    if not cfg.get("enable_multi_machine"):
        raise RuntimeError("shared Hub execution needs enable_multi_machine: true")
    worker_cfg = worker_config(cfg)
    hub = pathlib.Path(cfg["hub_root"])
    if not os.path.ismount(hub):
        raise RuntimeError("Hub mount missing: shared claim cannot be verified")
    source = hub / item.project / pathlib.Path(*safe_parts(item.path))
    if not watcher.try_claim_file(source, worker_cfg):
        raise RuntimeError(f"claim held elsewhere: {item.project}/{item.path}")

    def check_owned():
        try:
            value = json.loads(watcher.claim_path(source).read_text(encoding="utf-8"))["claim"]
            if (value.get("claimed_by") != watcher.host_label_of(worker_cfg) or
                    watcher.is_claim_expired(value, watcher.utcnow(),
                                             int(worker_cfg.get("claim_lease_minutes", 30)))):
                raise RuntimeError("shared claim lost or expired")
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError("shared claim cannot be verified") from exc

    with watcher._ClaimHeartbeat(source, worker_cfg):
        # The selection happened before claim convergence; read it again after
        # winning so a completed file or a changed bundle cannot be reprocessed.
        fresh, peers = choose_group(drive, item.project, item.path,
                                    allow_vtt=bool(cfg.get("zoom_vtt_only_input")))
        if ((fresh.file_id, fresh.revision) != (item.file_id, item.revision) or
                [(p.file_id, p.revision) for p in peers] !=
                [(p.file_id, p.revision) for p in companions]):
            raise RuntimeError("input or bundle changed while acquiring the claim")
        check_owned()
        result = process_one(drive, fresh, root, worker_cfg, config_path,
                             companions=peers, claim_check=check_owned)
    watcher.retire_claim(source, worker_cfg)  # only a verified asr-done retires the claim
    return result


def media_e2e_receipt(root: pathlib.Path, cfg: dict, item: MediaFile, state: dict) -> None:
    """An actual media run with a shared claim opens a short operator cutover window."""
    if pathlib.PurePosixPath(item.path).suffix.lower() not in MEDIA:
        return  # a VTT-only smoke test does not validate GPU/media/bundle handling
    if state.get("status") != "asr-done" or state.get("publication_verified") is not True:
        raise RuntimeError("media E2E was not verified")
    journal = root / "intake" / item.project / item.file_id / item.revision / "job.json"
    job = json.loads(journal.read_text(encoding="utf-8"))
    if job.get("phase") != "done" or job.get("code_sha256") != code_digest():
        raise RuntimeError("media E2E was not run to completion with this worker code")
    atomic_json(root / "media-e2e.json", {
        "source": f"{item.project}/{item.path}", "source_id": item.file_id,
        "source_revision": item.revision,
        "host": watcher.host_label_of(worker_config(cfg)),
        "code_sha256": code_digest(),
        "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })


def require_media_e2e(drive: Drive, root: pathlib.Path, cfg: dict,
                      now: dt.datetime | None = None) -> None:
    """No whole-Hub execution without a recent, same-code, still-published media proof."""
    try:
        receipt = json.loads((root / "media-e2e.json").read_text(encoding="utf-8"))
        now = now or dt.datetime.now(dt.timezone.utc)
        age = now - dt.datetime.fromisoformat(receipt["verified_at"])
        if not dt.timedelta(0) <= age <= dt.timedelta(hours=24):
            raise ValueError("media E2E is older than 24 hours")
        if (receipt["code_sha256"] != code_digest()
                or receipt["host"] != watcher.host_label_of(worker_config(cfg))):
            raise ValueError("worker code or host changed after media E2E")
        source = safe_parts(receipt["source"])
        state = drive.state(source[0], "/".join(source[1:]))
        if (state.get("status"), state.get("publication_verified"),
                state.get("source_id"), state.get("source_revision")) != (
                "asr-done", True, receipt["source_id"], receipt["source_revision"]):
            raise ValueError("media E2E result is no longer verified in Drive")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"whole-Hub cutover blocked: {exc}") from exc


def run_whole_hub(drive: Drive, root: pathlib.Path, cfg: dict,
                  config_path: pathlib.Path, max_files: int) -> dict:
    """Bounded single-node sweep; other nodes may only compete via the shared claim."""
    if max_files < 1 or not cfg.get("enable_multi_machine"):
        raise ValueError("whole-Hub sweep needs max_files >= 1 and multi-machine claim")
    legacy_inactive()
    require_media_e2e(drive, root, cfg)
    report = {"processed": 0, "attempted": 0, "quarantined": 0,
              "incomplete": 0, "failed": 0, "unscanned_projects": 0}
    with single_worker(root):
        available = projects(drive.list())
        for position, project in enumerate(available):
            if report["attempted"] >= max_files:
                report["unscanned_projects"] = len(available) - position
                break
            try:
                jobs, quarantined = pending_inputs(
                    drive, project, cfg, root / "state-cache" / project, worker_root=root)
                report["quarantined"] += len(quarantined)
            except (OSError, RuntimeError, ValueError) as exc:
                report["incomplete"] += 1
                log_private_failure(root, "scan", exc)
                print(f"INCOMPLETE project-scan ({type(exc).__name__})")
                continue
            for item, peers in jobs:
                if report["attempted"] >= max_files:
                    break
                report["attempted"] += 1
                try:
                    result = run_claimed(drive, item, root, cfg, config_path, peers)
                    if result.get("status") == "asr-done":
                        report["processed"] += 1
                except (OSError, RuntimeError, ValueError) as exc:
                    report["failed"] += 1
                    try:
                        defer_failed_job(root, item, exc)
                    except (OSError, ValueError, TypeError) as journal_error:
                        report["incomplete"] += 1
                        log_private_failure(root, item.file_id, journal_error)
                        print(f"LOCAL JOURNAL ERROR id={item.file_id[:8]}")
                    log_private_failure(root, item.file_id, exc)
                    print(f"DEFER id={item.file_id[:8]} ({type(exc).__name__})")
                    # One failing input must not prevent another project, but
                    # repeated attempts to the same input wait for the next tick.
    return report


def cleanup_local(drive: Drive, root: pathlib.Path, *, dry_run: bool = True,
                  now: dt.datetime | None = None) -> list[dict]:
    """Retain local media for 30 days AFTER verified Hub publication, never delete Hub."""
    now = now or dt.datetime.now(dt.timezone.utc)
    intake = (root / "intake").resolve()
    outputs = (root / "outputs").resolve()
    findings = []
    for journal in (root / "intake").glob("*/*/*/job.json"):
        try:
            job = json.loads(journal.read_text(encoding="utf-8"))
            if job.get("phase") not in {"done", "pruning"} or not job.get("published_at"):
                continue
            published = dt.datetime.fromisoformat(job["published_at"])
            if published.tzinfo is None or now - published < dt.timedelta(days=30):
                continue
            source = safe_parts(job["source"])
            if len(source) < 2:
                raise ValueError("job source is not a project path")
            project, rel = source[0], "/".join(source[1:])
            if (journal.parent.parent.name, journal.parent.name) != (
                    job.get("id"), job.get("revision")):
                raise ValueError("journal is not in its ID/revision directory")
            state = drive.state(project, rel)
            if (state.get("status"), state.get("publication_verified"),
                    state.get("source_revision")) != ("asr-done", True, job["revision"]):
                raise ValueError("Drive source marker does not confirm this publication")
            output_link = (root / "outputs").joinpath(*safe_parts(job["output"]))
            output = output_link.resolve()
            if (output_link.is_symlink() or not output.is_relative_to(outputs) or
                    output == outputs or not journal.resolve().is_relative_to(intake)):
                raise ValueError("job paths escape local worker root")
            if job["phase"] == "done":
                remote_verified(drive, job["output"], output)
            files = [journal.parent / source[-1]]
            files += [journal.parent / safe_parts(part["path"])[-1]
                      for part in job.get("companions", [])]
            files += [(root / "intake" / project / part["id"] / part["revision"] /
                       safe_parts(part["path"])[-1]) for part in job.get("companions", [])]
            files += [journal.parent / "asr-worker.log",
                      journal.parent / (source[-1] + ".asr-log.txt")]
            for path in files:
                if not path.resolve().is_relative_to(intake) or path.is_symlink():
                    raise ValueError("local source path escapes intake root")
            if output.is_symlink() or any(path.is_symlink() for path in output.rglob("*")):
                raise ValueError("worker output contains a symlink; manual review")
            findings.append({"source": job["source"], "action": "eligible" if dry_run else "pruned"})
            if dry_run:
                continue
            job["phase"] = "pruning"
            atomic_json(journal, job)
            for path in files:
                path.unlink(missing_ok=True)
            if output.exists():
                shutil.rmtree(output)
            job["phase"] = "pruned"
            job["pruned_at"] = now.isoformat()
            atomic_json(journal, job)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            findings.append({"source": str(journal.parent), "action": "held",
                             "reason": f"{type(exc).__name__}: {exc}"})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project")
    parser.add_argument("--file", help="relative input path within project")
    parser.add_argument("--all", action="store_true", help="read-only whole-Hub queue inspection")
    parser.add_argument("--max-files", type=int, default=1,
                        help="whole-Hub run limit, default one verified source per sweep")
    parser.add_argument("--cleanup-local", action="store_true",
                        help="report copies older than 30 days; --run permits local deletion")
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("--rclone-config")
    parser.add_argument("--remote", default="gdrive:Hub")
    parser.add_argument("--local-root", type=pathlib.Path, required=True)
    parser.add_argument("--run", action="store_true", help="actually start the local worker")
    parser.add_argument("--isolated-vtt-canary", action="store_true",
                        help="permit one _test* VTT only if old watcher config excludes VTT")
    parser.add_argument("--manual-vtt", action="store_true",
                        help="explicit one-file VTT backlog exception; never part of --all")
    parser.add_argument("--legacy-config", type=pathlib.Path,
                        help="actual live config of the running watcher (required for canary)")
    args = parser.parse_args()
    if args.cleanup_local:
        if args.all or args.project or args.file:
            parser.error("cleanup cannot be combined with project or queue selection")
    elif args.all:
        if args.project or args.file or args.manual_vtt:
            parser.error("--all cannot be combined with a single file")
    elif not args.project or not args.file:
        parser.error("--project and --file are both required unless --all is used")
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    if args.local_root.resolve().is_relative_to(pathlib.Path(cfg["hub_root"]).resolve()):
        parser.error("local-root must not be inside the mounted Hub")
    drive = Drive(args.remote, args.rclone_config)
    if args.cleanup_local:
        with single_worker(args.local_root):
            report = cleanup_local(drive, args.local_root, dry_run=not args.run)
        for row in report:
            print(f"{row['action']}: {row['source']}")
        return 1 if any(row["action"] == "held" for row in report) else 0
    if args.all:
        if args.run:
            summary = run_whole_hub(drive, args.local_root, cfg, args.config,
                                    args.max_files)
            print("TOTAL", summary)
            return 1 if summary["incomplete"] or summary["failed"] else 0
        counts = {"pending": 0, "quarantined": 0, "incomplete": 0}
        for project in projects(drive.list()):
            try:
                jobs, quarantined = pending_inputs(
                    drive, project, cfg, args.local_root / "state-cache" / project,
                    worker_root=args.local_root)
                counts["pending"] += len(jobs)
                counts["quarantined"] += len(quarantined)
                print(f"{project}: pending={len(jobs)} quarantined={len(quarantined)}")
            except (ValueError, RuntimeError, OSError) as exc:
                counts["incomplete"] += 1
                print(f"{project}: INCOMPLETE: {exc}")
        print("TOTAL", counts)
        return 1 if counts["incomplete"] else 0
    item, companions = choose_group(drive, args.project, args.file,
                                    allow_vtt=bool(cfg.get("zoom_vtt_only_input", False)))
    if args.manual_vtt and pathlib.PurePosixPath(item.path).suffix.lower() != ".vtt":
        parser.error("--manual-vtt requires a single VTT input")
    if args.run:
        require_vtt_route(item, cfg, manual=args.manual_vtt,
                          isolated=args.isolated_vtt_canary)
    if not args.run:
        print(f"DRY RUN: {item.project}/{item.path} ID={item.file_id} bytes={item.size} "
              f"companions={len(companions)}")
        return 0
    if args.isolated_vtt_canary:
        if not args.legacy_config:
            parser.error("--isolated-vtt-canary requires --legacy-config")
        isolated_vtt_canary(item, args.legacy_config)
    else:
        legacy_inactive()
    with single_worker(args.local_root):
        if args.isolated_vtt_canary:
            result = process_one(drive, item, args.local_root, cfg, args.config,
                                 companions=companions)
        else:
            result = run_claimed(drive, item, args.local_root, cfg, args.config,
                                 companions)
            media_e2e_receipt(args.local_root, cfg, item, result)
    print(f"published: {result['session_id']} ({result['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
