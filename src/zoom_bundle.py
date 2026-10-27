"""Read-only integrity validation and conservative planning of READY bundles.

No NAS mount management, queue transitions, ASR, delivery or source mutations.
The caller must separately verify its configured mount identity before use.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import stat
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from typing import Callable

AUDIO = {".m4a", ".mp3", ".wav", ".oga"}
VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
VIDEO_ROLES = {"active_speaker", "gallery_view", "shared_screen",
               "shared_screen_with_speaker_view", "shared_screen_with_gallery_view"}
HASH = re.compile(r"[0-9a-fA-F]{64}")


class BundleError(ValueError):
    pass


@dataclass
class BundleReport:
    status: str = "held"
    integrity_verified: bool = False
    revision: str | None = None
    reasons: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    plan: dict = field(default_factory=dict)


def _json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("duplicate_json_key")
        result[key] = value
    return result


def _signature(path: pathlib.Path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or
            getattr(info, "st_file_attributes", 0) & 0x400):  # Windows reparse point
        raise BundleError("symlink_or_reparse_point")
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _safe_relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value or
            any(ord(c) < 32 for c in value)):
        raise BundleError("unsafe_member_path")
    parts = value.split("/")
    if any(p in {"", ".", ".."} or p.rstrip(". ") != p for p in parts):
        raise BundleError("unsafe_member_path")
    if any(p.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL",
            *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for p in parts):
        raise BundleError("unsafe_member_path")
    return value


def _tree(root: pathlib.Path):
    result = {}
    names = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = pathlib.Path(directory)
        for name in [*dirs, *files]:
            path = parent / name
            signature = _signature(path)
            rel = path.relative_to(root).as_posix()
            _safe_relative(rel)
            normal = unicodedata.normalize("NFC", rel).casefold()
            if normal in names:
                raise BundleError("ambiguous_normalized_path")
            names.add(normal)
            if name in dirs and name.endswith(".asr"):
                dirs.remove(name)  # generated output never participates in revision
                continue
            if not (stat.S_ISREG(signature[2]) or stat.S_ISDIR(signature[2])):
                raise BundleError("non_regular_member")
            if name in dirs and name not in {"files", "Audio Record"}:
                raise BundleError("unsupported_directory")
            if name in dirs and parent != root:
                raise BundleError("nested_bundle_directory")
            result[rel] = signature
            if len(result) > 20000:
                raise BundleError("too_many_bundle_members")
    return result


@contextmanager
def _open_read(path: pathlib.Path):
    """On POSIX, open every directory without following links (not just the leaf)."""
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) |
             getattr(os, "O_NONBLOCK", 0))
    parent_fd = None
    fd = None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
            path = path.absolute()
            directory_flags = flags | os.O_DIRECTORY
            parent_fd = os.open(path.anchor, directory_flags)
            for part in path.parts[1:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(path.name, flags, dir_fd=parent_fd)
        else:
            for ancestor in (path, *path.parents):
                _signature(ancestor)
            fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError("non_regular_member")
        stream = os.fdopen(fd, "rb")
        fd = None  # stream owns descriptor
        with stream:
            yield stream
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _bytes(path: pathlib.Path, maximum: int):
    with _open_read(path) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise BundleError("control_file_too_large")
    return raw


def _sha(path: pathlib.Path):
    h = hashlib.sha256()
    with _open_read(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _timestamp(value):
    if not isinstance(value, str):
        raise BundleError("timeline_unresolved")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BundleError("timeline_unresolved") from exc
    if parsed.tzinfo is None or parsed.year < 1970:
        raise BundleError("timeline_unresolved")
    return parsed.astimezone(dt.timezone.utc)


def probe_media(path: pathlib.Path) -> dict:
    """Bounded ffprobe; never infer an audio stream from a file extension."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_streams", "-show_format",
         "-of", "json", str(path)], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise BundleError("media_probe_failed")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
        raise BundleError("media_probe_failed")
    if any(not isinstance(s, dict) for s in data["streams"]):
        raise BundleError("media_probe_failed")
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if len(audio) != 1:
        raise BundleError("missing_or_ambiguous_audio_stream")
    container = data.get("format", {})
    if not isinstance(container, dict):
        raise BundleError("media_probe_failed")
    duration = audio[0].get("duration") or container.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise BundleError("media_probe_failed") from exc
    return {"duration": duration, "audio_streams": len(audio)}


