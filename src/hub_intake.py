#!/usr/bin/env python3
"""Opt-in Drive inventory, verified local delivery and per-project status pages.

This is deliberately separate from the legacy FUSE watcher. It does not start ASR,
change claims, delete Hub files or switch systemd units. A production cutover must
connect its receipts to the ASR state machine without running two consumers.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import random
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

MEDIA = frozenset({".m4a", ".mp3", ".wav", ".oga", ".mp4", ".mov",
                   ".webm", ".m4v", ".mkv", ".avi"})
ZOOM_TIMESTAMP = re.compile(r"(?:GMT)?(\d{8}[-_]\d{6})")
SKIP_DIRS = frozenset({"sessions", "profiles", "Audio Record", "Transcripts",
                       "Speakers", "recordings", "pipeline", "_failed", "_processed",
                       "_archive"})
GENERATED = "<!-- speaker-transcribe hub-intake status; generated, do not edit -->"
PAGE_VERSION = "0.1.0"  # MAJOR.RELEASE.INC; HTML has crossed into the private Hub

DRIVE_RATE_LIMIT = re.compile(r"rate.?limit", re.IGNORECASE)
DRIVE_QUOTA = re.compile(r"quota", re.IGNORECASE)


class DriveError(RuntimeError):
    pass


class DriveTransient(DriveError):
    pass


class DriveRateLimited(DriveTransient):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class DrivePermanent(DriveError):
    pass


def _parse_retry_after(stderr: str, message: str) -> float | None:
    m = re.search(r"Retry-After:\s*(\d+)", stderr, re.IGNORECASE) or re.search(
        r"retry.?after['\"]?\s*[:=]\s*(\d+)", message, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _classify_drive_error(args: tuple[str, ...], returncode: int, stderr: str) -> DriveError:
    text = f"{' '.join(args)} {stderr}"
    if DRIVE_RATE_LIMIT.search(text) or DRIVE_QUOTA.search(text) or '"code": 429' in text or '429' in stderr:
        return DriveRateLimited(f"rclone {args[0]} failed: {stderr[-300:]}", _parse_retry_after(stderr, text))
    if returncode in {5, 6, 7, 8} or "timeout" in text.lower() or "temporar" in text.lower():
        return DriveTransient(f"rclone {args[0]} failed: {stderr[-300:]}")
    return DrivePermanent(f"rclone {args[0]} failed: {stderr[-300:]}")


def safe_parts(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("/"))
    if not parts or any(p in {"", ".", ".."} or "\\" in p or "\x00" in p
                        for p in parts):
        raise ValueError(f"unsafe Drive path: {path!r}")
    return parts


@dataclass(frozen=True)
class MediaFile:
    project: str
    path: str
    file_id: str
    size: int
    modified: str
    md5: str | None
    marker: bool

    @property
    def revision(self) -> str:
        return hashlib.sha256(f"{self.file_id}\0{self.size}\0{self.modified}".encode()).hexdigest()[:20]


class Drive:
    def __init__(self, remote: str, config: str | None = None, timeout: int = 180):
        if not re.fullmatch(r"[\w-]+:[^\x00]*", remote):
            raise ValueError("remote must be an rclone path such as gdrive:Hub")
        self.remote = remote.rstrip("/")
        self.config = config
        self.timeout = timeout
        self._last_call = 0.0
        self._min_interval = float(os.environ.get("DRIVE_MIN_INTERVAL", "0.2"))
        self._max_retries = int(os.environ.get("DRIVE_MAX_RETRIES", "6"))
        self._base_backoff = float(os.environ.get("DRIVE_BASE_BACKOFF", "2.0"))
        self._max_backoff = float(os.environ.get("DRIVE_MAX_BACKOFF", "120.0"))

    def path(self, *parts: str) -> str:
        return "/".join((self.remote, *(p for part in parts for p in safe_parts(part))))

    def _pace(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def call(self, *args: str) -> bytes:
        cmd = ["rclone", *args]
        if self.config:
            cmd.extend(["--config", self.config])
        last_exc: DriveError | None = None
        for attempt in range(self._max_retries):
            self._pace()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                last_exc = DriveTransient(f"Drive request timed out ({self.timeout}s)")
                sleep_for = min(self._max_backoff, self._base_backoff * (2 ** attempt) * (0.8 + random.random() * 0.4))
                time.sleep(sleep_for)
                continue
            if not result.returncode:
                return result.stdout
            stderr = result.stderr.decode('utf-8', errors='replace')
            err = _classify_drive_error(args, result.returncode, stderr)
            last_exc = err
            if isinstance(err, DrivePermanent):
                raise err
            sleep_for = err.retry_after if isinstance(err, DriveRateLimited) and err.retry_after else None
            if sleep_for is None:
                sleep_for = min(self._max_backoff, self._base_backoff * (2 ** attempt) * (0.8 + random.random() * 0.4))
            time.sleep(sleep_for)
        raise last_exc or DriveTransient(f"rclone {args[0]} failed after retries")

    def list(self, *parts: str, recursive: bool = False) -> list[dict]:
        flags = ["lsjson", self.path(*parts), "--hash"]
        if recursive:
            flags.extend(["-R", "--files-only", "--exclude", "/sessions/**",
                          "--exclude", "/profiles/**"])
        return json.loads(self.call(*flags))

    def state(self, project: str, path: str) -> dict:
        try:
            result = json.loads(self.call("cat", self.path(project, path + ".state.json")))
            if not isinstance(result, dict):
                raise ValueError("not a JSON object")
            return result
        except (DriveError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {"status": "unknown", "detail": type(exc).__name__}

    def states_bulk(self, project: str, target: pathlib.Path) -> None:
        """Synchronize markers in one Drive traversal instead of one rclone per file."""
        target.mkdir(parents=True, exist_ok=True)
        self.call("copy", self.path(project), str(target),
                  "--filter", "- /sessions/**", "--filter", "- /profiles/**",
                  "--filter", "+ *.state.json", "--filter", "+ **/*.state.json",
                  "--filter", "- **")


def projects(root: list[dict]) -> list[str]:
    # A completed project is still a project: never filter on activity or age.
    # Hub/_inbox is a real shared intake scanned by the watcher, not a service dir.
    return sorted((x["Name"] for x in root if x.get("IsDir") and
                   (x["Name"] == "_inbox" or
                    not x["Name"].startswith(("_", ".")))), key=str.casefold)


def scannable(project: str, path: str) -> bool:
    """Mirror project-inboxes discovery: its named inbox is an explicit scan root."""
    folders = safe_parts(path)[:-1]
    if folders and folders[0] == f"_{project}_inbox":
        folders = folders[1:]
    return not any(p in SKIP_DIRS or p.startswith(("_", ".")) for p in folders)


def media_inventory(project: str, entries: list[dict]) -> list[MediaFile]:
    collisions = collections.Counter(e.get("Path", "") for e in entries if not e.get("IsDir"))
    ambiguous = [p for p, n in collisions.items() if n > 1 and
                 scannable(project, p) and
                 (pathlib.PurePosixPath(p).suffix.lower() in MEDIA or
                  p.endswith(".state.json"))]
    if ambiguous:
        raise ValueError(f"duplicate Drive paths cannot be resolved safely: {ambiguous[:4]}")
    paths = {e["Path"] for e in entries if not e.get("IsDir")}
    found = []
    for entry in entries:
        path = entry.get("Path", "")
        if entry.get("IsDir") or pathlib.PurePosixPath(path).suffix.lower() not in MEDIA:
            continue
        safe_parts(path)
        if not scannable(project, path):
            continue
        if not entry.get("ID") or int(entry.get("Size", -1)) < 0:
            raise ValueError(f"missing Drive ID or size: {project}/{path}")
        hashes = entry.get("Hashes") or {}
        safe_parts(entry["ID"])
        found.append(MediaFile(project, path, entry["ID"], int(entry["Size"]),
                               entry.get("ModTime", ""), hashes.get("md5") or hashes.get("MD5"),
                               path + ".state.json" in paths))
    return sorted(found, key=lambda f: f.modified, reverse=True)


def marked_vtt_inventory(project: str, entries: list[dict]) -> list[MediaFile]:
    """Display processed standalone VTTs; never treat media sidecars as jobs."""
    paths = {entry.get("Path", "") for entry in entries if not entry.get("IsDir")}
    media_paths = [pathlib.PurePosixPath(p) for p in paths
                   if pathlib.PurePosixPath(p).suffix.lower() in MEDIA]
    found: list[MediaFile] = []
    for entry in entries:
        path = entry.get("Path", "")
        vtt = pathlib.PurePosixPath(path)
        if (entry.get("IsDir") or vtt.suffix.lower() != ".vtt" or
                path + ".state.json" not in paths or not scannable(project, path) or
                vtt.name.endswith("-segments.vtt")):
            continue
        stamp = ZOOM_TIMESTAMP.search(vtt.name)
        if any(other.parent == vtt.parent and (other.stem == vtt.stem or
               stamp and (match := ZOOM_TIMESTAMP.search(other.name)) and
               match.group(1) == stamp.group(1)) for other in media_paths):
            continue
        hashes = entry.get("Hashes") or {}
        found.append(MediaFile(project, path, entry.get("ID", ""), int(entry.get("Size", 0)),
                               entry.get("ModTime", ""), hashes.get("md5"), True))
    return found


def hygiene_candidates(project: str, entries: list[dict], media: list[MediaFile]) -> list[dict]:
    """Inventory suggestions only; never infer that a file is safe to delete."""
    suggestions: list[dict] = []
    sources = {item.path for item in media}
    by_digest: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    for item in media:
        if item.md5:
            by_digest[(item.md5.lower(), item.size)].append(item.path)
        if item.size == 0:
            suggestions.append({"reason": "empty-media", "path": item.path})
    for paths in by_digest.values():
        if len(paths) > 1:
            suggestions.append({"reason": "same-content-within-project", "paths": paths})
    for entry in entries:
        path = entry.get("Path", "")
        if path.endswith(".state.json") and path[:-11] not in sources:
            if scannable(project, path):
                suggestions.append({"reason": "orphan-state-marker", "path": path})
    return suggestions


def partition_entries(project: str, entries: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Separate ambiguous Drive paths while leaving unrelated inputs available."""
    by_path: dict[str, list[dict]] = collections.defaultdict(list)
    for entry in entries:
        if not entry.get("IsDir"):
            by_path[entry.get("Path", "")].append(entry)
    duplicated = {path: group for path, group in by_path.items() if len(group) > 1}
    affected = {path for path in duplicated if scannable(project, path) and
                pathlib.PurePosixPath(path).suffix.lower() in MEDIA | {".vtt"}}
    affected.update(path[:-11] for path in duplicated if path.endswith(".state.json")
                    and scannable(project, path))
    unambiguous = [entry for entry in entries if entry.get("Path") not in duplicated and
                   entry.get("Path") not in affected and
                   not (entry.get("Path", "").endswith(".state.json") and
                        entry["Path"][:-11] in affected)]
    return unambiguous, {path: group for path, group in duplicated.items()
                         if path in affected or path.endswith(".state.json") and
                         path[:-11] in affected}


