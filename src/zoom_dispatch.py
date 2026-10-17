#!/usr/bin/env python3
"""Deliver downloaded Zoom VTT/chat files to project inboxes, without media.

The source is zoomrs' SQLite database and its local recording repository, not
its GPL code or Zoom credentials. A VTT is published LAST so the watcher sees
it only after its chat sidecar has arrived. This program never deletes sources.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys


PID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
WORKER = re.compile(r"w\d+\Z", re.I)


def load_config(path: pathlib.Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("database", "repository", "hub_root", "state_file", "fallback_pid"):
        if not config.get(key):
            raise ValueError(f"zoom dispatcher needs {key}")
    for pid in [config["fallback_pid"], *(rule["pid"] for rule in config.get("routes", []))]:
        if not PID.fullmatch(pid):
            raise ValueError(f"unsafe pid: {pid!r}")
    if not WORKER.fullmatch(config.get("worker_folder", "w1")):
        raise ValueError("worker_folder must be w1, w2, etc.")
    if int(config.get("max_meetings_per_run", 3)) < 1:
        raise ValueError("max_meetings_per_run must be positive")
    for rule in config.get("routes", []):
        if ("regex" in rule) == ("contains" in rule):
            raise ValueError("each route needs exactly one of regex/contains")
        if "regex" in rule:
            re.compile(rule["regex"], re.I)
    return config


def project_id(topic: str, config: dict) -> str:
    for rule in config.get("routes", []):
        if ("contains" in rule and rule["contains"].casefold() in topic.casefold()
                or "regex" in rule and re.search(rule["regex"], topic, re.I)):
            return rule["pid"]
    return config["fallback_pid"]


def _meetings(db: pathlib.Path) -> list[dict]:
    """Read zoomrs' tables without opening its SQLite DB for writing."""
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            'SELECT m.uuid, m.topic, m.startTime AS meeting_start, '
            'r.id AS record_id, r.type, r.fileExtension, r.fileSize, '
            'r.status, r.path FROM meetings m JOIN records r ON r.meetingId=m.uuid '
            "WHERE r.type IN ('audio_transcript','chat_file') "
            'ORDER BY m.startTime DESC, r.id'
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, dict] = {}
    for row in rows:
        meeting = grouped.setdefault(row["uuid"], {
            "uuid": row["uuid"], "topic": row["topic"], "start": row["meeting_start"],
            "records": [],
        })
        meeting["records"].append(dict(row))
    return list(grouped.values())


def _local_file(record: dict, repository: pathlib.Path, settle_sec: int) -> pathlib.Path | None:
    path = pathlib.Path(record["path"] or "").expanduser()
    if record["status"] != "downloaded" or not record["path"]:
        return None
    root = repository.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("zoomrs file is outside the configured repository")
    if not resolved.is_file():
        return None
    stat = resolved.stat()
    if record["fileSize"] and stat.st_size != int(record["fileSize"]):
        return None
    now = dt.datetime.now().timestamp()
    if now - stat.st_mtime < settle_sec:
        return None
    return resolved


def _digest(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_once(source: pathlib.Path, target: pathlib.Path) -> None:
    if target.exists():
        if source.stat().st_size == target.stat().st_size and _digest(source) == _digest(target):
            return
        raise ValueError("existing inbox file differs from zoomrs source")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        with source.open("rb") as src, temp.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if _digest(source) != _digest(temp):
            raise OSError("staged Zoom file failed hash verification")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _state(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_state(path: pathlib.Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _filename(meeting: dict, vtt: dict) -> str:
    try:
        stamp = dt.datetime.fromisoformat(meeting["start"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid zoomrs meeting startTime") from exc
    key = hashlib.sha256((meeting["uuid"] + ":" + vtt["record_id"]).encode()).hexdigest()[:12]
    return stamp.strftime("%Y%m%d_%H%M%S") + "_" + key


def _already_processed(project: pathlib.Path, filename: str) -> bool:
    """Recover when a crash lost the local manifest after watcher completion."""
    index = project / "_sessions-index.md"
    if index.is_file():
        for line in index.read_text(encoding="utf-8").splitlines():
            if f"| `{filename}` | asr-done |" in line:
                return True
    # The watcher may have already moved the VTT into _processed while the
    # project index is still waiting for its next refresh.
    inbox = project / f"_{project.name}_inbox"
    if inbox.is_dir():
        folders = [inbox / "_processed", project / "_processed"]
        for worker in inbox.iterdir():
            if not WORKER.fullmatch(worker.name) or not worker.is_dir():
                continue
            folders.extend((worker, worker / "_processed"))
        for folder in folders:
            sidecar = folder / (filename + ".state.json")
            if sidecar.is_file():
                try:
                    if json.loads(sidecar.read_text(encoding="utf-8")).get("status") == "asr-done":
                        return True
                except (OSError, ValueError):
                    continue
    return False


def dispatch_once(config: dict, *, dry_run: bool = False) -> dict:
    repository = pathlib.Path(config["repository"]).expanduser()
    hub = pathlib.Path(config["hub_root"]).expanduser()
    # Failing closed here matters: mkdir() on an unmounted hub would silently
    # write to the node's local underlying directory and mark meetings delivered.
    if not hub.is_dir() or (config.get("require_hub_mount", True) and not hub.is_mount()):
        raise ValueError("Hub mount is unavailable; refusing to dispatch")
    state_path = pathlib.Path(config["state_file"]).expanduser()
    saved = _state(state_path)
    limit = int(config.get("max_meetings_per_run", 3))
    settle = int(config.get("min_stable_age_seconds", 120))
    if settle < 0:
        raise ValueError("min_stable_age_seconds cannot be negative")
    report = {"delivered": 0, "pending": 0, "already": 0, "ambiguous": 0}
    for meeting in _meetings(pathlib.Path(config["database"]).expanduser()):
        if meeting["uuid"] in saved:
            report["already"] += 1
            continue
        vtts = [r for r in meeting["records"] if r["type"] == "audio_transcript"]
        chats = [r for r in meeting["records"] if r["type"] == "chat_file"]
        if len(vtts) != 1 or len(chats) > 1:
            report["ambiguous"] += 1
            continue  # never discard one of several transcript/chat files silently
        vtt = _local_file(vtts[0], repository, settle)
        chat = _local_file(chats[0], repository, settle) if chats else None
        if vtt is None or chats and chat is None:
            report["pending"] += 1
            continue
        pid = project_id(meeting["topic"] or "", config)
        stem = _filename(meeting, vtts[0])
        project = hub / pid
        inbox = project / f"_{pid}_inbox" / config.get("worker_folder", "w1")
        vtt_name = f"{stem}.transcript.vtt"
        target_vtt = inbox / vtt_name
        if _already_processed(project, vtt_name):
            if not dry_run:
                saved[meeting["uuid"]] = {"pid": pid, "vtt": vtt_name, "recovered": True}
                _save_state(state_path, saved)
            report["already"] += 1
            continue
        if not dry_run:
            if chat is not None:
                _copy_once(chat, inbox / f"{stem}.chat.txt")
            _copy_once(vtt, target_vtt)  # last: this is the watcher's ready signal
            saved[meeting["uuid"]] = {"pid": pid, "vtt": vtt_name,
                                      "delivered_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            _save_state(state_path, saved)
        report["delivered"] += 1
        if report["delivered"] >= limit:
            break
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        report = dispatch_once(load_config(args.config), dry_run=args.dry_run)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Zoom dispatch failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