def _role(rel):
    path = pathlib.PurePosixPath(rel)
    if path.suffix.lower() == ".zoom":
        raise BundleError("conversion_required")
    if path.suffix.lower() in AUDIO:
        return "participant_audio" if path.parts[0] == "Audio Record" else "audio_only"
    if path.suffix.lower() in VIDEO:
        return "video_unclassified"
    return "companion"


def _plan(files: list[dict], probe: Callable, root: pathlib.Path):
    media = [f for f in files if pathlib.PurePosixPath(f["path"]).suffix.lower() in AUDIO | VIDEO]
    if not media:
        raise BundleError("no_media")
    participants = [f for f in media if f["role"] == "participant_audio"]
    mixed = [f for f in media if f["role"] == "audio_only"]
    videos = [f for f in media if f["role"] in VIDEO_ROLES | {"video_unclassified"}]
    chosen = participants or mixed or videos
    if not chosen:
        raise BundleError("no_original_audio_candidate")
    mode = "participant_tracks" if participants else "mixed_audio" if mixed else "video_fallback"
    plan = {"mode": mode, "units": [], "speaker_identity_verified": False}
    reasons = []
    # Require trustworthy, explicit intervals even for one media source. A cloud
    # wall-clock start is NOT itself proof of internal track alignment.
    intervals = []
    for file in chosen:
        try:
            start, end = _timestamp(file.get("recording_start")), _timestamp(file.get("recording_end"))
            if end <= start:
                raise BundleError("timeline_unresolved")
            intervals.append((start, end, file))
        except BundleError as exc:
            reasons.append(str(exc))
    if reasons:
        return plan, sorted(set(reasons))
    intervals.sort(key=lambda row: (row[0], row[1], row[2]["id"]))
    origin = min(row[0] for row in intervals)
    previous_end = None
    for start, end, file in intervals:
        if not participants and previous_end is not None and start < previous_end:
            reasons.append("ambiguous_overlapping_media")
        if not participants and previous_end is not None and start > previous_end:
            reasons.append("recording_gap_needs_review")
        previous_end = max(previous_end, end) if previous_end else end
        try:
            info = probe(root / file["path"])
            if not isinstance(info, dict):
                raise BundleError("media_probe_failed")
            duration = info.get("duration")
            if (info.get("audio_streams") != 1 or isinstance(duration, bool) or
                    not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0):
                raise BundleError("missing_or_ambiguous_audio_stream")
            if abs(duration - (end - start).total_seconds()) > 1.0:
                raise BundleError("media_duration_mismatch")
        except (BundleError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(str(exc) if isinstance(exc, BundleError) else "media_probe_failed")
        plan["units"].append({"id": file["id"], "path": file["path"],
                              "offset_seconds": (start - origin).total_seconds(),
                              "speaker_source": "track" if participants else "profile"})
    plan["timeline_origin"] = origin.isoformat()
    if not participants:
        # A mixed track for the first segment must not hide a later video-only
        # segment. Every known original-media interval needs selected coverage.
        for file in mixed + videos:
            try:
                start, end = _timestamp(file.get("recording_start")), _timestamp(file.get("recording_end"))
                if end <= start:
                    raise BundleError("timeline_unresolved")
                cursor = start
                for left, right, _ in intervals:
                    if left > cursor:
                        break
                    if right > cursor:
                        cursor = right
                if cursor < end:
                    reasons.append("selected_media_incomplete")
            except BundleError:
                reasons.append("timeline_unresolved")
    if participants:
        # The v1 exporter has neither track-coverage evidence nor participant
        # identity/phone-group metadata. Do not invent these from a filename.
        reasons.extend(["participant_coverage_unverified", "track_alignment_unverified"])
    return plan, sorted(set(reasons))


def validate_bundle(directory: pathlib.Path, *, allowed_root: pathlib.Path,
                    probe: Callable = probe_media) -> BundleReport:
    report = BundleReport()
    try:
        # Reject links/reparse points in the complete path, including root itself.
        for path in (directory.absolute(), *directory.absolute().parents):
            _signature(path)
        root = directory.resolve(strict=True)
        allowed = allowed_root.resolve(strict=True)
        if not root.is_dir() or not root.is_relative_to(allowed):
            raise BundleError("bundle_outside_allowed_root")
        before = _tree(root)
        if ("READY" not in before or not stat.S_ISREG(before["READY"][2]) or
                before["READY"][3] != 0):
            raise BundleError("missing_or_invalid_ready")
        manifest = None
        if "bundle.json" in before:
            manifest = json.loads(_bytes(root / "bundle.json", 4 * 1024 * 1024),
                                  object_pairs_hook=_json)
            if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or
                    manifest["schema_version"] != 1 or not isinstance(manifest.get("meeting_uuid"), str) or
                    not manifest["meeting_uuid"] or manifest.get("completeness") != "observed_api_inventory"):
                raise BundleError("unsupported_manifest")
            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries or len(entries) > 10000:
                raise BundleError("invalid_manifest_inventory")
        else:
            entries = [{"path": rel, "id": rel, "role": _role(rel), "size": sig[3]}
                       for rel, sig in before.items() if stat.S_ISREG(sig[2]) and rel != "READY"]
            if not entries:
                raise BundleError("empty_bundle")
        ids, paths = set(), set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise BundleError("invalid_manifest_member")
            rel = _safe_relative(entry.get("path"))
            identity, role, size = entry.get("id"), entry.get("role"), entry.get("size")
            if (not isinstance(identity, str) or not identity or identity in ids or rel in paths or
                    not isinstance(role, str) or not role or type(size) is not int or size < 0 or
                    rel in {"READY", "bundle.json"}):
                raise BundleError("invalid_or_duplicate_member")
            ids.add(identity)
            paths.add(rel)
            if rel not in before or not stat.S_ISREG(before[rel][2]):
                raise BundleError("missing_member")
            if before[rel][3] != size:
                raise BundleError("member_size_mismatch")
            digest = _sha(root / rel)
            if manifest is not None:
                expected = entry.get("sha256")
                if not isinstance(expected, str) or not HASH.fullmatch(expected) or digest != expected.lower():
                    raise BundleError("member_checksum_mismatch")
                if entry.get("participant_audio") is not (role == "participant_audio"):
                    raise BundleError("inconsistent_participant_role")
            _role(rel)  # detect unconverted .zoom regardless of claimed role
            suffix = pathlib.PurePosixPath(rel).suffix.lower()
            if ((role in {"audio_only", "participant_audio", "audio_interpretation"} and suffix not in AUDIO) or
                    (role in VIDEO_ROLES and suffix not in VIDEO)):
                raise BundleError("role_media_type_mismatch")
            report.files.append({"id": identity, "path": rel, "role": role, "size": size,
                                 "sha256": digest, "recording_start": entry.get("recording_start"),
                                 "recording_end": entry.get("recording_end")})
        actual = {rel for rel, sig in before.items() if stat.S_ISREG(sig[2])} - {"READY", "bundle.json"}
        if actual != paths:
            raise BundleError("unlisted_bundle_members")
        report.files.sort(key=lambda f: f["path"])
        revision = json.dumps(report.files, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        report.revision = hashlib.sha256(revision.encode()).hexdigest()
        try:
            report.plan, report.reasons = _plan(report.files, probe, root)
        except BundleError as exc:
            report.reasons.append(str(exc))
        if _tree(root) != before:
            raise BundleError("bundle_changed_during_validation")
        report.integrity_verified = True
        report.status = "validated" if not report.reasons else "held"
    except (BundleError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        report.reasons = sorted(set(report.reasons + [str(exc) if isinstance(exc, BundleError)
                                                    else "bundle_unreadable_or_invalid"]))
        report.status = "held"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--allowed-root", required=True, type=pathlib.Path)
    args = parser.parse_args()
    report = validate_bundle(args.directory, allowed_root=args.allowed_root)
    print(json.dumps(asdict(report), ensure_ascii=False))
    return 0 if report.status == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
