#!/usr/bin/env python3
"""Audio Inbox watcher — generic, hub-oriented, cross-platform.

Scans the source folders declared in the node config, manages a per-file state
machine via a sidecar JSON, and dispatches ASR through ``media_transcribe_cli``.
Outputs (transcripts + state) are written to declarative ``outputs`` templates
with runtime placeholders (``{hub_root}`` / ``{pid}`` / ``{sid}`` / ``{YYYY-MM}``
/ ``{cache_root}``) so the repository never carries personal absolute paths.

State machine (per source file, sidecar ``<file>.state.json``):

    queued  -> in-progress -> asr-done       (success path)
    queued  -> in-progress -> queued (retry) (transient failure, attempts < max)
    queued  -> in-progress -> failed         (attempts >= max -> moved to _failed/)

Marker: ``<file>.processed-asr`` (zero-byte, redundant signal for downstream code).

Multi-node coordination is opt-in (``enable_multi_machine``): a per-file
``<file>.claim.json`` in the shared source uses claim-and-verify + lease +
heartbeat instead of a fragile filesystem lock (resilient to cloud-drive
eventual consistency).

Design: run on a timer (Windows Task Scheduler / launchd / cron). A host-local
lock makes concurrent invocations on the same machine safe.

The engine is vault-agnostic. Writing an Obsidian session card is an *optional*
output adapter (``outputs.session_card.adapter``); the default ``none`` keeps the
core fully generic.

Usage::

    python audio_inbox_watch.py --config config/node.local.json --once
    python audio_inbox_watch.py --config config/node.local.json --catch-up-only
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config" / "node.local.json"

# Default watcher knobs — overridable from the node config.
DEFAULT_SCAN_EXTENSIONS = [".m4a", ".mp3", ".wav", ".oga", ".mp4", ".mov", ".webm", ".m4v"]
DEFAULT_SKIP_FOLDERS = ["_failed", "_processed", "_archive", "sessions", "Audio Record",
                        "Transcripts", "profiles", "Speakers", "recordings", "pipeline"]
DEFAULT_SKIP_FOLDER_PREFIXES = ["_", "."]
DEFAULT_SKIP_FILENAME_SUFFIXES = [
    "-transcript.md", "-transcript.txt", "_original.txt",
    "-segments.vtt", "-segments.jsonl", "-raw.json", "-run-meta.json",
]
DEFAULT_SKIP_FILENAME_PATTERNS = [r"^_PROJECT-.*", r"^_README.*"]

# Cyrillic -> Latin transliteration for slug generation (filename -> ASCII slug).
_CYR_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".oga"}
PRIMARY_NAME_PATTERN = re.compile(r".*Recording.*\.m4a$", re.IGNORECASE)
BUNDLE_TS_PATTERN = re.compile(r"(?:GMT)?(\d{8}[-_]\d{6})")


# ---------------------------------------------------------------------------
# Logging / time / atomic IO
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] {msg}\n")
    sys.stderr.flush()


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utcnow_iso() -> str:
    """UTC ISO timestamp with ``Z`` suffix (cross-machine claim coordination)."""
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except Exception:
        return None


def atomic_write_json(path: pathlib.Path, data: dict) -> None:
    """Atomic write: tmp -> fsync -> os.replace. Retries os.replace on EDEADLK.

    Cloud-drive FUSE backends (e.g. GoogleDriveFS on macOS) occasionally raise
    ``OSError errno=11 (EDEADLK)`` on os.replace when mid-sync. A single shot then
    loses state.json. Linear backoff 2/4/6s, 3 attempts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        max_retries, base_sleep = 3, 2.0
        for attempt in range(max_retries):
            try:
                os.replace(tmp_name, path)
                return
            except OSError as exc:
                if exc.errno != errno.EDEADLK or attempt == max_retries - 1:
                    raise
                sleep_for = base_sleep * (attempt + 1)
                log(f"atomic_write EDEADLK on {path.name} attempt {attempt + 1}/{max_retries}; "
                    f"retry in {sleep_for:.0f}s")
                time.sleep(sleep_for)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _copy_with_edeadlk_retry(src: pathlib.Path, dst: pathlib.Path,
                             max_retries: int = 3, base_sleep: float = 2.0) -> None:
    """shutil.copy2 with retry-on-EDEADLK for cloud-drive sync conflicts."""
    last_exc: BaseException | None = None
    for attempt in range(max_retries):
        try:
            shutil.copy2(src, dst)
            return
        except OSError as exc:
            last_exc = exc
            if exc.errno != errno.EDEADLK or attempt == max_retries - 1:
                raise
            sleep_for = base_sleep * (attempt + 1)
            log(f"copy EDEADLK on attempt {attempt + 1}/{max_retries} ({src.name}); "
                f"retry in {sleep_for:.0f}s")
            time.sleep(sleep_for)
    if last_exc:
        raise last_exc


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Config helpers: placeholders, node fields, watcher knobs
# ---------------------------------------------------------------------------


def node_field(cfg: dict, key: str, default=None):
    """Read a field from the ``node`` block (falls back to top-level then default)."""
    node = cfg.get("node") or {}
    if key in node:
        return node[key]
    return cfg.get(key, default)


def host_label_of(cfg: dict) -> str:
    return node_field(cfg, "host_label", None) or socket.gethostname()


def resolve_template(template: str, ctx: dict) -> str:
    """Substitute ``{placeholder}`` tokens from ctx into ``template``.

    Unknown placeholders are left intact (so a partially-resolved template can be
    completed later). Backslashes from Windows paths are preserved.
    """
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        val = ctx.get(key)
        return str(val) if val is not None else m.group(0)

    # Placeholder names may contain hyphens (e.g. {YYYY-MM}, {YYYY-MM-DD}).
    return re.sub(r"\{([a-zA-Z0-9_-]+)\}", _sub, template)


def base_placeholder_ctx(cfg: dict) -> dict:
    """Placeholders that do not depend on a specific file/session."""
    return {
        "hub_root": cfg.get("hub_root", ""),
        "cache_root": node_field(cfg, "cache_root", ""),
        "local": node_field(cfg, "cache_root", ""),
    }


# ---------------------------------------------------------------------------
# Slug + SessionId
# ---------------------------------------------------------------------------


def slugify_from_filename(stem: str) -> str:
    """Convert a filename stem -> kebab-case ASCII slug.

    Transliterates Cyrillic, strips diacritics and Zoom-style timestamp tokens,
    lowercases, keeps only ``[a-z0-9]`` collapsing runs to a single ``-``.
    Returns "" if nothing usable remains (caller falls back to ``voice-note``).
    """
    s = stem or ""
    out_chars: list[str] = []
    for ch in s:
        low = ch.lower()
        if low in _CYR_TRANSLIT:
            mapped = _CYR_TRANSLIT[low]
            out_chars.append(mapped.upper() if ch.isupper() and mapped else mapped)
        else:
            out_chars.append(ch)
    s = "".join(out_chars)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"(?:GMT)?\d{8}[-_]\d{6}", "", s)
    s = re.sub(r"\d{6}[_-]\d{6}", "", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def sanitize_pid(value: str) -> str:
    """Make an arbitrary routing value safe as a path segment.

    Leading underscores are preserved on purpose — sentinel pids like ``_unrouted``
    / ``_shared`` use them as a sortable marker. Only stray edge punctuation and
    whitespace are trimmed.
    """
    v = slugify_from_filename(value) if re.search(r"[^a-zA-Z0-9_.-]", value or "") else (value or "")
    v = v.strip("-. ") or "_unrouted"
    return v


def started_at_for(audio: pathlib.Path) -> dt.datetime:
    """Best-effort recording time: file mtime (stable across re-scans)."""
    try:
        return dt.datetime.fromtimestamp(audio.stat().st_mtime)
    except OSError:
        return dt.datetime.now()


def generate_session_id(audio: pathlib.Path, started_at_dt: dt.datetime) -> str:
    """Return SessionId ``S{YYYYMMDD}T{HHMM}-{slug}`` (collision handled by caller)."""
    date_part = started_at_dt.strftime("%Y%m%d")
    time_part = started_at_dt.strftime("T%H%M")
    slug = slugify_from_filename(audio.stem) or "voice-note"
    return f"S{date_part}{time_part}-{slug}"


# ---------------------------------------------------------------------------
# Routing: source folder -> project id (pid)
# ---------------------------------------------------------------------------


def load_mapper(cfg: dict) -> dict:
    routing = cfg.get("routing") or {}
    mapper_path = routing.get("mapper")
    if not mapper_path:
        return {}
    p = pathlib.Path(resolve_template(mapper_path, base_placeholder_ctx(cfg))).expanduser()
    if not p.is_absolute():
        # resolve relative to config file dir if provided, else CWD
        cfg_dir = pathlib.Path(cfg.get("_config_dir", ".")).expanduser()
        p = (cfg_dir / p)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"mapper unreadable: {exc}")
        return {}


def parse_pid_from_name(folder_name: str, aliases: dict) -> str | None:
    """Heuristic pid from a folder name: first numeric token, else alias match."""
    for token in re.findall(r"\d+", folder_name):
        if int(token) >= 100:
            return token
    for alias, pid in (aliases or {}).items():
        if alias.lower() in folder_name.lower():
            return str(pid)
    return None


def route_pid_for_audio(audio: pathlib.Path, source_root: pathlib.Path,
                        source: dict, cfg: dict, mapper: dict) -> tuple[str, bool]:
    """Return (pid, needs_classification).

    Routing strategies (``source.route``):
      - ``"mapper"``: match the deepest relative subfolder prefix in ``mapper.folders``.
      - ``"literal"``/``"fixed"``: use ``source.project`` verbatim.
      - else / no match: derive from the top-level subfolder name (numeric token
        or alias), falling back to ``shared_name`` with needs_classification=True.
    """
    route = source.get("route", "mapper")
    if route in ("literal", "fixed") and source.get("project"):
        return sanitize_pid(str(source["project"])), False

    try:
        rel = audio.relative_to(source_root)
    except ValueError:
        rel = pathlib.Path(audio.name)
    parts = rel.parts[:-1]  # drop filename

    if route == "mapper" and mapper:
        folders = mapper.get("folders", {})
        for depth in range(len(parts), 0, -1):
            candidate = "/".join(parts[:depth])
            if candidate in folders:
                rule = folders[candidate]
                pid = rule.get("project_id") or rule.get("project")
                if pid is not None:
                    return sanitize_pid(str(pid)), False
        default_rule = mapper.get("_default", {})
        if default_rule.get("project_id"):
            return sanitize_pid(str(default_rule["project_id"])), False

    aliases = cfg.get("project_aliases", {})
    if parts:
        pid = parse_pid_from_name(parts[0], aliases)
        if pid:
            return sanitize_pid(pid), False
        return sanitize_pid(parts[0]), False

    return cfg.get("shared_name", "_shared"), True


