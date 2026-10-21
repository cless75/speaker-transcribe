#!/usr/bin/env python3
"""One Lenovo scheduler for Drive-staged media and local zoomrs bundles.

The existing per-source journals remain authoritative for ASR and publication
resume. SQLite stores fair scheduling, attempts, and an auditable migration
index; it never replaces a source marker or grants publication by itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import hub_worker as hub
import zoom_local_worker as zoom
from hub_intake import Drive, projects


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(config: dict) -> dict:
    """Pin implementation and node routing without storing private config values."""
    source = pathlib.Path(__file__).resolve().parent
    files = ("unified_intake.py", "hub_worker.py", "hub_intake.py",
             "zoom_local_worker.py", "zoom_dispatch.py", "audio_inbox_watch.py",
             "media_transcribe.py")
    return {"code": {name: digest(source / name) for name in files},
            "units": {name: digest(source.parent / "scripts" / "systemd" / name)
                      for name in ("unified-intake@.service", "unified-intake@.timer")},
            "config": {key: digest(pathlib.Path(config[key])) for key in (
                "hub_config", "zoom_config")},
            "routing": {key: config.get(key) for key in (
                "remote", "hub_worker_root", "queue_path", "deployed_at")},
            "node": hub.watcher.host_label_of(hub.worker_config(
                json.loads(pathlib.Path(config["hub_config"]).read_text(encoding="utf-8"))))}


class Queue:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.db = sqlite3.connect(path)
        if os.name == "posix":
            os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs ("
                        "key TEXT PRIMARY KEY, source TEXT NOT NULL, project TEXT NOT NULL, "
                        "locator TEXT NOT NULL, revision TEXT NOT NULL, status TEXT NOT NULL, "
                        "attempts INTEGER NOT NULL DEFAULT 0, retry_at TEXT, error TEXT, "
                        "updated_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert(self, key: str, source: str, project: str, locator: str, revision: str,
               *, done: bool = False) -> None:
        status = "done" if done else "pending"
        self.db.execute("INSERT INTO jobs (key, source, project, locator, revision, status, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                        "status=CASE WHEN excluded.status='done' OR jobs.status='done' "
                        "THEN 'done' ELSE jobs.status END, "
                        "updated_at=excluded.updated_at",
                        (key, source, project, locator, revision, status, now().isoformat()))
        self.db.commit()

    def update(self, key: str, status: str, *, error: str | None = None,
               retry_at: str | None = None, attempt: bool = False) -> None:
        self.db.execute("UPDATE jobs SET status=?, error=?, retry_at=?, "
                        "attempts=attempts+?, updated_at=? WHERE key=?",
                        (status, error, retry_at, int(attempt), now().isoformat(), key))
        self.db.commit()

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT status, attempts, retry_at FROM jobs WHERE key=?", (key,)).fetchone()
        return dict(zip(("status", "attempts", "retry_at"), row)) if row else None

    def last_source(self) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key='last_source'").fetchone()
        return row[0] if row else "zoom"

    def set_last_source(self, source: str) -> None:
        self.db.execute("INSERT INTO settings VALUES ('last_source', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (source,))
        self.db.commit()

    def record_run(self, report: dict) -> None:
        self.db.execute("INSERT INTO settings VALUES ('last_run', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps({"finished_at": now().isoformat(), **report}),))
        self.db.commit()

    def counts(self) -> dict:
        return dict(self.db.execute("SELECT status, count(*) FROM jobs GROUP BY status"))

    def status(self) -> dict:
        row = self.db.execute("SELECT value FROM settings WHERE key='last_run'").fetchone()
        by_source = {f"{source}:{status}": count for source, status, count in self.db.execute(
            "SELECT source, status, count(*) FROM jobs GROUP BY source, status")}
        return {"jobs": by_source, "last_run": json.loads(row[0]) if row else None}


def hub_key(item) -> str:
    return f"drive:{item.project}:{item.file_id}:{item.revision}"


def zoom_key(item: dict) -> str:
    return "zoom:" + hashlib.sha256(item["uuid"].encode()).hexdigest()[:16]


def configs(config: dict) -> tuple[dict, dict, Drive]:
    hc = json.loads(pathlib.Path(config["hub_config"]).read_text(encoding="utf-8"))
    zc = json.loads(pathlib.Path(config["zoom_config"]).read_text(encoding="utf-8"))
    drive = Drive(config.get("remote", "gdrive:Hub"), config.get("rclone_config"))
    if not zc["hub_worker_root"] == config["hub_worker_root"]:
        raise ValueError("Zoom and Hub must share the same Lenovo lock")
    return hc, zc, drive


def verify_zoom_proof(drive: Drive, root: pathlib.Path, *,
                      after: dt.datetime | None = None) -> list[str]:
    verified = []
    for path in (root / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("phase") != "done":
            continue
        local = json.loads((path.parent / "state.json").read_text(encoding="utf-8"))
        when = dt.datetime.fromisoformat(local["finished_at"])
        if after is not None and when < after:
            continue
        if local.get("status") != "asr-done" or local.get("publication_verified") is not True:
            continue
        publication = root / "publication" / job["output"]
        hub.remote_verified(drive, job["output"], publication)
        remote = json.loads(drive.call("cat", drive.path(job["output"], "pipeline", "state.json")))
        if remote == local and local.get("speaker_source") == "zoom_vtt":
            verified.append(job["output"])
    return verified


def attest(config: dict, drive: Drive, hc: dict, zc: dict,
           *, deployed_at: dt.datetime | None = None) -> dict:
    """Operator action after both same-release E2Es, not an automatic bypass."""
    hub.require_media_e2e(drive, pathlib.Path(config["hub_worker_root"]), hc,
                          max_age=dt.timedelta(hours=24))
    deployed_at = deployed_at or dt.datetime.fromisoformat(config["deployed_at"])
    if deployed_at.tzinfo is None or deployed_at > now():
        raise ValueError("release deployment time must be timezone-aware and in the past")
    hub_proof = json.loads((pathlib.Path(config["hub_worker_root"]) /
                            "media-e2e.json").read_text(encoding="utf-8"))
    if dt.datetime.fromisoformat(hub_proof["verified_at"]) < deployed_at:
        raise RuntimeError("Hub media E2E predates this release deployment")
    zoom_proofs = verify_zoom_proof(drive, pathlib.Path(zc["local_root"]), after=deployed_at)
    if not zoom_proofs:
        raise RuntimeError("no verified Zoom media+VTT E2E for this release")
    receipt = {"identity": identity(config), "attested_at": now().isoformat(),
               "hub_proof": [hub_proof["source_id"], hub_proof["source_revision"]],
               "zoom_proof": zoom_proofs[0]}
    hub.atomic_json(pathlib.Path(config["hub_worker_root"]) / "release-attestation.json", receipt)
    return receipt


def require_attestation(config: dict, drive: Drive, hc: dict, zc: dict) -> None:
    receipt = json.loads((pathlib.Path(config["hub_worker_root"]) /
                          "release-attestation.json").read_text(encoding="utf-8"))
    if receipt.get("identity") != identity(config):
        raise RuntimeError("release identity changed; repeat both E2Es")
    hub.require_media_e2e(drive, pathlib.Path(config["hub_worker_root"]), hc, max_age=None)
    hub_proof = json.loads((pathlib.Path(config["hub_worker_root"]) /
                            "media-e2e.json").read_text(encoding="utf-8"))
    if receipt.get("hub_proof") != [hub_proof["source_id"], hub_proof["source_revision"]]:
        raise RuntimeError("attested Hub proof was replaced")
    if receipt.get("zoom_proof") not in verify_zoom_proof(
            drive, pathlib.Path(zc["local_root"])):
        raise RuntimeError("Zoom release proof is no longer published")


def old_consumers_inactive() -> None:
    hub.legacy_inactive()
    for unit in ("hub-worker@clessd.timer", "hub-worker@clessd.service",
                 "zoom-local-worker@clessd.timer", "zoom-local-worker@clessd.service",
                 "zoom-dispatch@clessd.timer", "zoom-dispatch@clessd.service"):
        result = subprocess.run(["systemctl", "show", "--value", "-p", "ActiveState", unit],
                                capture_output=True, text=True, check=False)
        if result.returncode or result.stdout.strip() not in {"inactive", "failed"}:
            raise RuntimeError("competing intake consumer active or cannot be checked")
        if unit.endswith(".timer"):
            enabled = subprocess.run(["systemctl", "is-enabled", unit],
                                     capture_output=True, text=True, check=False)
            if enabled.stdout.strip() in {"enabled", "enabled-runtime", "linked"}:
                raise RuntimeError("competing intake timer is still enabled")


def discover(config: dict, hc: dict, zc: dict, drive: Drive,
             queue: Queue | None = None) -> tuple[dict[str, tuple], dict]:
    candidates: dict[str, tuple] = {}
    report = {"hub_incomplete": 0, "hub_quarantined": 0, "zoom_ready": 0}
    for item in zoom.select(zc):
        key = zoom_key(item)
        journal = pathlib.Path(zc["local_root"]) / "jobs" / key.split(":", 1)[1] / "job.json"
        if journal.is_file() and json.loads(journal.read_text(encoding="utf-8")).get("phase") == "done":
            continue
        candidates[key] = ("zoom", item)
        report["zoom_ready"] += 1
        if queue:
            queue.upsert(key, "zoom", item["pid"], key.split(":", 1)[1],
                         "local-content-fingerprint-in-journal")
    for pid in projects(drive.list()):
        try:
            jobs, held = hub.pending_inputs(drive, pid, hc,
                pathlib.Path(config["hub_worker_root"]) / "state-cache" / pid,
                worker_root=pathlib.Path(config["hub_worker_root"]))
            report["hub_quarantined"] += len(held)
        except (OSError, RuntimeError, ValueError):
            report["hub_incomplete"] += 1
            continue
        for item, peers in jobs:
            key = hub_key(item)
            candidates[key] = ("drive", item, peers)
            if queue:
                queue.upsert(key, "drive", item.project, item.path, item.revision)
    return candidates, report


def migrate(config: dict, queue: Queue, drive: Drive, zc: dict) -> dict:
    """Index existing receipts; never rewrite a per-source journal or marker."""
    report = {"done": 0, "resume": 0, "held": 0}
    root = pathlib.Path(config["hub_worker_root"])
    for path in (root / "intake").glob("*/*/*/job.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if not all(job.get(k) for k in ("id", "revision", "source", "output")):
            report["held"] += 1
            continue
        pid, _, rel = job["source"].partition("/")
        key = f"drive:{pid}:{job['id']}:{job['revision']}"
        done = False
        if job["phase"] in {"done", "pruned"}:
            state = drive.state(pid, rel)
            done = (state.get("status") == "asr-done" and
                    state.get("publication_verified") is True and
                    state.get("source_revision") == job["revision"])
            if done:
                remote = json.loads(drive.call("cat", drive.path(job["output"],
                                                               "pipeline", "state.json")))
                done = (remote.get("status") == "asr-done" and
                        remote.get("source_revision") == job["revision"])
            if not done:
                report["held"] += 1
                continue
        queue.upsert(key, "drive", pid, rel, job["revision"], done=done)
        report["done" if done else "resume"] += 1
    zroot = pathlib.Path(zc["local_root"])
    for path in (zroot / "jobs").glob("*/job.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        key = f"zoom:{job['uuid_digest']}"
        done = False
        if job["phase"] == "done":
            local = json.loads((path.parent / "state.json").read_text(encoding="utf-8"))
            remote = json.loads(drive.call("cat", drive.path(job["output"], "pipeline", "state.json")))
            done = (local == remote and local.get("status") == "asr-done" and
                    local.get("publication_verified") is True)
            if not done:
                report["held"] += 1
                continue
        queue.upsert(key, "zoom", job["pid"], job["uuid_digest"],
                     json.dumps(job["source_fingerprints"], sort_keys=True), done=done)
        report["done" if done else "resume"] += 1
    return report


def schedule(queue: Queue, candidates: dict[str, tuple], limit: int) -> list[str]:
    groups = {"drive": [], "zoom": []}
    for key, task in candidates.items():
        status = queue.get(key)
        if not status or status["status"] == "done":
            continue
        due = status["retry_at"]
        if due and now() < dt.datetime.fromisoformat(due):
            continue
        groups[task[0]].append(key)
    previous = queue.last_source()
    order = []
    while len(order) < limit and any(groups.values()):
        source = "zoom" if previous == "drive" else "drive"
        if not groups[source]:
            source = previous
        order.append(groups[source].pop(0))
        previous = source
    return order


def run(config: dict, *, dry_run: bool = True, max_files: int = 3) -> dict:
    if not 1 <= max_files <= 3:
        raise ValueError("one to three tasks per pass")
    hc, zc, drive = configs(config)
    report = {"attempted": 0, "processed": 0, "failed": 0}
    if dry_run:
        candidates, scan = discover(config, hc, zc, drive)
        return {**report, **scan, "eligible": len(candidates)}
    old_consumers_inactive()
    require_attestation(config, drive, hc, zc)
    if not hc.get("enable_multi_machine"):
        raise RuntimeError("Hub claim must be enabled")
    root = pathlib.Path(config["hub_worker_root"])
    with hub.single_worker(root):
        queue = Queue(pathlib.Path(config["queue_path"]))
        try:
            candidates, scan = discover(config, hc, zc, drive, queue)
            report.update(scan)
            for key in schedule(queue, candidates, max_files):
                task = candidates[key]
                report["attempted"] += 1
                queue.update(key, "processing", attempt=True)
                queue.set_last_source(task[0])
                try:
                    if task[0] == "drive":
                        item, peers = task[1:]
                        result = hub.run_claimed(drive, item, root, hc,
                                                  pathlib.Path(config["hub_config"]), peers)
                        success = result.get("status") == "asr-done"
                    else:
                        success = zoom.process_one(zc, task[1], drive).get("phase") == "done"
                    if not success:
                        raise RuntimeError("worker did not verify final publication")
                    queue.update(key, "done")
                    report["processed"] += 1
                except (OSError, RuntimeError, ValueError) as exc:
                    attempts = queue.get(key)["attempts"]
                    retry = now() + dt.timedelta(minutes=min(60, 2 ** min(attempts - 1, 6)))
                    queue.update(key, "retry", error=type(exc).__name__, retry_at=retry.isoformat())
                    if task[0] == "drive":
                        hub.defer_failed_job(root, task[1], exc)
                    report["failed"] += 1
                    print(f"DEFER {task[0]} {hashlib.sha256(key.encode()).hexdigest()[:8]} "
                          f"({type(exc).__name__})", file=sys.stderr)
            report["queue"] = queue.counts()
            queue.record_run(report)
        finally:
            queue.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--attest", action="store_true")
    parser.add_argument("--status", action="store_true", help="read local queue status without Drive")
    parser.add_argument("--max-files", type=int, default=3)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if sum((args.run, args.migrate, args.attest, args.status)) > 1:
        parser.error("choose only one operation")
    if args.status:
        queue = Queue(pathlib.Path(config["queue_path"]))
        try:
            print(json.dumps(queue.status()))
        finally:
            queue.close()
        return 0
    if args.migrate or args.attest:
        old_consumers_inactive()
        hc, zc, drive = configs(config)
        with hub.single_worker(pathlib.Path(config["hub_worker_root"])):
            if args.attest:
                report = attest(config, drive, hc, zc)
                print(json.dumps({"attested_at": report["attested_at"]}))
            else:
                queue = Queue(pathlib.Path(config["queue_path"]))
                try:
                    print(json.dumps(migrate(config, queue, drive, zc)))
                finally:
                    queue.close()
        return 0
    result = run(config, dry_run=not args.run, max_files=args.max_files)
    print(json.dumps(result))
    return 1 if result.get("failed") or result.get("hub_incomplete") else 0


if __name__ == "__main__":
    raise SystemExit(main())
