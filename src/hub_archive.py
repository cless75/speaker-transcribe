#!/usr/bin/env python3
"""Fail-closed Hub archive inventory and verified NAS copy.

No Hub mutation is exposed here. A project action is policy metadata until a
separate, reviewed, exact-ID trash operation has been implemented and accepted.
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
import tempfile

from hub_intake import Drive, MEDIA, partition_entries, safe_parts, scannable
from hub_worker import atomic_json, remote_verified


def config_checked(cfg: dict) -> dict:
    if cfg.get("schema_version") != 1 or not isinstance(cfg.get("projects"), dict):
        raise ValueError("unsupported archive config")
    if not isinstance(cfg.get("defaults"), dict) or not isinstance(cfg.get("nas"), dict):
        raise ValueError("archive defaults and nas are required")
    if cfg["defaults"].get("enabled") is not False:
        raise ValueError("archive defaults must be disabled")
    for pid, policy in cfg["projects"].items():
        safe_parts(pid)
        if "/" in pid or not isinstance(policy, dict):
            raise ValueError("invalid project policy")
        if policy.get("hub_action", "keep") not in {"keep", "archive", "trash_after_nas"}:
            raise ValueError("unknown Hub action")
        if not isinstance(policy.get("enabled", False), bool):
            raise ValueError("enabled must be boolean")
        if (not isinstance(policy.get("min_days_after_publication", 30), int) or
                policy.get("min_days_after_publication", 30) < 0):
            raise ValueError("invalid age threshold")
    if (cfg["defaults"].get("hub_action", "keep") not in
            {"keep", "archive", "trash_after_nas"} or
            not isinstance(cfg["defaults"].get("min_days_after_publication", 30), int) or
            cfg["defaults"].get("min_days_after_publication", 30) < 0):
        raise ValueError("invalid archive defaults")
    mount = pathlib.PurePosixPath(cfg["nas"]["mount"])
    root = pathlib.PurePosixPath(cfg["nas"]["root"])
    if (not mount.is_absolute() or not root.is_absolute() or
            not root.is_relative_to(mount) or root == mount or
            not isinstance(cfg["nas"].get("min_free_gib"), (int, float)) or
            cfg["nas"]["min_free_gib"] < 0):
        raise ValueError("NAS root must be under an absolute mount")
    identity = cfg["nas"]["identity_file"]
    if safe_parts(identity) != (identity,) or not identity.startswith("."):
        raise ValueError("NAS identity must be a hidden file at mount root")
    return cfg


def policy(cfg: dict, project: str) -> dict:
    safe_parts(project)
    return {**cfg["defaults"], **cfg["projects"].get(project, {})}


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def nas_ready(cfg: dict, *, need_bytes: int = 0) -> pathlib.Path:
    """Never write under an unmounted NAS or an unrecognised replacement."""
    nas = cfg["nas"]
    mount = pathlib.Path(nas["mount"])
    root = pathlib.Path(nas["root"])
    if os.name != "posix":
        raise RuntimeError("NAS copy requires Lenovo POSIX mount verification")
    import subprocess
    try:
        next(mount.iterdir(), None)  # trigger systemd automount before findmnt
    except OSError as exc:
        raise RuntimeError("NAS automount is inaccessible") from exc
    result = subprocess.run(["findmnt", "-n", "-T", str(mount), "-o", "TARGET,SOURCE,FSTYPE"],
                            capture_output=True, text=True, check=False)
    mounts = [line.split() for line in result.stdout.splitlines()]
    if result.returncode or mounts.count([str(mount), nas["source"], "cifs"]) != 1:
        raise RuntimeError("NAS CIFS mount identity mismatch")
    identity = mount / nas["identity_file"]
    if not identity.is_file() or identity.is_symlink() or identity.read_text(
            encoding="utf-8").strip() != "speaker-transcribe-archive-v1":
        raise RuntimeError("NAS identity file missing or unexpected")
    if root.is_symlink() or not root.resolve().is_relative_to(mount.resolve()):
        raise RuntimeError("NAS archive root escapes mount")
    free = shutil.disk_usage(mount).free
    if free - need_bytes < int(nas["min_free_gib"] * 1024 ** 3):
        raise RuntimeError("NAS free-space reserve would be exhausted")
    return root


def inventory(drive: Drive, cfg: dict, project: str,
              *, now: dt.datetime | None = None) -> list[dict]:
    """Fresh Drive listing; legacy markers and missing journals remain held."""
    now = now or dt.datetime.now(dt.timezone.utc)
    entries = drive.list(project, recursive=True)
    proof_path = pathlib.Path(cfg["worker_root"]) / "media-e2e.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8")) if proof_path.is_file() else {}
    clean, duplicated = partition_entries(project, entries)
    counts: dict[str, int] = {}
    for entry in entries:
        if not entry.get("IsDir"):
            counts[entry.get("Path", "")] = counts.get(entry.get("Path", ""), 0) + 1
    by_path = {e["Path"]: e for e in clean if not e.get("IsDir")}
    results = []
    for entry in entries:
        rel = entry.get("Path", "")
        if entry.get("IsDir") or pathlib.PurePosixPath(rel).suffix.lower() not in MEDIA:
            continue
        if not scannable(project, rel):
            continue
        reasons = []
        if counts[rel] != 1 or rel in duplicated:
            reasons.append("ambiguous-source")
        marker = by_path.get(rel + ".state.json")
        if counts.get(rel + ".state.json", 0) != 1 or not marker:
            reasons.append("missing-or-ambiguous-marker")
        digest = (entry.get("Hashes") or {}).get("md5")
        if not entry.get("ID") or not digest or not re.fullmatch(r"[a-fA-F0-9]{32}", digest):
            reasons.append("missing-source-identity-or-md5")
        if entry.get("ID") == proof.get("source_id") or f"{project}/{rel}" == proof.get("source"):
            reasons.append("release-e2e-source")
        # Never archive a stamped media member without an explicit bundle review.
        from hub_worker import ZOOM_TIMESTAMP
        if ZOOM_TIMESTAMP.search(pathlib.PurePosixPath(rel).name):
            reasons.append("meeting-bundle-review-required")
        state = None
        if marker and not reasons:
            try:
                raw = drive.call("cat", drive.path(project, rel + ".state.json"))
                expected = (marker.get("Hashes") or {}).get("md5", "").lower()
                if not expected or hashlib.md5(raw).hexdigest() != expected:
                    raise ValueError("marker checksum mismatch")
                state = json.loads(raw)
            except (RuntimeError, ValueError, TypeError):
                reasons.append("unreadable-marker")
        revision = hashlib.sha256(
            f"{entry.get('ID', '')}\0{entry.get('Size', '')}\0{entry.get('ModTime', '')}".encode()
        ).hexdigest()[:20]
        if state is not None:
            if (state.get("status"), state.get("publication_verified"),
                    state.get("source_revision")) != ("asr-done", True, revision):
                reasons.append("publication-not-verified-for-revision")
        journal = (pathlib.Path(cfg["worker_root"]) / "intake" / project /
                   str(entry.get("ID", "")) / revision / "job.json")
        job = None
        if not reasons:
            try:
                safe_parts(str(entry["ID"]))
                job = json.loads(journal.read_text(encoding="utf-8"))
                if (job.get("phase") not in {"done", "pruned"} or
                        job.get("source") != f"{project}/{rel}" or
                        job.get("revision") != revision or job.get("companions") or
                        job.get("marker_id") != marker.get("ID")):
                    reasons.append("journal-not-eligible-or-has-companions")
                published = dt.datetime.fromisoformat(job["published_at"])
                days = policy(cfg, project).get("min_days_after_publication", 30)
                if published.tzinfo is None or now - published < dt.timedelta(days=days):
                    reasons.append("publication-too-recent")
            except (OSError, ValueError, KeyError, TypeError):
                reasons.append("missing-or-unreadable-journal")
        if not reasons:
            try:
                output = pathlib.Path(cfg["worker_root"]) / "outputs" / job["output"]
                remote_verified(drive, job["output"], output)
            except (RuntimeError, ValueError, OSError, KeyError):
                reasons.append("published-output-not-verified")
        results.append({"project": project, "path": rel, "id": entry.get("ID"),
                        "revision": revision, "size": entry.get("Size"), "md5": digest,
                        "marker_id": marker.get("ID") if marker else None,
                        "marker_md5": (marker.get("Hashes") or {}).get("md5") if marker else None,
                        "status": "eligible" if not reasons else "held", "reasons": reasons,
                        "output": job.get("output") if job else None})
    return results


def copy_verified(drive: Drive, cfg: dict, row: dict) -> pathlib.Path:
    """Copy one already-reviewed source + marker; Hub remains unchanged."""
    if row["status"] != "eligible" or not policy(cfg, row["project"])["enabled"]:
        raise ValueError("project or source not eligible for NAS copy")
    safe_parts(row["project"])
    if len(safe_parts(row["project"])) != 1 or len(safe_parts(row["id"])) != 1 or len(safe_parts(row["revision"])) != 1:
        raise ValueError("unsafe NAS destination")
    root = nas_ready(cfg, need_bytes=int(row["size"]) * 2)
    project, rel = row["project"], row["path"]
    current = next((r for r in inventory(drive, cfg, project)
                    if r["path"] == rel and r["id"] == row["id"] and
                    r["revision"] == row["revision"] and r["status"] == "eligible"), None)
    if current != row:
        raise RuntimeError("source eligibility changed since approval")
    destination = root / project / row["id"] / row["revision"]
    if destination.exists():
        raise RuntimeError("archive destination already exists; inspect before retry")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=".archive-", dir=destination.parent))
    try:
        source = work / "source"
        marker = work / "source.state.json"
        for remote, local, md5 in (
                (drive.path(project, rel), source, row["md5"]),
                (drive.path(project, rel + ".state.json"), marker, row["marker_md5"])):
            drive.call("copyto", remote, str(local))
            if not local.is_file():
                raise RuntimeError("NAS staged copy missing")
            checksum = hashlib.md5()  # nosec: compare with Drive-provided MD5
            with local.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(block)
            if md5 and checksum.hexdigest() != md5.lower():
                raise RuntimeError("NAS staged copy differs from Drive checksum")
        if source.stat().st_size != row["size"]:
            raise RuntimeError("NAS copy has wrong size")
        # Closing the listing-to-copy race: a replacement is not a valid archive.
        after = next((r for r in inventory(drive, cfg, project) if r["path"] == rel), None)
        if after != row:
            raise RuntimeError("Drive source or publication changed during NAS copy")
        record = {"schema_version": 1, "source": row,
                  "copied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "files": {name: {"size": path.stat().st_size, "sha256": sha256(path)}
                            for name, path in (("source", source), ("source.state.json", marker))},
                  "hub_action": "none"}
        atomic_json(work / "manifest.json", record)
        os.replace(work, destination)
        for name, evidence in record["files"].items():
            restored = destination / name
            if (restored.stat().st_size != evidence["size"] or
                    sha256(restored) != evidence["sha256"]):
                raise RuntimeError("NAS read-back failed; Hub not touched")
        return destination
    finally:
        if work.exists():
            shutil.rmtree(work)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=pathlib.Path)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    cfg = config_checked(json.loads(args.config.read_text(encoding="utf-8")))
    drive = Drive(cfg["remote"], cfg.get("rclone_config"))
    print(json.dumps({"project": args.project, "policy": policy(cfg, args.project),
                      "files": inventory(drive, cfg, args.project)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