# ---------------------------------------------------------------------------
# Scan: discover source files
# ---------------------------------------------------------------------------


def is_skipped_path(rel: pathlib.Path, skip_folders: list[str],
                    skip_prefixes: list[str]) -> bool:
    for part in rel.parts[:-1]:  # only folder components, never the filename
        if part in skip_folders:
            return True
        for prefix in skip_prefixes:
            if prefix and part.startswith(prefix) and part not in (".", ".."):
                return True
    return False


def is_output_artifact(name: str, suffixes: list[str]) -> bool:
    return any(name.endswith(suf) for suf in suffixes)


def is_marker_file(name: str, patterns: list[str]) -> bool:
    for pat in patterns or []:
        try:
            if re.match(pat, name):
                return True
        except re.error:
            continue
    return False


def _scan_one_source(root: pathlib.Path, cfg: dict, *, recursive: bool) -> list[pathlib.Path]:
    extensions = {ext.lower() for ext in cfg.get("scan_extensions", DEFAULT_SCAN_EXTENSIONS)}
    skip_folders = cfg.get("skip_folders", DEFAULT_SKIP_FOLDERS)
    skip_prefixes = cfg.get("skip_folder_prefixes", DEFAULT_SKIP_FOLDER_PREFIXES)
    skip_suffixes = cfg.get("skip_filename_suffixes", DEFAULT_SKIP_FILENAME_SUFFIXES)
    skip_patterns = cfg.get("skip_filename_patterns", DEFAULT_SKIP_FILENAME_PATTERNS)
    found: list[pathlib.Path] = []
    pattern = "**/*" if recursive else "*"
    # macOS Google Drive (File Provider) can wedge a recursive listdir on a dataless
    # subtree with NO Errno 60 and no timeout of its own (observed: a full-Hub scan
    # hung 3h at 0% CPU). The per-entry OSError guard below only helps once the OS
    # eventually *returns* an error; an indefinite hang never does. Bound the whole
    # source scan with the cloud watchdog so a wedged subtree is skipped instead of
    # freezing the sweep. Make the source available-offline to include one that keeps
    # timing out. No-op on Windows (no SIGALRM), which does not exhibit this hang.
    scan_timeout = int(cfg.get("scan_op_timeout_seconds", 90))
    try:
        with _cloud_watchdog(scan_timeout, f"scan {root.name}"):
            for path in root.glob(pattern):
                try:
                    if not path.is_file():
                        continue
                    if path.suffix.lower() not in extensions:
                        continue
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                except OSError:
                    continue  # a single denied/locked entry shouldn't drop the whole scan
                if is_skipped_path(rel, skip_folders, skip_prefixes):
                    continue
                if is_output_artifact(path.name, skip_suffixes):
                    continue
                if is_marker_file(path.name, skip_patterns):
                    continue
                found.append(path)
    except _CloudOpTimeout as exc:
        log(f"scan timed out on {root}: {exc} — dataless subtree wedged; skipping this "
            f"source (make it available-offline to include it)")
    except OSError as exc:
        log(f"scan interrupted on {root}: {exc}")
    return found


def _probe_dir(path: pathlib.Path) -> tuple[bool, str | None]:
    """Return (usable_dir, error_message). Tolerates cloud-drive access errors.

    A missing path is not an error — it may appear later — so it returns
    (False, None) silently. A ``PermissionError`` (Windows WinError 5 / macOS TCC)
    or other ``OSError`` is reported with an actionable hint instead of crashing
    the sweep.
    """
    try:
        if not path.is_dir():
            return (False, None)
    except PermissionError as exc:
        return (False,
            f"PERMISSION DENIED reading source {path}: {exc}\n"
            "    The path exists but access is denied. Common causes:\n"
            "    - cloud drive not signed in to the account that owns this folder\n"
            "    - folder shared from another account (add a shortcut to My Drive / add the shared drive)\n"
            "    - drive still initializing, or the folder is online-only and not materialized yet\n"
            "    - (macOS) the launchd process lacks Full Disk Access")
    except OSError as exc:
        return (False, f"cannot read source {path}: {exc}")
    try:
        for _ in path.iterdir():
            break  # confirm we can actually list it, not just stat it
    except OSError as exc:
        return (False, f"PERMISSION DENIED listing source {path}: {exc}")
    return (True, None)


def resolved_sources(cfg: dict) -> list[tuple[pathlib.Path, dict]]:
    """Return [(resolved_root, source_dict), ...] for the *configured* source roots
    (pre-discovery). Used for the permission preflight in run_once."""
    ctx = base_placeholder_ctx(cfg)
    out: list[tuple[pathlib.Path, dict]] = []
    for source in cfg.get("sources", []):
        root_tpl = source.get("root", "")
        root = pathlib.Path(resolve_template(root_tpl, ctx)).expanduser()
        out.append((root, source))
    return out


def _discover_project_inboxes(hub_root: pathlib.Path, source: dict, cfg: dict
                              ) -> list[tuple[pathlib.Path, dict, bool]]:
    """Expand a ``"discover": "project-inboxes"`` source into concrete scan dirs.

    Layout convention: drops live in ``{hub_root}/<pid>/_<pid>_inbox/`` (per-project
    intake) and/or directly in ``{hub_root}/<pid>/`` (top level), plus a shared
    ``{hub_root}/_inbox``. Returns ``[(scan_dir, source_for_routing, recursive), ...]``
    where pid is the ``<pid>`` folder name (injected as a literal route).
    """
    out: list[tuple[pathlib.Path, dict, bool]] = []
    inbox_name = cfg.get("hub_inbox_root_name", "_inbox")
    pattern = source.get("project_inbox_pattern", "_{pid}_inbox")
    scan_roots = source.get("scan_project_roots", True)
    skip = set(cfg.get("discover_skip_names",
                       [inbox_name, "_shared", "_meta", "_voiceprints", "_archive", "_failed"]))

    root_inbox = hub_root / inbox_name
    if _safe_is_dir_bool(root_inbox):
        out.append((root_inbox, source, True))  # shared drop -> route via mapper/folder

    try:
        children = sorted(hub_root.iterdir())
    except OSError:
        children = []
    for child in children:
        name = child.name
        if name in skip or name.startswith(".") or not _safe_is_dir_bool(child):
            continue
        literal = {**source, "route": "literal", "project": name}
        pinbox = child / pattern.replace("{pid}", name)
        if _safe_is_dir_bool(pinbox):
            out.append((pinbox, literal, True))
        if scan_roots:
            # Scan the WHOLE project folder (recursive by default) so drops land
            # anywhere inside {hub}/{pid}/ are picked up — not only the root or
            # _{pid}_inbox/. skip_folders/prefixes prune sessions, _failed, _inbox,
            # profiles, recordings, etc. Set scan_project_recursive:false for flat.
            recursive = source.get("scan_project_recursive", True)
            out.append((child, {**literal, "_flat": not recursive}, recursive))
    return out


def expand_sources(cfg: dict) -> list[tuple[pathlib.Path, dict, bool]]:
    """Resolve configured sources into concrete ``(scan_dir, source, recursive)``.

    A source with ``"discover": "project-inboxes"`` is expanded into per-project
    intake dirs; a plain source scans its ``root`` directly.
    """
    ctx = base_placeholder_ctx(cfg)
    out: list[tuple[pathlib.Path, dict, bool]] = []
    for source in cfg.get("sources", []):
        root = pathlib.Path(resolve_template(source.get("root", ""), ctx)).expanduser()
        if source.get("discover") == "project-inboxes":
            out.extend(_discover_project_inboxes(root, source, cfg))
        else:
            recursive = source.get("recursive", cfg.get("scan_recursive", True))
            out.append((root, source, recursive))
    return out


def find_audio_files(cfg: dict) -> list[tuple[pathlib.Path, pathlib.Path, dict]]:
    """Scan all sources. Return [(audio, source_root, source_dict), ...], LIFO by mtime."""
    seen: set[pathlib.Path] = set()
    results: list[tuple[pathlib.Path, pathlib.Path, dict]] = []
    for root, source, recursive in expand_sources(cfg):
        if not _safe_is_dir_bool(root):
            continue
        for path in _scan_one_source(root, cfg, recursive=recursive):
            if path in seen:
                continue
            seen.add(path)
            results.append((path, root, source))
    results.sort(key=lambda t: _safe_mtime(t[0]), reverse=True)
    return results