def stage(drive: Drive, item: MediaFile, local_root: pathlib.Path) -> pathlib.Path:
    """Download one immutable revision, verify it, then atomically expose it locally.

    This does NOT claim or submit the file for ASR. Never overwrite a successful
    receipt; two runs of the same revision are idempotent.
    """
    safe_parts(item.project)
    parts = safe_parts(item.path)
    if not item.md5 or not re.fullmatch(r"[a-fA-F0-9]{32}", item.md5):
        raise ValueError("Drive MD5 unavailable; refusing an unverified copy")
    dest_dir = local_root / item.project / item.file_id / item.revision
    target = dest_dir / parts[-1]
    receipt = dest_dir / "delivery.json"
    if receipt.is_file():
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if (saved.get("id"), saved.get("revision"), saved.get("md5")) == (
                item.file_id, item.revision, item.md5.lower()) and target.is_file():
            digest = hashlib.md5()  # nosec: integrity check against Drive's MD5
            with target.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if target.stat().st_size == item.size and digest.hexdigest() == item.md5.lower():
                return target
            raise ValueError(f"existing local receipt is corrupt: {target}")
    if target.exists():
        raise ValueError(f"refusing to overwrite unreceipted local file: {target}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=dest_dir)
    os.close(fd)
    tmp = pathlib.Path(temp_name)
    try:
        drive.call("copyto", drive.path(item.project, item.path), str(tmp))
        digest = hashlib.md5()  # nosec: Drive supplies MD5 for transfer integrity, not security
        with tmp.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if tmp.stat().st_size != item.size or digest.hexdigest() != item.md5.lower():
            raise ValueError(f"size or Drive checksum mismatch for {item.project}/{item.path}")
        os.replace(tmp, target)
        payload = {"id": item.file_id, "revision": item.revision,
                   "md5": item.md5.lower(), "size": item.size,
                   "source": f"{item.project}/{item.path}",
                   "delivered_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=dest_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False)
        try:
            os.replace(name, receipt)
        finally:
            pathlib.Path(name).unlink(missing_ok=True)
        return target
    finally:
        tmp.unlink(missing_ok=True)


def render(project: str, rows: list[dict], *, checked_at: str,
           complete: bool, error: str = "") -> str:
    counts = collections.Counter(row["status"] for row in rows)
    banner = ("Обход завершён" if complete else
              "НЕПОЛНЫЙ ОБХОД: отсутствие записи не означает, что очередь пуста")
    summary = " · ".join(f"{html.escape(status)}: {count}"
                         for status, count in sorted(counts.items()))
    body = []
    for row in rows:
        item = row["file"]
        link = f"https://drive.google.com/file/d/{quote(item.file_id, safe='')}/view"
        body.append("<tr><td><a href=\"" + html.escape(link, quote=True) + "\">" +
                    html.escape(item.path) + "</a></td><td>" +
                    html.escape(row["status"]) + "</td><td>" +
                    html.escape(row.get("updated", "")) + "</td><td>" +
                    ("<a href=\"" + html.escape(row["result_url"], quote=True) +
                     "\">Открыть транскрипт</a>" if row.get("result_url", "").startswith(
                         "https://drive.google.com/file/d/") else
                     html.escape(row.get("result", ""))) + "</td></tr>")
    return ("<!doctype html><html lang=\"ru\"><meta charset=\"utf-8\">" + GENERATED +
            "<meta name=\"artifact-version\" content=\"" + PAGE_VERSION + "\">" +
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
            "<title>Обработка — " + html.escape(project) + "</title>" +
            "<style>body{font:16px system-ui;max-width:75rem;margin:2rem auto;padding:0 1rem}" +
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;" +
            "padding:.6rem;text-align:left}tr:nth-child(even){background:#f6f6f6}" +
            ".warn{background:#ffe8ce;padding:1rem}</style><h1>" + html.escape(project) +
            "</h1><p class=\"" + ("ok" if complete else "warn") + "\">" +
            banner + "</p><p>Проверено: " + html.escape(checked_at) + "</p><p>" +
            html.escape(error) + "</p><p>" + summary + "</p><table><thead><tr>" +
            "<th>Запись</th><th>Состояние</th><th>Обновлено</th><th>Результат</th>" +
            "</tr></thead><tbody>" + "".join(body) + "</tbody></table></html>")


def _hash_matches(path: pathlib.Path, expected: str | None) -> bool:
    if not expected:
        return False
    try:
        digest = hashlib.md5()  # nosec: compare with Drive-provided MD5
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest().lower() == expected.lower()
    except OSError:
        return False


def snapshot(drive: Drive, project: str, *, state_cache: pathlib.Path | None = None
             ) -> tuple[str, dict]:
    entries = drive.list(project, recursive=True)  # an error must not become "empty"
    unambiguous, duplicated = partition_entries(project, entries)
    affected = {path[:-11] if path.endswith(".state.json") else path for path in duplicated}
    media = media_inventory(project, unambiguous) + marked_vtt_inventory(project, unambiguous)
    markers = {e["Path"]: e for e in unambiguous
               if e.get("Path", "").endswith(".state.json")}
    if state_cache is not None and any(item.marker for item in media):
        drive.states_bulk(project, state_cache)

    def row(item: MediaFile) -> dict:
        state = {"status": "new"}
        if item.marker and state_cache is None:
            try:
                state = drive.state(project, item.path)
            except DriveRateLimited as exc:
                state = {"status": "unknown", "detail": "drive-rate-limited"}
            except DriveTransient:
                state = {"status": "unknown", "detail": "drive-transient"}
        elif item.marker:
            cached = state_cache.joinpath(*safe_parts(item.path + ".state.json"))
            marker = markers.get(item.path + ".state.json")
            expected = (marker.get("Hashes") or {}).get("md5") if marker else None
            try:
                if cached.is_file() and _hash_matches(cached, expected):
                    raw = cached.read_bytes()
                    state = json.loads(raw)
                    if not isinstance(state, dict):
                        raise ValueError("marker is not a JSON object")
                elif marker and cached.parent.exists():
                    # Prefer cached copy synced by states_bulk; do not cat per item
                    raw = cached.read_bytes()
                    if expected and hashlib.md5(raw).hexdigest() != expected.lower():
                        raise ValueError("marker does not match Drive metadata")
                    state = json.loads(raw)
                    if not isinstance(state, dict):
                        raise ValueError("marker is not a JSON object")
                else:
                    state = {"status": "unknown"}
            except (OSError, ValueError):
                state = {"status": "unknown"}
        status = str(state.get("status") or "unknown")
        # Old asr-done sidecars do not prove that the output still exists in Drive.
        # Never present an unverified legacy marker as a verified publication.
        if status == "asr-done" and state.get("publication_verified") is not True:
            status = "asr-done (публикация не проверена)"
        return {"file": item, "status": status,
                "updated": str(state.get("finished_at") or state.get("updated_at") or
                               state.get("started_at") or ""),
                "result": str(state.get("session_id") or ""),
                "result_url": str(state.get("transcript_url") or "")}
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(row, media))
    for entry in entries:
        path = entry.get("Path", "")
        if (entry.get("IsDir") or path not in affected or
                pathlib.PurePosixPath(path).suffix.lower() not in MEDIA | {".vtt"}):
            continue
        rows.append({"file": MediaFile(project, path, entry.get("ID", ""),
                                       int(entry.get("Size", 0)), entry.get("ModTime", ""),
                                       None, False),
                     "status": "ambiguous-duplicate", "updated": "", "result": ""})
    complete = not affected and all(row["status"] != "unknown" for row in rows)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    page = render(project, rows, checked_at=now, complete=complete)
    report = {"project": project, "checked_at": now, "complete": complete,
              "counts": dict(collections.Counter(row["status"] for row in rows)),
              "hygiene_candidates": hygiene_candidates(project, unambiguous, media),
              "policy": "report-only; no Hub deletion"}
    report["hygiene_candidates"].extend(
        {"reason": "failed-or-unreadable-state", "path": row["file"].path}
        for row in rows if row["status"] in {"failed", "unknown"})
    report["hygiene_candidates"].extend(
        {"reason": "duplicate-drive-path", "path": path,
         "ids": [e.get("ID") for e in duplicated[path]]}
        for path in sorted(duplicated))
    return page, report


def publish_page(drive: Drive, project: str, local: pathlib.Path) -> None:
    try:
        existing = {x["Name"] for x in drive.list(project)}
    except DriveError:
        existing = set()
    if "index.html" in existing:
        try:
            if GENERATED.encode() not in (drive.call("cat", drive.path(project, "index.html"))[:512]):
                raise RuntimeError("refusing to overwrite existing unmanaged index.html")
        except DriveRateLimited:
            raise
        except DriveError:
            # If we cannot read existing page due to transient API issues, require explicit safety.
            raise RuntimeError("cannot verify existing index.html due to Drive API issue")
    drive.call("copyto", str(local), drive.path(project, "index.html"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default="gdrive:Hub")
    parser.add_argument("--rclone-config")
    parser.add_argument("--project", help="one project, otherwise inventory every project")
    parser.add_argument("--after", help="resume whole-Hub run after this project name")
    parser.add_argument("--through", help="stop whole-Hub run at this project name")
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--publish", action="store_true", help="publish generated index.html to Drive")
    parser.add_argument("--stage", help="stage exactly project/relative/media, without running ASR")
    args = parser.parse_args()
    drive = Drive(args.remote, args.rclone_config)
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(args.output_dir, 0o700)  # state cache contains private filenames
    if args.stage:
        project, sep, rel = args.stage.partition("/")
        if not sep:
            parser.error("--stage needs project/relative/media")
        item = next((x for x in media_inventory(project, drive.list(project, recursive=True))
                     if x.path == rel), None)
        if item is None:
            parser.error("media not found in Drive inventory")
        if item.marker and drive.state(project, rel).get("status") == "asr-done":
            parser.error("already marked asr-done; refusing to stage for duplicate processing")
        print(stage(drive, item, args.output_dir / "intake"))
        return 0
    selected = [args.project] if args.project else projects(drive.list())
    if args.after:
        selected = [project for project in selected if project.casefold() > args.after.casefold()]
    if args.through:
        selected = [project for project in selected if project.casefold() <= args.through.casefold()]
    report_path = args.output_dir / "hub-hygiene.json"
    try:
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        reports = {x["project"]: x for x in previous if isinstance(x, dict) and "project" in x}
    except FileNotFoundError:
        reports = {}
    except (ValueError, OSError) as exc:
        parser.error(f"cannot read existing hygiene report (not overwriting): {exc}")
    current = []

    def save(report: dict) -> None:
        reports[report["project"]] = report
        current.append(report)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".hub-hygiene-", dir=args.output_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump([reports[key] for key in sorted(reports, key=str.casefold)], out,
                      ensure_ascii=False, indent=2)
        os.replace(temp, report_path)

    for project in selected:
        safe_parts(project)
        local = args.output_dir / project / "index.html"
        try:
            page, report = snapshot(drive, project,
                                    state_cache=args.output_dir / ".state-cache" / project)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(page, encoding="utf-8")
            if args.publish:
                publish_page(drive, project, local)
            save(report)
            print(f"{project}: {report['counts']} (complete={report['complete']})")
        except (DriveRateLimited, DriveTransient) as exc:
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            save({"project": project, "complete": False, "error": type(exc).__name__,
                  "retry_after": getattr(exc, "retry_after", None)})
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(render(project, [], checked_at=now, complete=False,
                                    error="Drive API временно ограничен; готовность определяется по журналам/сессиям"), encoding="utf-8")
            if args.publish:
                try:
                    publish_page(drive, project, local)
                except (DriveError, RuntimeError, OSError):
                    pass
            print(f"{project}: INCOMPLETE (drive-transient): {exc}")
        except (DrivePermanent, RuntimeError, ValueError, OSError) as exc:
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            save({"project": project, "complete": False, "error": str(exc)})
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(render(project, [], checked_at=now, complete=False,
                                    error=str(exc)), encoding="utf-8")
            # A failed scan must never masquerade as an empty healthy queue. Try to
            # publish a visible warning; if Drive itself is down the local report
            # remains and the command exits nonzero for external alerting.
            if args.publish:
                try:
                    publish_page(drive, project, local)
                except (DriveError, RuntimeError, OSError):
                    pass
            print(f"{project}: INCOMPLETE: {exc}")
    return 0 if all(x.get("complete") for x in current) else 1


if __name__ == "__main__":
    raise SystemExit(main())