def _safe_mtime(p: pathlib.Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _safe_is_dir_bool(p: pathlib.Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


# A Ktalk (Kontur.Talk) download is a media file plus a transcript named after it.
# The transcript names its speakers, so when one is present the worker takes speakers
# from it and skips diarization entirely (media_transcribe.resolve_speaker_turns).
# Override via cfg.ktalk_sidecar_patterns if your export names them differently.
KTALK_SIDECAR_PATTERNS = ["Транскрипция {stem}.txt"]


def ktalk_sidecar_for(audio: pathlib.Path, cfg: dict) -> pathlib.Path | None:
    """The Ktalk transcript sitting next to ``audio``, if the export included one."""
    patterns = cfg.get("ktalk_sidecar_patterns")
    if patterns is None:
        patterns = KTALK_SIDECAR_PATTERNS
    if not patterns:
        return None
    for pattern in patterns:
        try:
            candidate = audio.parent / str(pattern).format(stem=audio.stem)
            if candidate.is_file():
                return candidate
        except (OSError, KeyError, IndexError):
            continue
    return None


def source_for_audio(audio: pathlib.Path, cfg: dict) -> tuple[pathlib.Path, dict] | None:
    """Find which configured source root is an ancestor of ``audio``."""
    for root, source in resolved_sources(cfg):
        try:
            audio.relative_to(root)
            return root, source
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Meeting-bundle detection (Zoom full-export: mixed audio + video + tracks).
# A bundle has one ASR'd primary; siblings are tracked but never transcribed.
# ---------------------------------------------------------------------------


def detect_bundle(directory: pathlib.Path,
                  candidates: list[pathlib.Path]) -> tuple[pathlib.Path | None, list[pathlib.Path], str | None]:
    if len(candidates) < 2:
        return None, [], None
    audios = sorted(p for p in candidates if p.suffix.lower() in AUDIO_EXTS)
    videos = sorted(p for p in candidates if p.suffix.lower() in VIDEO_EXTS)
    primary: pathlib.Path | None = None

    if audios:
        primary = next((p for p in audios if PRIMARY_NAME_PATTERN.match(p.name)), None)
        if primary is None and videos:
            eligible = [p for p in audios if not p.name.lower().startswith("audio_only_")]
            if eligible:
                try:
                    primary = max(eligible, key=lambda p: p.stat().st_size)
                except OSError:
                    primary = eligible[0]

    if primary is None and len(videos) >= 2:
        toks = [m.group(1) if m else None for m in (BUNDLE_TS_PATTERN.search(v.name) for v in videos)]
        unique_ts = {t for t in toks if t}
        if len(unique_ts) == 1 and all(t is not None for t in toks):
            avo = [v for v in videos if "_avo_" in v.name.lower()]
            if avo:
                primary = avo[0]
            else:
                try:
                    primary = min(videos, key=lambda p: p.stat().st_size)
                except OSError:
                    primary = videos[0]

    if primary is None:
        return None, [], None
    siblings = [p for p in audios + videos if p != primary]
    if not siblings:
        return None, [], None
    m = BUNDLE_TS_PATTERN.search(primary.name)
    bundle_id = m.group(1) if m else f"{directory.name}__{primary.stem}"
    return primary, siblings, bundle_id


def sibling_role(audio: pathlib.Path) -> str:
    return "sibling-video" if audio.suffix.lower() in VIDEO_EXTS else "sibling-audio"


def stamp_bundle_sibling(audio: pathlib.Path, bundle_id: str, primary: pathlib.Path,
                         pid: str | None, host_label: str) -> None:
    """Mark ``audio`` as a bundle sibling (tracked, never ASR'd). Idempotent."""
    sf = state_path(audio)
    if sf.exists():
        try:
            existing = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if existing.get("status") in {"asr-done", "in-progress", "failed", "bundle-sibling"}:
            changed = False
            for k, v in (("bundle_id", bundle_id), ("bundle_primary", str(primary)),
                         ("bundle_role", existing.get("bundle_role") or sibling_role(audio))):
                if existing.get(k) != v:
                    existing[k] = v
                    changed = True
            if changed:
                atomic_write_json(sf, existing)
            return
    atomic_write_json(sf, {
        "status": "bundle-sibling", "pid": pid, "attempts": 0,
        "started_at": None, "finished_at": None, "transcript_path": None,
        "last_error": None, "host": host_label, "duration_sec": None,
        "bundle_id": bundle_id, "bundle_role": sibling_role(audio),
        "bundle_primary": str(primary),
    })


def detect_bundle_for_file(audio: pathlib.Path, source_root: pathlib.Path,
                           cfg: dict) -> tuple[bool, str | None, list[pathlib.Path]]:
    extensions = {ext.lower() for ext in cfg.get("scan_extensions", DEFAULT_SCAN_EXTENSIONS)}
    skip_folders = cfg.get("skip_folders", DEFAULT_SKIP_FOLDERS)
    skip_prefixes = cfg.get("skip_folder_prefixes", DEFAULT_SKIP_FOLDER_PREFIXES)
    skip_suffixes = cfg.get("skip_filename_suffixes", DEFAULT_SKIP_FILENAME_SUFFIXES)
    members: list[pathlib.Path] = []
    try:
        entries = list(audio.parent.iterdir())
    except OSError:
        return False, None, []
    for sib in entries:
        if not sib.is_file() or sib.suffix.lower() not in extensions:
            continue
        try:
            rel = sib.relative_to(source_root)
        except ValueError:
            continue
        if is_skipped_path(rel, skip_folders, skip_prefixes):
            continue
        if is_output_artifact(sib.name, skip_suffixes):
            continue
        members.append(sib)
    primary, siblings, bundle_id = detect_bundle(audio.parent, members)
    if primary == audio:
        return True, bundle_id, siblings
    return False, None, []


def maybe_stamp_primary_bundle(state: dict, audio: pathlib.Path,
                               source_root: pathlib.Path, cfg: dict) -> dict:
    is_primary, bid, siblings = detect_bundle_for_file(audio, source_root, cfg)
    if is_primary:
        state["bundle_id"] = bid
        state["bundle_role"] = "primary"
        state["bundle_members"] = sorted(str(s) for s in siblings)
    return state


def apply_bundle_metadata(candidates: list[tuple[pathlib.Path, pathlib.Path, dict]],
                          cfg: dict, mapper: dict) -> list[tuple[pathlib.Path, pathlib.Path, dict]]:
    """Group by directory + Zoom timestamp, stamp siblings, drop them from the queue.

    Files without a Zoom timestamp token are never bundled (each is standalone).
    """
    host_label = host_label_of(cfg)
    by_dir: dict[pathlib.Path, list[tuple[pathlib.Path, pathlib.Path, dict]]] = {}
    for tup in candidates:
        by_dir.setdefault(tup[0].parent, []).append(tup)

    kept: list[tuple[pathlib.Path, pathlib.Path, dict]] = []
    for directory, dir_members in by_dir.items():
        by_ts: dict[str, list[tuple[pathlib.Path, pathlib.Path, dict]]] = {}
        for tup in dir_members:
            m = BUNDLE_TS_PATTERN.search(tup[0].name)
            key = m.group(1) if m else "_no_ts"
            by_ts.setdefault(key, []).append(tup)
        for ts_key, members in by_ts.items():
            if ts_key == "_no_ts":
                kept.extend(members)
                continue
            paths = [t[0] for t in members]
            primary, siblings, bundle_id = detect_bundle(directory, paths)
            if primary is None:
                kept.extend(members)
                continue
            primary_tup = next(t for t in members if t[0] == primary)
            src_root = primary_tup[1]
            # Announce the bundle only when the primary is still pending. A finished
            # bundle gets re-grouped every sweep; re-logging it each time is noise that
            # makes an idle sweep look busy. Siblings are still (idempotently) stamped.
            _psf = state_path(primary)
            _primary_done = False
            if _psf.exists():
                try:
                    _primary_done = (json.loads(_psf.read_text(encoding="utf-8")).get("status")
                                     in ("asr-done", "failed"))
                except Exception:
                    _primary_done = False
            if not _primary_done:
                log(f"bundle in {directory.name}/[{ts_key}]: primary={primary.name} "
                    f"siblings={len(siblings)} (id={bundle_id})")
            pid, _ = route_pid_for_audio(primary, src_root, primary_tup[2], cfg, mapper)
            for sib in siblings:
                stamp_bundle_sibling(sib, bundle_id, primary, pid, host_label)
            kept.append(primary_tup)
    return kept


# ---------------------------------------------------------------------------
# State machine: sidecar, markers, transitions
# ---------------------------------------------------------------------------


def state_path(audio: pathlib.Path) -> pathlib.Path:
    return audio.with_suffix(audio.suffix + ".state.json")


def claim_path(audio: pathlib.Path) -> pathlib.Path:
    return audio.with_suffix(audio.suffix + ".claim.json")


def marker_processed_asr(audio: pathlib.Path) -> pathlib.Path:
    return audio.with_suffix(audio.suffix + ".processed-asr")


def existing_transcript(audio: pathlib.Path,
                        transcript_dir: pathlib.Path | None = None) -> pathlib.Path | None:
    """Look for an already-written transcript (catch-up of pre-existing work).

    Searches, in order: the resolved ``transcript_dir`` for this file, a local
    ``Transcripts/`` next to the audio, and historical sidecar formats. Falls back
    to recording-timestamp matching because the ASR worker transliterates names.
    """
    base = audio.stem
    direct = [
        audio.parent / f"{base}-transcript.md",
        audio.parent / "Transcripts" / f"{base}-transcript.md",
        audio.parent / f"{base}_original.txt",
    ]
    search_dirs = [transcript_dir] if transcript_dir else []
    search_dirs.append(audio.parent / "Transcripts")
    for d in search_dirs:
        if d and d.is_dir():
            for c in d.glob(f"*{base}*-transcript.md"):
                direct.append(c)
            translit = slugify_from_filename(base)
            if translit:
                for c in d.glob(f"*{translit}*-transcript.md"):
                    direct.append(c)
    for c in direct:
        if c.is_file():
            return c
    tokens = re.findall(r"\d{6,}", base)
    if not tokens:
        return None
    for d in search_dirs:
        if d and d.is_dir():
            for tr in d.glob("*-transcript.md"):
                if any(token in tr.name for token in tokens):
                    return tr
    return None


def caught_up_state(transcript: pathlib.Path, pid: str | None, host_label: str) -> dict:
    mtime = dt.datetime.fromtimestamp(transcript.stat().st_mtime).isoformat(timespec="seconds")
    return {
        "status": "asr-done", "pid": pid, "attempts": 1,
        "started_at": mtime, "finished_at": mtime,
        "transcript_path": str(transcript), "last_error": None,
        "host": f"{host_label}-CATCHUP", "duration_sec": None, "caught_up": True,
    }


def queued_state(pid: str | None, host_label: str) -> dict:
    return {
        "status": "queued", "pid": pid, "attempts": 0,
        "started_at": None, "finished_at": None,
        "transcript_path": None, "last_error": None,
        "host": host_label, "duration_sec": None, "caught_up": False,
    }


def reset_stuck(state: dict, threshold_min: int) -> bool:
    if state.get("status") != "in-progress":
        return False
    started_at = state.get("started_at")
    if not started_at:
        return False
    try:
        started = dt.datetime.fromisoformat(started_at)
    except Exception:
        return False
    age_min = (dt.datetime.now() - started).total_seconds() / 60
    if age_min > threshold_min:
        state["status"] = "queued"
        state["last_error"] = f"stuck > {threshold_min}m, reset"
        return True
    return False


def move_to_failed(audio: pathlib.Path, source_root: pathlib.Path, error_tail: str) -> None:
    """Move a permanently-failed file (+ sidecars) into ``{source_root}/_failed/``."""
    failed_dir = source_root / "_failed"
    failed_dir.mkdir(parents=True, exist_ok=True)
    for p in (audio, state_path(audio), marker_processed_asr(audio), claim_path(audio),
              asr_log_path(audio)):
        if p.exists():
            try:
                shutil.move(str(p), str(failed_dir / p.name))
            except Exception as exc:
                log(f"move_to_failed {p.name} failed: {exc}")
    try:
        (failed_dir / f"{audio.name}.error.txt").write_text(error_tail or "", encoding="utf-8")
    except Exception:
        pass


def _free_processed_name(dest_dir: pathlib.Path, audio: pathlib.Path) -> str:
    """A filename for ``audio`` in ``dest_dir`` colliding with no existing audio or its
    ``.state.json`` sidecar. Keeps the primary extension so the ``.state.json`` suffix
    (which ``_write_project_index`` globs on) stays intact for a same-name re-drop."""
    name = audio.name
    if not (dest_dir / name).exists() and not (dest_dir / f"{name}.state.json").exists():
        return name
    n = 1
    while True:
        cand = f"{audio.stem}-dup{n}{audio.suffix}"
        if not (dest_dir / cand).exists() and not (dest_dir / f"{cand}.state.json").exists():
            return cand
        n += 1


def move_to_processed(audio: pathlib.Path, source_root: pathlib.Path, cfg: dict) -> bool:
    """OPT-IN tidy: move a successfully-processed file (+ ALL sidecars) into
    ``{source_root}/{processed_dir_name}/`` — a mirror of ``_failed/`` for success.

    NON-FATAL by contract: ASR already succeeded and the transcript/state outputs are
    already persisted under ``sessions/``, so a failed move is logged and swallowed and
    must never fail the sweep. Nothing is deleted; the source stays fully recoverable."""
    dir_name = cfg.get("processed_dir_name", "_processed")
    dest_dir = source_root / dir_name
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"move_to_processed mkdir failed (non-fatal) {audio.name}: {exc}")
        return False
    new_name = _free_processed_name(dest_dir, audio)
    moved_any = False
    for p in (audio, state_path(audio), marker_processed_asr(audio), claim_path(audio),
              asr_log_path(audio)):
        if p.exists():
            # Rename the whole group consistently: swap the audio's basename for the
            # collision-free one, preserving each sidecar's suffix.
            dest = dest_dir / p.name.replace(audio.name, new_name, 1)
            try:
                shutil.move(str(p), str(dest))
                moved_any = True
            except FileNotFoundError:
                pass  # a peer/other sweep already moved the group — benign
            except Exception as exc:
                log(f"move_to_processed {p.name} failed (non-fatal): {exc}")
    if moved_any:
        log(f"moved to {dir_name}/: {audio.name}")
    return moved_any


# Signatures of a cloud-mount / input-staging failure (not the file's fault). Such a
# failure defers the file (re-queued, no attempt counted) instead of quarantining it.
_TRANSIENT_CLOUD_ERROR_SIGNS = (
    "resource deadlock avoided",  # EDEADLK (errno 11): macOS fcopyfile on a GDrive src
    "errno 11",
    "operation timed out",        # ETIMEDOUT (errno 60): dataless enumeration / read
    "errno 60",
    "stage_ascii_input",          # failed staging the unicode->ascii input copy
    "input-ascii",                # staged-copy destination path in the traceback
)


def _is_transient_cloud_error(err_tail: str) -> bool:
    """True if an ASR failure looks like a wedged cloud mount / input-staging problem
    (dataless read, fcopyfile deadlock) rather than a real problem with the media."""
    t = (err_tail or "").lower()
    return any(sign in t for sign in _TRANSIENT_CLOUD_ERROR_SIGNS)


# ---------------------------------------------------------------------------
# CPU-aware scheduling
# ---------------------------------------------------------------------------


def measure_cpu_percent(sample_sec: float = 1.0) -> float | None:
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        return float(psutil.cpu_percent(interval=sample_sec))
    except Exception:
        return None


def wait_for_cpu_available(cfg: dict) -> tuple[bool, str]:
    """Wait until CPU load drops below threshold, bounded by a max wait time.

    Returns (proceed, reason). If psutil is unavailable, proceeds (graceful no-op).
    proceed=False means: exceeded max wait; keep the file queued and retry later.
    """
    if not cfg.get("respect_cpu_load", True):
        return True, "cpu-check disabled in config"
    threshold = float(cfg.get("cpu_threshold_percent", 70))
    poll_sec = float(cfg.get("cpu_check_poll_sec", 60))
    max_wait_sec = float(cfg.get("cpu_check_max_wait_min", 30)) * 60.0
    initial = measure_cpu_percent(sample_sec=2.0)
    if initial is None:
        return True, "cpu-check skipped (psutil unavailable)"
    if initial < threshold:
        return True, f"cpu={initial:.0f}% below threshold {threshold:.0f}%"
    log(f"cpu busy: {initial:.0f}% >= {threshold:.0f}%; waiting up to {max_wait_sec/60:.0f}m")
    waited = 0.0
    while waited < max_wait_sec:
        time.sleep(poll_sec)
        waited += poll_sec
        current = measure_cpu_percent(sample_sec=2.0)
        if current is None:
            return True, f"cpu-check skipped mid-wait after {waited/60:.1f}m"
        if current < threshold:
            return True, f"cpu={current:.0f}% available after {waited/60:.1f}m"
        log(f"cpu still busy: {current:.0f}% (waited {waited/60:.1f}m / {max_wait_sec/60:.0f}m)")
    return False, f"cpu busy >{max_wait_sec/60:.0f}m, deferred"


# ---------------------------------------------------------------------------
# Host-local lock + process window
# ---------------------------------------------------------------------------


def _pid_alive(pid) -> bool:
    """Best-effort: does a process with this PID exist on the local host?

    Used to reclaim a watcher lock left by a run that crashed, was killed, or was
    frozen by machine sleep — instead of waiting out the full stale window. The
    lock is host-scoped (``watcher-{hostname}.lock``), so the PID is local and this
    check is meaningful. Conservative: when liveness can't be determined, returns
    True so the caller falls back to the age-based staleness check.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return True  # can't tell on this host -> fall back to age check
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def acquire_watcher_lock() -> pathlib.Path | None:
    """Guard concurrent invocations on THIS machine (not cross-machine claim).

    Exclusive per host: a sweep proceeds only if it holds this lock, and it can hold it
    only when the previous owner is gone (dead PID / 6h-stale / no lock). That guarantee
    is what lets ``inprogress_recoverable`` reclaim this host's own stranded in-progress
    files at once — while this sweep runs, no other live sweep of ours can be working
    them (a concurrent sweep would find our live PID here and exit without scanning)."""
    lock_dir = pathlib.Path(tempfile.gettempdir()) / "audio-inbox"
    lock_dir.mkdir(exist_ok=True)
    lock = lock_dir / f"watcher-{socket.gethostname()}.lock"
    if lock.exists():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            pid = data.get("pid")
            # A same-host owner whose PID is gone crashed / was killed / slept through
            # its run: reclaim immediately rather than blocking this node for the full
            # 6h stale window (critical for a launchd/Task-Scheduler node that a laptop
            # or Mac Mini can suspend mid-sweep).
            if not _pid_alive(pid):
                log(f"watcher lock owner pid={pid} not alive; taking over")
            else:
                started = dt.datetime.fromisoformat(data.get("started_at", ""))
                age_min = (dt.datetime.now() - started).total_seconds() / 60
                if age_min < 360:
                    log(f"watcher lock present, age {age_min:.1f}m (pid={pid}); skipping")
                    return None
                log(f"watcher lock stale ({age_min:.1f}m); taking over")
        except Exception:
            log("watcher lock unparseable; taking over")
    atomic_write_json(lock, {
        "host": socket.gethostname(), "pid": os.getpid(),
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
    })
    return lock


def release_watcher_lock(lock: pathlib.Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def _within_process_window(window: str | None, now: dt.datetime | None = None) -> bool:
    """True if ``now`` is within ``"HH:MM-HH:MM"`` (overnight ranges supported)."""
    if not window:
        return True
    try:
        start_str, end_str = window.split("-", 1)
        sh, sm = (int(x) for x in start_str.strip().split(":"))
        eh, em = (int(x) for x in end_str.strip().split(":"))
    except (ValueError, AttributeError):
        return True
    cur = (now or dt.datetime.now()).time()
    start, end = dt.time(sh, sm), dt.time(eh, em)
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


# ---------------------------------------------------------------------------
# Multi-node claim coordination (opt-in: cfg.enable_multi_machine).
# Per-file <file>.claim.json on the shared source; claim-and-verify + lease +
# heartbeat. Resilient to cloud-drive eventual consistency.
# ---------------------------------------------------------------------------


def is_claim_expired(claim: dict, now_utc: dt.datetime, lease_minutes: int) -> bool:
    lease = _parse_utc(claim.get("lease_until"))
    if lease is None:
        claimed = _parse_utc(claim.get("claimed_at"))
        if claimed is None:
            return True
        lease = claimed + dt.timedelta(minutes=lease_minutes)
    return now_utc >= lease


def resolve_claim_winner(a: dict, b: dict) -> dict:
    ta = _parse_utc(a.get("claimed_at")) or utcnow()
    tb = _parse_utc(b.get("claimed_at")) or utcnow()
    if ta != tb:
        return a if ta < tb else b
    return a if a.get("claimed_by", "") <= b.get("claimed_by", "") else b


def claim_is_dead(audio: pathlib.Path, cfg: dict) -> bool:
    """True when an ``in-progress`` file's owner has stopped heartbeating, so another
    node may take it over. Authoritative liveness signal is the claim lease: a live
    owner refreshes it every ``claim_heartbeat_minutes`` (see _ClaimHeartbeat)."""
    cp = claim_path(audio)
    try:
        if not cp.is_file():
            return True  # in-progress but no claim at all -> orphaned
        claim = (json.loads(cp.read_text(encoding="utf-8")) or {}).get("claim", {})
    except OSError:
        return False  # unreadable this sweep -> be conservative, leave it
    except Exception:
        return True   # garbled claim -> treat as dead
    return is_claim_expired(claim, utcnow(), int(cfg.get("claim_lease_minutes", 30)))


def _owned_by_this_host(state: dict, cfg: dict) -> bool:
    """True if THIS host stamped the in-progress state (``state['host']`` == our
    ``host_label``, written at pickup, line ~1702).

    A missing host stamp (legacy sidecar) is treated as ours only on a single-machine
    node — there, only this host could have produced it. On a multi-machine node a
    stamp-less file could belong to a peer, so we do NOT assume ownership and let the
    claim-lease check decide instead."""
    h = state.get("host")
    if h:
        return h == host_label_of(cfg)
    return not cfg.get("enable_multi_machine")


def inprogress_recoverable(audio: pathlib.Path, state: dict, cfg: dict) -> bool:
    """Whether an ``in-progress`` file may be re-queued this sweep.

    Self-blocked -> auto-correct: an in-progress file stamped with OUR ``host`` is
    stranded from an earlier run of ours. This sweep holds the exclusive host lock (it
    would not be here otherwise — see ``acquire_watcher_lock``), so no live sweep of
    ours can be working the file: a concurrent sweep finds our live PID on the lock and
    exits without scanning, so a genuinely long-running local ASR is never seen here and
    never reclaimed. Therefore an own-host in-progress file is always safe to reclaim NOW
    — no need to wait out the claim lease / stuck window.

    Blocked by someone else: fall back to the peer-liveness signals. Multi-machine —
    an expired claim lease (the owning node stopped heartbeating). Single-machine —
    wall-clock staleness, matching ``reset_stuck``.

    Why this exists: ``reset_stuck`` and the claim-preempt both live inside
    ``process_one_file``, but the queue admitted only ``queued``/no-state files, so a
    hard node death (crash / power loss / window close mid-ASR) left the file
    ``in-progress`` forever — no node ever reached the recovery paths. This gate lets
    the orphan back into the queue.
    """
    if _owned_by_this_host(state, cfg):
        return True
    if cfg.get("enable_multi_machine"):
        return claim_is_dead(audio, cfg)
    started = state.get("started_at")
    if not started:
        return False
    try:
        age_min = (dt.datetime.now() - dt.datetime.fromisoformat(started)).total_seconds() / 60
    except Exception:
        return False
    return age_min > int(cfg.get("stuck_threshold_minutes", 120))


class _CloudOpTimeout(Exception):
    """A filesystem syscall exceeded its watchdog budget (see _cloud_watchdog)."""


@contextlib.contextmanager
def _cloud_watchdog(seconds: int, label: str):
    """Abort a wedged filesystem syscall instead of freezing the whole sweep.

    macOS Google Drive (File Provider) can block a stat()/listdir()/open() on an
    online-only path indefinitely with no timeout of its own (observed: a single
    dataless recording hung the watcher 24m at 0% CPU). SIGALRM (POSIX, main thread)
    interrupts the blocking call. No-op where SIGALRM is unavailable (Windows), which
    does not exhibit this hang. Disarmed by ``_cancel_cloud_watchdog`` before the
    legitimately long CPU-wait / ASR phase.
    """
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum, frame):
        raise _CloudOpTimeout(f"cloud op exceeded {seconds}s: {label}")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _cancel_cloud_watchdog() -> None:
    """Disarm the pickup watchdog before the (legitimately long) CPU-wait / ASR phase."""
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)


def host_can_process(cfg: dict) -> bool:
    """A file's required capabilities vs this host's. Generic: gate by config.

    ``required_capabilities`` (top-level) must be a subset of node capabilities.
    Returns True when multi-machine is off or no requirement is configured.
    """
    if not cfg.get("enable_multi_machine"):
        return True
    required = set(cfg.get("required_capabilities", []))
    if not required:
        return True
    have = set(node_field(cfg, "capabilities", []) or [])
    return required.issubset(have)


def try_claim_file(audio: pathlib.Path, cfg: dict) -> bool:
    """Claim-and-verify over a cloud-synced claim.json.

    Single-machine (enable_multi_machine false) -> True immediately (no claim file).
    """
    if not cfg.get("enable_multi_machine"):
        return True
    host = host_label_of(cfg)
    lease_min = int(cfg.get("claim_lease_minutes", 30))
    wait_sec = float(cfg.get("claim_sync_wait_seconds", 8))
    now = utcnow()
    cp = claim_path(audio)
    existing = {}
    if cp.is_file():
        try:
            existing = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    claim = existing.get("claim") or {}
    preempt = int(claim.get("preempt_count", 0))
    if claim.get("claimed_by") and claim["claimed_by"] != host:
        if not is_claim_expired(claim, now, lease_min):
            log(f"claim held by {claim['claimed_by']} (lease ok): yield {audio.name}")
            return False
        log(f"claim by {claim['claimed_by']} expired; preempting {audio.name}")
        preempt += 1
    my_claim = {
        "claimed_by": host,
        "claimed_at": utcnow_iso(),
        "lease_until": (now + dt.timedelta(minutes=lease_min)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "heartbeat_at": utcnow_iso(),
        "claim_phase": "pickup",
        "preempt_count": preempt,
    }
    atomic_write_json(cp, {"claim": my_claim, "audio": audio.name})
    time.sleep(wait_sec)  # let the cloud drive converge before re-reading
    final = {}
    if cp.is_file():
        try:
            final = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            final = {}
    final_claim = final.get("claim") or {}
    if final_claim.get("claimed_by") == host:
        return True
    log(f"race-lost: {audio.name} -> {final_claim.get('claimed_by') or 'unknown'}")
    return False


class _ClaimHeartbeat:
    """Daemon thread refreshing claim.json heartbeat + extending the lease."""

    def __init__(self, audio: pathlib.Path, cfg: dict):
        self.audio = audio
        self.cfg = cfg
        self.host = host_label_of(cfg)
        self.enabled = bool(cfg.get("enable_multi_machine"))
        self.interval = max(30, int(cfg.get("claim_heartbeat_minutes", 5)) * 60)
        self.lease_min = int(cfg.get("claim_lease_minutes", 30))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _beat_once(self) -> None:
        cp = claim_path(self.audio)
        if not cp.is_file():
            return
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            return
        claim = data.get("claim") or {}
        if claim.get("claimed_by") != self.host:
            return  # preempted — stop refreshing someone else's claim
        now = utcnow()
        claim["heartbeat_at"] = utcnow_iso()
        claim["lease_until"] = (now + dt.timedelta(minutes=self.lease_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        claim["claim_phase"] = "asr"
        data["claim"] = claim
        try:
            atomic_write_json(cp, data)
        except Exception as exc:
            log(f"heartbeat write failed (non-critical): {exc}")

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._beat_once()

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True, name="claim-heartbeat")
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False


def retire_claim(audio: pathlib.Path, cfg: dict) -> None:
    """Mark claim_phase=done after asr-done (tidy signal; lease already covers it)."""
    if not cfg.get("enable_multi_machine"):
        return
    host = host_label_of(cfg)
    cp = claim_path(audio)
    if not cp.is_file():
        return
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        claim = data.get("claim") or {}
        if claim.get("claimed_by") == host:
            claim["claim_phase"] = "done"
            claim["heartbeat_at"] = utcnow_iso()
            data["claim"] = claim
            atomic_write_json(cp, data)
    except Exception as exc:
        log(f"retire claim failed (non-critical): {exc}")


# ---------------------------------------------------------------------------
# Output resolution: transcript_dir / state_dir / session-card adapter
# ---------------------------------------------------------------------------


def session_placeholder_ctx(cfg: dict, pid: str, sid: str, started: dt.datetime) -> dict:
    ctx = base_placeholder_ctx(cfg)
    ctx.update({
        "pid": pid,
        "sid": sid,
        "YYYY": started.strftime("%Y"),
        "YYYY-MM": started.strftime("%Y-%m"),
        "YYYY-MM-DD": started.strftime("%Y-%m-%d"),
    })
    return ctx


def resolve_output_dir(cfg: dict, key: str, ctx: dict, fallback: pathlib.Path) -> pathlib.Path:
    outputs = cfg.get("outputs") or {}
    tpl = outputs.get(key)
    if not tpl:
        return fallback
    return pathlib.Path(resolve_template(tpl, ctx)).expanduser()


def write_session_card(cfg: dict, state: dict, audio: pathlib.Path,
                       transcript: pathlib.Path | None, ctx: dict) -> None:
    """Optional output adapter. Default 'none' = no card (fully generic core).

    'obsidian' writes a minimal stub session note with frontmatter; the personal
    enrichment layer (project 258) extends it. Kept intentionally small.
    """
    outputs = cfg.get("outputs") or {}
    card_cfg = outputs.get("session_card") or {}
    adapter = card_cfg.get("adapter", "none")
    if adapter == "none":
        return
    if adapter != "obsidian":
        log(f"unknown session_card adapter '{adapter}'; skipping")
        return
    target_tpl = card_cfg.get("target")
    if not target_tpl:
        return
    target_dir = pathlib.Path(resolve_template(target_tpl, ctx)).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    sid = state.get("session_id") or audio.stem
    card = target_dir / f"{sid}.md"
    if card.exists():
        return
    tr = str(transcript) if transcript else ""
    body = (
        "---\n"
        "tags: [note, audio-session]\n"
        f"SessionId: {sid}\n"
        f"pid: \"{state.get('pid', '')}\"\n"
        f"media_primary: \"{audio.name}\"\n"
        f"transcript_file: \"{tr}\"\n"
        f"CDate: {dt.datetime.now().strftime('%Y-%m-%d')}\n"
        "stage: 1\n"
        "---\n\n"
        f"# Session: {sid}\n\n"
        f"- Audio: `{audio.name}`\n"
        f"- Transcript: `{tr}`\n"
    )
    try:
        card.write_text(body, encoding="utf-8")
    except Exception as exc:
        log(f"session card write failed: {exc}")


# ---------------------------------------------------------------------------
# ASR dispatch
# ---------------------------------------------------------------------------


class AsrEnvironmentError(Exception):
    """The ASR worker could not be launched at all (bad interpreter / env).

    This is an infrastructure failure that affects every file equally, so it must
    NOT count against a file's attempts or quarantine it to ``_failed/``; the sweep
    aborts cleanly instead and resumes once the environment is fixed.
    """


def _clean_path_value(v: str | None) -> str | None:
    """Strip stray quotes / trailing '>' / whitespace that creep into config paths
    via copy-paste (e.g. a value pasted with a PowerShell prompt ``>`` suffix)."""
    if not v:
        return v
    return str(v).strip().strip('"').strip("'").rstrip(">").strip() or None


def asr_log_path(audio: pathlib.Path) -> pathlib.Path:
    return audio.with_suffix(audio.suffix + ".asr-log.txt")


def _write_asr_log(audio: pathlib.Path, cmd: list[str], returncode,
                   full_stdout: str, full_stderr: str) -> None:
    """Persist the full ASR worker output next to the audio for post-mortem.

    Stays beside the file on failure (and moves into ``_failed/`` with it on the
    final attempt); removed on success. The captured stderr/stdout is the full
    worker output, not just the tail recorded in state.json.
    """
    try:
        asr_log_path(audio).write_text(
            "# ASR processing log\n"
            f"file: {audio}\n"
            f"when: {now_iso()}\n"
            f"returncode: {returncode}\n"
            f"command: {' '.join(cmd)}\n\n"
            f"===== STDERR =====\n{full_stderr}\n"
            f"===== STDOUT =====\n{full_stdout}\n",
            encoding="utf-8")
    except Exception as exc:
        log(f"asr-log write failed: {exc}")


def _discover_transcript(output_dir: pathlib.Path, stem: str) -> pathlib.Path | None:
    """Find the transcript the worker wrote in this session's transcripts dir.

    Name-based matches first (the worker transliterates names: spaces->_, lower),
    then a session-scoped fallback — any top-level ``*-transcript.md`` in
    ``output_dir`` is THIS file's result, because the dir is per-session.
    Subdirectories (``asr/`` intermediates) are not considered (glob is top-level).
    """
    patterns = [
        f"*{stem}*-transcript.md",
        f"*{slugify_from_filename(stem)}*-transcript.md",
        f"*{stem.lower().replace(' ', '_')}*-transcript.md",
    ]
    for pat in patterns:
        try:
            for cand in output_dir.glob(pat):
                if cand.is_file():
                    return cand
        except (OSError, ValueError):
            continue
    m = re.search(r"(\d{6})[_-](\d{6})", stem)
    if m:
        ts = f"{m.group(1)}_{m.group(2)}"
        for cand in output_dir.glob("*-transcript.md"):
            if ts in cand.name:
                return cand
    pool = [p for p in output_dir.glob("*-transcript.md") if p.is_file()]
    named = [p for p in pool if p.name != "session-transcript.md"]
    chosen = named or pool
    if chosen:
        try:
            return max(chosen, key=lambda p: p.stat().st_mtime)
        except OSError:
            return chosen[0]
    return None


def _cleanup_intermediates(output_dir: pathlib.Path, cfg: dict) -> None:
    """Prune heavy intermediate ASR artifacts from a session output dir after success.

    Removes decoded chunk WAV/JSON, raw + merged segment dumps, and the ``asr/``
    scratch subdir — keeping the transcript, segments (vtt/jsonl) and run-meta.
    Recovery only matters for incomplete runs, so deleting on success is safe.
    Disable with ``cleanup_intermediates: false``; override the lists with
    ``cleanup_globs`` / ``cleanup_dirs``.
    """
    if not cfg.get("cleanup_intermediates", True):
        return
    # NOTE: *-raw.json is kept on purpose — speaker_pass reuses it to re-run
    # diarization/voiceprints without re-ASR. Override via cleanup_globs to drop it.
    globs = cfg.get("cleanup_globs", [
        "*-asr-chunk-*.wav", "*-asr-chunk-*.json", "*-asr-merged.json",
    ])
    dirs = cfg.get("cleanup_dirs", ["asr"])
    removed = 0
    for pat in globs:
        for p in output_dir.glob(pat):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    for sub in dirs:
        d = output_dir / sub
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        log(f"cleaned {removed} intermediate artifact(s) in {output_dir.name}")


def _write_project_index(hub_root: pathlib.Path, pid: str, cfg: dict) -> None:
    """Write ``{hub_root}/{pid}/_sessions-index.md`` — processed vs pending files.

    Status comes from the per-file ``*.state.json`` sidecars anywhere in the
    project except ``sessions/`` (those are outputs). asr-done = processed;
    queued/in-progress = pending; the rest (failed / bundle-sibling) listed apart.
    """
    proj = hub_root / pid
    if not _safe_is_dir_bool(proj):
        return
    try:
        sidecars = [p for p in proj.rglob("*.state.json")
                    if "sessions" not in p.relative_to(proj).parts]
    except OSError:
        return
    done, pending, other = [], [], []
    for sf in sidecars:
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = sf.name[: -len(".state.json")]
        status = st.get("status", "?")
        sid = st.get("session_id", "") or ""
        row = f"| `{name}` | {status} | {sid} |"
        if status == "asr-done":
            done.append(row)
        elif status in ("queued", "in-progress"):
            pending.append(row)
        else:
            other.append(row)

    def section(title, rows):
        uniq = sorted(set(rows))
        return [f"## {title} ({len(uniq)})", "", "| Файл | Статус | SessionId |",
                "|---|---|---|", *(uniq or ["| — | — | — |"]), ""]

    lines = [f"# {pid} — индекс сессий", "",
             f"_Авто-обновление watcher'ом. Обработано: {len(set(done))} · "
             f"в очереди: {len(set(pending))} · прочее: {len(set(other))}._", ""]
    lines += section("Обработанные (asr-done)", done)
    lines += section("Необработанные (queued / in-progress)", pending)
    if other:
        lines += section("Прочие (failed / bundle-sibling)", other)
    try:
        (proj / "_sessions-index.md").write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        log(f"project index write failed for {pid}: {exc}")


def _parse_worker_result(stdout: str) -> dict | None:
    """The worker prints its final result as one JSON object on stdout. Return the last
    line that parses as a dict carrying ``status`` (its summary), or None."""
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"status"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "status" in obj:
            result = obj
    return result


def _fmt_dur(sec: float) -> str:
    sec = float(sec or 0)
    return f"{sec:.0f}s" if sec < 60 else f"{sec / 60:.1f}m"


def _log_result_summary(audio: pathlib.Path, pid: str | None, proc_sec: float,
                        meta: dict | None, transcript: pathlib.Path | None) -> None:
    """Highlighted, greppable end-of-file block: what was processed and how efficiently,
    with diarization made explicit (source + speaker count + its share of the time)."""
    meta = meta or {}
    audio_sec = float(meta.get("duration_sec") or 0)
    diar = meta.get("diarization") or {}
    align = meta.get("alignment") or {}
    assigned = int(align.get("assigned_segments") or 0)
    total_seg = assigned + int(align.get("unassigned_segments") or 0)
    log("=" * 60)
    log(f"DONE: {audio.name}  (pid={pid})")
    rt = f"  ({proc_sec / audio_sec:.1f}x realtime)" if audio_sec > 0 else ""
    log(f"  audio {_fmt_dur(audio_sec)} -> processed {_fmt_dur(proc_sec)}{rt}")
    if not diar.get("enabled"):
        log(f"  speakers: off (no diarization) | segments {total_seg}")
    else:
        src = diar.get("source") or "diarization"
        diar_sec = diar.get("elapsed_sec")
        share = f" | diarization {diar_sec}s" if diar_sec is not None else ""
        note = " (from transcript, no pyannote)" if src in ("ktalk_txt", "zoom_vtt") else ""
        status = diar.get("status")
        if status == "fallback":
            log(f"  speakers: DIARIZATION FAILED -> ASR only | {diar.get('reason') or diar.get('error')}")
        else:
            log(f"  speakers: {diar.get('speakers_detected') or 0} via {src}{note} | "
                f"segments {assigned}/{total_seg} labeled{share}")
    if transcript:
        log(f"  transcript: {transcript.name}")
    log("=" * 60)


def run_asr(audio: pathlib.Path, cfg: dict, pid: str | None,
            source_root: pathlib.Path, output_dir: pathlib.Path,
            config_path: pathlib.Path,
            execution_mode: str | None = None) -> tuple[bool, str, pathlib.Path | None, dict | None]:
    """Invoke media_transcribe_cli for one file. Returns (ok, error_tail, transcript, meta).

    ``meta`` is the worker's parsed result JSON (audio duration, diarization, alignment),
    or None if it could not be parsed. ``execution_mode`` (e.g. ``speaker_pass``) is
    forwarded to the worker to reuse existing ASR (``*-raw.json``) and run only
    diarization/voiceprints — no whisper.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    python = _clean_path_value(node_field(cfg, "transcribe_python", None)) or sys.executable
    cli = _clean_path_value(cfg.get("transcribe_cli"))
    if not cli:
        cli = str(pathlib.Path(__file__).resolve().parent / "media_transcribe_cli.py")

    cmd: list[str] = [
        python, cli,
        "--config", str(config_path),
        "--input", str(audio),
        "--output-dir", str(output_dir),
        "--quality-preset", cfg.get("quality_preset", "medium"),
        "--speaker-mode", cfg.get("speaker_mode", "diarize"),
        "--timestamps", cfg.get("timestamps", "both"),
        "--inbox", str(source_root),
    ]
    if pid:
        cmd += ["--project-id", str(pid)]
    if execution_mode:
        cmd += ["--execution-mode", execution_mode]

    # A Ktalk download brings its own named speakers -> hand them to the worker and
    # let it skip diarization. Only meaningful when speakers were wanted at all.
    if cfg.get("speaker_mode", "diarize") == "diarize":
        ktalk_txt = ktalk_sidecar_for(audio, cfg)
        if ktalk_txt:
            cmd += ["--ktalk-txt", str(ktalk_txt)]
            log(f"ktalk transcript found (speakers from export, no diarization): {ktalk_txt.name}")

    # Voiceprint storage at the PROJECT ROOT ({hub_root}/{pid}) when enabled.
    # The registry (index.json + profiles/) lives with the project; the embeddings
    # store stays node-local (off the shared hub). Skipped for reserved pids (_…)
    # and when voiceprint_mode is off.
    vp = (cfg.get("outputs") or {}).get("voiceprints") or {}
    vmode = cfg.get("voiceprint_mode", "off")
    if pid and not str(pid).startswith("_") and vmode != "off":
        vctx = base_placeholder_ctx(cfg)
        vctx["pid"] = str(pid)
        reg_tpl = vp.get("project_projection")
        store_tpl = vp.get("local_cache")
        if reg_tpl:
            cmd += ["--project-speaker-registry", resolve_template(reg_tpl, vctx)]
        if store_tpl:
            store_dir = pathlib.Path(resolve_template(store_tpl, vctx)).expanduser()
            try:
                store_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            cmd += ["--voiceprint-store", str(store_dir / "voiceprints.json")]
        gr_tpl = vp.get("global_registry")
        if gr_tpl:
            cmd += ["--global-registry", resolve_template(gr_tpl, vctx)]
        cmd += ["--voiceprint-mode", vmode]

    log(f"ASR start: {audio.name} (pid={pid}, output={output_dir})")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
    except Exception as exc:
        msg = f"subprocess error: {exc}"
        _write_asr_log(audio, cmd, "spawn-failed", "", msg)
        # Infra failure: the worker never started (bad interpreter / env). Don't
        # blame this file — let process_one_file reset it and abort the sweep.
        raise AsrEnvironmentError(f"{msg} (interpreter: {python})") from exc

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _tee(stream, sink: list[str]) -> None:
        for line in iter(stream.readline, ""):
            sink.append(line)
            sys.stderr.write(line)
            sys.stderr.flush()
        stream.close()

    t_out = threading.Thread(target=_tee, args=(proc.stdout, stdout_lines), daemon=True)
    t_err = threading.Thread(target=_tee, args=(proc.stderr, stderr_lines), daemon=True)
    t_out.start()
    t_err.start()
    proc.wait()
    t_out.join()
    t_err.join()

    full_stdout = "".join(stdout_lines)
    full_stderr = "".join(stderr_lines)
    stderr_tail = "\n".join(full_stderr.splitlines()[-30:])
    result_meta = _parse_worker_result(full_stdout)
    _write_asr_log(audio, cmd, proc.returncode, full_stdout, full_stderr)
    # Success criterion = a transcript was produced. output_dir is the session's
    # own transcripts dir, so ANY top-level *-transcript.md in it is THIS file's
    # result. The worker transliterates names unpredictably (spaces->_, lowercase),
    # so name-based globs miss — the session-scoped fallback is the reliable signal.
    transcript = _discover_transcript(output_dir, audio.stem)
    stdout_json_ok = "phase=stdout_json ok" in full_stderr
    if transcript is not None:
        if proc.returncode != 0:
            log(f"exit={proc.returncode} but transcript present — treating as success "
                f"(non-fatal/atexit crash; artifacts on disk)")
        return True, stderr_tail, transcript, result_meta
    # No transcript produced -> genuine failure.
    if proc.returncode != 0 and stdout_json_ok:
        log(f"exit={proc.returncode}, stdout_json ok, but no transcript in {output_dir} — failing")
    return False, stderr_tail, None, result_meta


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------


def process_one_file(audio: pathlib.Path, source_root: pathlib.Path, source: dict,
                     cfg: dict, mapper: dict, config_path: pathlib.Path) -> None:
    state_file = state_path(audio)
    host_label = host_label_of(cfg)
    pid, needs_class = route_pid_for_audio(audio, source_root, source, cfg, mapper)
    started = started_at_for(audio)

    if not host_can_process(cfg):
        return
    claim_required = source.get("claim", False)
    if claim_required and not try_claim_file(audio, cfg):
        return

    # resolve outputs early (used for catch-up search + ASR + state mirror)
    sid_preview = generate_session_id(audio, started)
    ctx = session_placeholder_ctx(cfg, pid, sid_preview, started)
    transcript_dir = resolve_output_dir(cfg, "transcript_dir", ctx, audio.parent / "Transcripts")
    state_dir = resolve_output_dir(cfg, "state_dir", ctx, transcript_dir)

    # 1) catch-up: existing transcript without a sidecar -> mark asr-done
    if not state_file.exists():
        transcript = existing_transcript(audio, transcript_dir)
        if transcript:
            state = caught_up_state(transcript, pid, host_label)
            state["session_id"] = sid_preview
            maybe_stamp_primary_bundle(state, audio, source_root, cfg)
            atomic_write_json(state_file, state)
            marker_processed_asr(audio).touch()
            return
        state = queued_state(pid, host_label)
        state["session_id"] = sid_preview
        if needs_class:
            state["needs_classification"] = True
        maybe_stamp_primary_bundle(state, audio, source_root, cfg)
        atomic_write_json(state_file, state)

    # 2) load + reset-stuck
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if reset_stuck(state, cfg.get("stuck_threshold_minutes", 120)):
        atomic_write_json(state_file, state)
    status = state.get("status")
    if status in {"asr-done", "failed", "in-progress", "bundle-sibling"}:
        return
    if not state.get("session_id"):
        state["session_id"] = sid_preview

    # Reuse an existing session transcript instead of re-running full ASR.
    # transcript_dir is session-scoped, so a transcript there belongs to this file.
    # Voiceprints off -> adopt it and skip ASR. Voiceprints on -> reuse the ASR via
    # speaker_pass (no whisper) when a *-raw.json is present; else fall back to full.
    exec_mode: str | None = None
    vmode = cfg.get("voiceprint_mode", "off")
    need_vp = vmode != "off" and bool(pid) and not str(pid).startswith("_")
    existing = _discover_transcript(transcript_dir, audio.stem)
    if existing is not None:
        if not need_vp:
            state["status"] = "asr-done"
            state["transcript_path"] = str(existing)
            state["last_error"] = None
            state["finished_at"] = now_iso()
            state["reused_transcript"] = True
            atomic_write_json(state_file, state)
            retire_claim(audio, cfg)
            marker_processed_asr(audio).touch()
            log(f"reuse existing transcript (skip ASR): {audio.name}")
            return
        if any(transcript_dir.glob("*-raw.json")):
            exec_mode = "speaker_pass"
            log(f"reuse ASR via speaker_pass (voiceprints only, no whisper): {audio.name}")

    # pickup (claim + cloud reads/writes) done — disarm the CloudStorage watchdog
    # before the legitimately long CPU-wait / ASR phase (see _cloud_watchdog).
    _cancel_cloud_watchdog()

    # 3) CPU-aware gate
    proceed, cpu_reason = wait_for_cpu_available(cfg)
    if not proceed:
        log(f"defer ASR: {audio.name} ({cpu_reason})")
        return

    # 4) queued -> in-progress -> ASR
    state["status"] = "in-progress"
    state["started_at"] = now_iso()
    state["host"] = host_label
    atomic_write_json(state_file, state)

    try:
        size_mb = f"{audio.stat().st_size / 1e6:.0f}MB"
    except OSError:
        size_mb = "?MB"
    # File-info line at pickup. For long files the worker then streams a per-chunk ETA
    # ("eta_wall_local~HH:MM:SS") so the finish time of the current file is visible live.
    log(f"▶ processing: {audio.name} | pid={pid} | {size_mb} | "
        f"speaker_mode={cfg.get('speaker_mode', 'diarize')} | session={state.get('session_id')}")
    t0 = time.time()
    try:
        with _ClaimHeartbeat(audio, cfg):
            ok, err_tail, transcript, result_meta = run_asr(
                audio, cfg, pid, source_root, transcript_dir, config_path, execution_mode=exec_mode)
    except AsrEnvironmentError as exc:
        # Roll the file back to queued without counting an attempt or quarantining
        # it; re-raise so the sweep aborts (every file would hit the same wall).
        state["status"] = "queued"
        state["last_error"] = str(exc)
        state["finished_at"] = now_iso()
        atomic_write_json(state_file, state)
        retire_claim(audio, cfg)
        raise
    duration = time.time() - t0
    state["duration_sec"] = round(duration, 1)
    state["finished_at"] = now_iso()
    log(f"asr done: {audio.name} ok={ok} elapsed={duration/60:.1f}m "
        f"transcript={transcript.name if transcript else 'NONE'}")

    if ok and transcript:
        state["status"] = "asr-done"
        state["transcript_path"] = str(transcript)
        state["last_error"] = None
        if cfg.get("enable_multi_machine"):
            state["processed_by_host"] = host_label
        atomic_write_json(state_file, state)
        # mirror canonical state into outputs.state_dir + optional session card
        try:
            mirror = state_dir / "state.json"
            atomic_write_json(mirror, state)
        except Exception as exc:
            log(f"state_dir mirror failed: {exc}")
        try:
            write_session_card(cfg, state, audio, transcript, ctx)
        except Exception as exc:
            log(f"session card adapter failed: {exc}")
        retire_claim(audio, cfg)
        marker_processed_asr(audio).touch()
        asr_log_path(audio).unlink(missing_ok=True)  # keep logs only on failure
        try:
            _cleanup_intermediates(transcript_dir, cfg)
        except Exception as exc:
            log(f"cleanup_intermediates failed: {exc}")
        _log_result_summary(audio, pid, duration, result_meta, transcript)
        return

    # failure -> classify. A cloud-mount / input-staging failure (macOS fcopyfile
    # EDEADLK on a Google Drive source, or a dataless read that never materializes)
    # is NOT the file's fault: it must not burn an attempt or quarantine the file to
    # _failed, which would strand real recordings that merely need the source made
    # available-offline. Defer instead — roll back to queued, keep the claim retired.
    err_tail = err_tail or "unknown error"
    if _is_transient_cloud_error(err_tail):
        state["status"] = "queued"
        state["last_error"] = err_tail
        atomic_write_json(state_file, state)
        retire_claim(audio, cfg)
        log(f"ASR deferred — transient cloud/staging error, attempt not counted "
            f"(make the source available-offline if it persists): {audio.name}")
        return

    # failure -> retry or terminal
    state["attempts"] = int(state.get("attempts", 0)) + 1
    state["last_error"] = err_tail
    max_attempts = int(cfg.get("max_attempts", 3))
    if state["attempts"] < max_attempts:
        state["status"] = "queued"
        atomic_write_json(state_file, state)
        log(f"ASR fail (retry {state['attempts']}/{max_attempts}): {audio.name}")
    else:
        state["status"] = "failed"
        atomic_write_json(state_file, state)
        log(f"ASR fail (final): {audio.name} -> _failed/")
        try:
            move_to_failed(audio, source_root, err_tail or "")
        except Exception as exc:
            log(f"move_to_failed error: {exc}")


# ---------------------------------------------------------------------------
# Sweep orchestration
# ---------------------------------------------------------------------------


def _sweep_processed_moves(cfg: dict, mapper: dict) -> None:
    """OPT-IN end-of-sweep phase: relocate ``asr-done`` sources (+ sidecars) into
    ``_processed/``. No-op unless ``on_asr_done == "move"``.

    Runs as its own phase (not inline in the success branch) because the main loop's
    actionable filter drops ``asr-done`` files, so an inline hook could neither honor the
    age gate nor uniformly cover the reused/caught-up paths. Here every terminal
    ``asr-done`` sidecar is visited via ``find_audio_files`` and moved uniformly, leaving
    the hot path untouched. ``on_asr_done_after_days`` defers the move until the file is
    that old (measured from ``finished_at``); 0 = move on this sweep."""
    if cfg.get("on_asr_done", "leave") != "move":
        return
    after_days = float(cfg.get("on_asr_done_after_days", 0) or 0)
    host = host_label_of(cfg)
    now = time.time()
    for audio, source_root, _source in find_audio_files(cfg):
        sf = state_path(audio)
        if not sf.exists():
            continue
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.get("status") != "asr-done":
            continue
        # Move only our own completions (multi-node): the file we finished. A peer's
        # asr-done on the shared hub is left for the peer to tidy.
        if cfg.get("enable_multi_machine") and st.get("host") not in (host, None):
            continue
        if after_days > 0:
            fin = st.get("finished_at")
            try:
                age_sec = ((dt.datetime.now() - dt.datetime.fromisoformat(fin)).total_seconds()
                           if fin else (now - _safe_mtime(audio)))
            except Exception:
                age_sec = now - _safe_mtime(audio)
            if age_sec < after_days * 86400:
                continue
        try:
            move_to_processed(audio, source_root, cfg)
        except Exception as exc:  # never let tidy-up break a sweep
            log(f"move_to_processed error (non-fatal) {audio.name}: {exc}")


def run_once(cfg: dict, config_path: pathlib.Path, *,
             catch_up_only: bool = False,
             time_budget_minutes: int | None = None,
             max_files: int | None = None,
             force_window: bool = False) -> None:
    sources = resolved_sources(cfg)
    if not sources:
        log("no sources configured")
        return
    existing_roots = []
    for r, _ in sources:
        ok, err = _probe_dir(r)
        if err:
            log(err)
        if ok:
            existing_roots.append(r)
    if not existing_roots:
        log("no readable source root this sweep (see messages above); nothing to do")
        return

    window = cfg.get("process_window_local")
    if window and not force_window and not _within_process_window(window):
        log(f"outside process_window_local={window}; skipping tick")
        return
    if window and force_window and not _within_process_window(window):
        log(f"--force-window: bypassing process_window_local={window}")

    lock = acquire_watcher_lock()
    if lock is None:
        return

    mapper = load_mapper(cfg)
    host_label = host_label_of(cfg)
    stop_flag = existing_roots[0] / ".audio-inbox-stop"
    try:
        if catch_up_only:
            files = find_audio_files(cfg)
            files = apply_bundle_metadata(files, cfg, mapper)
            log(f"found {len(files)} candidate files (post-bundle filter)")
            for audio, root, source in files:
                if state_path(audio).exists():
                    continue
                pid, _ = route_pid_for_audio(audio, root, source, cfg, mapper)
                started = started_at_for(audio)
                ctx = session_placeholder_ctx(cfg, pid, generate_session_id(audio, started), started)
                transcript_dir = resolve_output_dir(cfg, "transcript_dir", ctx, audio.parent / "Transcripts")
                transcript = existing_transcript(audio, transcript_dir)
                if not transcript:
                    continue
                st = caught_up_state(transcript, pid, host_label)
                atomic_write_json(state_path(audio), st)
                marker_processed_asr(audio).touch()
                log(f"caught-up: {audio.name}")
            return

        # ASR interpreter preflight — a missing/garbled transcribe_python would fail
        # every file and quarantine the whole inbox. Check once up front; abort the
        # sweep (touching nothing) so a config typo never trashes the queue.
        py = _clean_path_value(node_field(cfg, "transcribe_python", None))
        if py and not pathlib.Path(py).expanduser().is_file():
            log(f"transcribe_python not found: {py!r} — fix the node config "
                f"(stray quote/'>' or wrong path/venv). Aborting sweep; no files touched.")
            return

        start_time = time.time()
        budget_sec = (time_budget_minutes * 60) if time_budget_minutes else None
        processed = 0
        # Process each file at most once per sweep. A transient defer or a retry rolls the
        # file back to `queued`; without this guard the re-scan re-picks it immediately, so
        # a persistently-failing input (corrupt / still-syncing) loops forever within one
        # sweep and never lets the tick end. Deferring it to the next tick is exactly the
        # intent ("scheduler will retry next tick").
        attempted_this_sweep: set[str] = set()
        while True:
            if stop_flag.exists():
                log("graceful shutdown via .audio-inbox-stop")
                try:
                    stop_flag.unlink()
                except OSError as exc:
                    log(f"stop-flag unlink failed: {exc}")
                break
            if max_files is not None and processed >= max_files:
                log(f"max-files limit reached ({max_files}); exiting")
                break
            if budget_sec is not None and (time.time() - start_time) > budget_sec:
                log(f"time-budget exhausted ({time_budget_minutes}m); exiting")
                break

            files = find_audio_files(cfg)
            files = apply_bundle_metadata(files, cfg, mapper)
            actionable: list[tuple[pathlib.Path, pathlib.Path, dict]] = []
            for audio, root, source in files:
                if not host_can_process(cfg):
                    continue
                if str(audio) in attempted_this_sweep:
                    continue  # already handled this sweep — defer any re-queue to next tick
                sf = state_path(audio)
                # The per-candidate state/claim reads below are cloud syscalls that can
                # wedge indefinitely on a dataless sidecar with no Errno of their own
                # (observed via `sample`: read() on a *.state.json hung the sweep at 0%
                # CPU *after* the scan itself completed). Guard each candidate with the
                # cloud watchdog: a wedged sidecar is skipped this sweep, not frozen.
                try:
                    with _cloud_watchdog(int(cfg.get("cloud_op_timeout_seconds", 120)),
                                         f"inspect {audio.name}"):
                        if not sf.exists():
                            actionable.append((audio, root, source))
                            continue
                        try:
                            s = json.loads(sf.read_text(encoding="utf-8"))
                        except _CloudOpTimeout:
                            raise                # wedged read -> outer handler skips it
                        except Exception:
                            continue             # garbled/unreadable state -> skip
                        status = s.get("status")
                        if status == "queued":
                            actionable.append((audio, root, source))
                        elif status == "in-progress" and inprogress_recoverable(audio, s, cfg):
                            # Orphaned by a node that died mid-ASR (hard crash / power
                            # loss / host unreachable). reset_stuck + claim-preempt live
                            # in process_one_file, but the queue previously admitted only
                            # queued/no-state, so the orphan was never reached and stayed
                            # stuck forever regardless of lease expiry. Re-queue it here;
                            # the subsequent try_claim_file preempts the dead owner's
                            # stale claim.
                            prev_host = s.get("host")
                            mine = prev_host == host_label_of(cfg) or (
                                not prev_host and not cfg.get("enable_multi_machine"))
                            who = "self" if mine else f"dead peer {prev_host}"
                            s["status"] = "queued"
                            s["last_error"] = f"in-progress reclaimed ({who})"
                            try:
                                atomic_write_json(sf, s)
                                log(f"reclaim in-progress: {audio.name} — blocked by {who}; "
                                    f"re-queued for reprocessing")
                                actionable.append((audio, root, source))
                            except OSError as exc:
                                log(f"reclaim write failed (skip this sweep) {audio.name}: {exc}")
                except _CloudOpTimeout as exc:
                    log(f"state inspect timed out for {audio.name}: {exc}; skipping this sweep")
                    continue
            if not actionable:
                log("queue empty; nothing more to process; exiting cleanly")
                break
            audio, root, source = actionable[0]
            attempted_this_sweep.add(str(audio))
            log(f"queue rebuilt: {len(actionable)} actionable; processing newest: {audio.name}")
            try:
                with _cloud_watchdog(int(cfg.get("cloud_op_timeout_seconds", 120)),
                                     f"pickup {audio.name}"):
                    process_one_file(audio, root, source, cfg, mapper, config_path)
            except _CloudOpTimeout as exc:
                log(f"{exc} — CloudStorage wedged during pickup; aborting sweep "
                    f"(scheduler/launchd will retry next tick). If this recurs, make "
                    f"the Hub available-offline on this node.")
                break
            except AsrEnvironmentError as exc:
                log(f"ASR environment error — aborting sweep; no files quarantined. "
                    f"Fix the ASR env (transcribe_python/venv/deps), then re-run. Detail: {exc}")
                break
            processed += 1
        # OPT-IN tidy: relocate asr-done sources into _processed/ (runs before the index
        # refresh so the index is written from the sidecars in their final location).
        _sweep_processed_moves(cfg, mapper)
        # Refresh per-project session index (processed vs pending) for the hub.
        if cfg.get("write_session_index", True) and cfg.get("hub_root"):
            try:
                hub_root = pathlib.Path(
                    resolve_template(cfg["hub_root"], base_placeholder_ctx(cfg))).expanduser()
                skip = set(cfg.get("discover_skip_names",
                                   ["_inbox", "_shared", "_meta", "_voiceprints", "_archive", "_failed"]))
                if _safe_is_dir_bool(hub_root):
                    for child in sorted(hub_root.iterdir()):
                        if child.name in skip or child.name.startswith(".") or not _safe_is_dir_bool(child):
                            continue
                        _write_project_index(hub_root, child.name, cfg)
            except OSError as exc:
                log(f"session index refresh failed: {exc}")

        log(f"run_once done: processed={processed} in {(time.time()-start_time)/60:.1f}m")
    finally:
        release_watcher_lock(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audio Inbox watcher (generic)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help=f"Path to node config JSON (default: {DEFAULT_CONFIG})")
    parser.add_argument("--once", action="store_true",
                        help="Run a single sweep and exit (the only mode currently)")
    parser.add_argument("--catch-up-only", action="store_true",
                        help="Only mark files with existing transcripts as asr-done; never run ASR")
    parser.add_argument("--time-budget-minutes", type=int, default=None,
                        help="Max wall-clock minutes; exits cleanly between files when exhausted")
    parser.add_argument("--max-files", type=int, default=None,
                        help="Max files to process this run (graceful stop after N)")
    parser.add_argument("--force-window", action="store_true",
                        help="Bypass process_window_local gate for an urgent local run")
    args = parser.parse_args()

    cfg_path = pathlib.Path(args.config).expanduser().resolve()
    if not cfg_path.is_file():
        sys.stderr.write(
            f"config not found: {cfg_path}\n"
            "Copy config/node.example.json -> config/node.local.json and fill in your paths.\n"
        )
        return 2
    cfg = load_config(cfg_path)
    cfg["_config_dir"] = str(cfg_path.parent)

    run_once(cfg, cfg_path,
             catch_up_only=args.catch_up_only,
             time_budget_minutes=args.time_budget_minutes,
             max_files=args.max_files,
             force_window=args.force_window)
    return 0


if __name__ == "__main__":
    sys.exit(main())
