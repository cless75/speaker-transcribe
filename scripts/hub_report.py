#!/usr/bin/env python3
"""Hub report — what the ASR fleet finished, what it is running, what is waiting.

Read-only. Reads the same node config the watcher uses, walks the configured
sources for per-file sidecars (``<file>.state.json``) and the canonical session
state under ``{hub}/{pid}/sessions/{YYYY-MM}/{sid}/pipeline/state.json``, and
prints a daily summary.

Answers, per day:

* what completed — per project, per host, media hours vs processing hours
* what is running right now — and whether the owning node is still heartbeating
  (an expired claim lease is the signal a node died mid-file)
* what is waiting — backlog with age, so a file stuck behind a long job is
  visible instead of looking like a silent failure
* what failed — attempts, last error, quarantined files under ``_failed/``

Usage::

    python scripts/hub_report.py                       # today
    python scripts/hub_report.py --date 2026-07-22
    python scripts/hub_report.py --days 7              # last 7 days, per-day rollup
    python scripts/hub_report.py --markdown report.md  # also write a Markdown copy
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import audio_inbox_watch as w  # noqa: E402

DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config" / "node.local.json"

# Statuses that mean "this file is done and needs nothing further".
TERMINAL_OK = {"asr-done", "insights-done"}


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _parse_local(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _safe_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _safe_size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def collect_files(cfg: dict) -> list[dict]:
    """Every media file the watcher would consider, joined with its sidecar state."""
    mapper = w.load_mapper(cfg)
    rows: list[dict] = []
    for audio, root, source in w.find_audio_files(cfg):
        pid, _ = w.route_pid_for_audio(audio, root, source, cfg, mapper)
        state_file = w.state_path(audio)
        state = _safe_json(state_file) if state_file.exists() else {}
        claim = _safe_json(w.claim_path(audio)).get("claim") or {}
        rows.append({
            "path": audio,
            "name": audio.name,
            "pid": state.get("pid") or pid,
            "size": _safe_size(audio),
            "mtime": dt.datetime.fromtimestamp(w._safe_mtime(audio)),
            "status": state.get("status") or "untouched",
            "host": state.get("host") or "",
            "attempts": state.get("attempts") or 0,
            "started_at": _parse_local(state.get("started_at")),
            "finished_at": _parse_local(state.get("finished_at")),
            "duration_sec": state.get("duration_sec") or 0,
            "last_error": state.get("last_error") or "",
            "session_id": state.get("session_id") or "",
            "transcript": state.get("transcript_path") or "",
            "claim": claim,
        })
    return rows


def collect_failed_dirs(cfg: dict) -> list[dict]:
    """Files quarantined under ``_failed/`` (they are skipped by the normal scan)."""
    out: list[dict] = []
    extensions = {e.lower() for e in cfg.get("scan_extensions", w.DEFAULT_SCAN_EXTENSIONS)}
    seen: set[pathlib.Path] = set()
    for root, _source, _rec in w.expand_sources(cfg):
        failed = root / "_failed"
        if failed in seen or not w._safe_is_dir_bool(failed):
            continue
        seen.add(failed)
        try:
            entries = list(failed.iterdir())
        except OSError:
            continue
        for item in entries:
            if not item.is_file() or item.suffix.lower() not in extensions:
                continue
            error_file = failed / f"{item.name}.error.txt"
            tail = ""
            if error_file.is_file():
                try:
                    tail = error_file.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    tail = ""
            out.append({
                "path": item,
                "name": item.name,
                "size": _safe_size(item),
                "error": (tail.splitlines() or [""])[-1][:160],
            })
    return out


def collect_sessions(cfg: dict, months: set[str]) -> list[dict]:
    """Canonical per-session state under ``{pid}/sessions/{YYYY-MM}/{sid}/pipeline``."""
    hub_root = pathlib.Path(
        w.resolve_template(cfg.get("hub_root", ""), w.base_placeholder_ctx(cfg))).expanduser()
    if not w._safe_is_dir_bool(hub_root):
        return []
    skip = set(cfg.get("discover_skip_names",
                       ["_inbox", "_shared", "_meta", "_voiceprints", "_archive", "_failed"]))
    out: list[dict] = []
    try:
        projects = sorted(hub_root.iterdir())
    except OSError:
        return []
    for project in projects:
        if project.name in skip or project.name.startswith(".") or not w._safe_is_dir_bool(project):
            continue
        for month in months:
            month_dir = project / "sessions" / month
            if not w._safe_is_dir_bool(month_dir):
                continue
            try:
                sessions = sorted(month_dir.iterdir())
            except OSError:
                continue
            for session in sessions:
                state_file = session / "pipeline" / "state.json"
                if not state_file.is_file():
                    continue
                state = _safe_json(state_file)
                transcripts = session / "transcripts"
                md = sorted(transcripts.glob("*-transcript.md")) if w._safe_is_dir_bool(transcripts) else []
                frames = transcripts / (cfg.get("video_frames", {}) or {}).get("frames_dir", "frames")
                frame_count = len(list(frames.glob("*.png"))) if w._safe_is_dir_bool(frames) else 0
                out.append({
                    "pid": project.name,
                    "sid": session.name,
                    "status": state.get("status") or "?",
                    "host": state.get("host") or "",
                    "finished_at": _parse_local(state.get("finished_at")),
                    "started_at": _parse_local(state.get("started_at")),
                    "duration_sec": state.get("duration_sec") or 0,
                    "transcripts": len(md),
                    "frames": frame_count,
                })
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_dur(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def fmt_mb(size: int) -> str:
    return f"{size / 1024 / 1024:.0f}M" if size else "-"


def fmt_age(moment: dt.datetime | None, now: dt.datetime) -> str:
    if not moment:
        return "-"
    delta = (now - moment).total_seconds()
    if delta < 3600:
        return f"{delta / 60:.0f}m"
    if delta < 86400:
        return f"{delta / 3600:.0f}h"
    return f"{delta / 86400:.0f}d"


def build_report(cfg: dict, target: dt.date, days: int) -> list[str]:
    now = dt.datetime.now()
    window_start = target - dt.timedelta(days=days - 1)
    months = {
        (window_start + dt.timedelta(days=offset)).strftime("%Y-%m")
        for offset in range((target - window_start).days + 1)
    }

    files = collect_files(cfg)
    sessions = collect_sessions(cfg, months)
    quarantined = collect_failed_dirs(cfg)

    lines: list[str] = []
    label = str(target) if days == 1 else f"{window_start} .. {target}"
    lines.append(f"# Hub report — {label}")
    lines.append(f"hub: {cfg.get('hub_root')}   this node: {w.host_label_of(cfg)}   generated {now:%Y-%m-%d %H:%M}")
    lines.append("")

    def in_window(moment: dt.datetime | None) -> bool:
        return bool(moment) and window_start <= moment.date() <= target

    # --- completed -----------------------------------------------------------
    done = [f for f in files if f["status"] in TERMINAL_OK and in_window(f["finished_at"])]
    media_sec = sum(float(f["duration_sec"] or 0) for f in done)
    proc_sec = sum(
        (f["finished_at"] - f["started_at"]).total_seconds()
        for f in done if f["started_at"] and f["finished_at"]
        and f["finished_at"] >= f["started_at"]
    )
    lines.append(f"## Обработано: {len(done)} файлов")
    if done:
        ratio = f", {proc_sec / media_sec:.1f}x realtime" if media_sec > 0 and proc_sec > 0 else ""
        lines.append(f"медиа {fmt_dur(media_sec)} -> машинное время {fmt_dur(proc_sec)}{ratio}")
        lines.append("")
        by_host: dict[str, list] = collections.defaultdict(list)
        for f in done:
            by_host[f["host"] or "?"].append(f)
        lines.append("| узел | файлов | медиа | обработка |")
        lines.append("|---|---:|---:|---:|")
        for host, group in sorted(by_host.items(), key=lambda kv: -len(kv[1])):
            group_media = sum(float(g["duration_sec"] or 0) for g in group)
            group_proc = sum(
                (g["finished_at"] - g["started_at"]).total_seconds()
                for g in group if g["started_at"] and g["finished_at"]
                and g["finished_at"] >= g["started_at"]
            )
            lines.append(f"| {host} | {len(group)} | {fmt_dur(group_media)} | {fmt_dur(group_proc)} |")
        lines.append("")
        by_pid: dict[str, list] = collections.defaultdict(list)
        for f in done:
            by_pid[str(f["pid"])].append(f)
        lines.append("| проект | файлов | медиа |")
        lines.append("|---|---:|---:|")
        for pid, group in sorted(by_pid.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"| {pid} | {len(group)} | {fmt_dur(sum(float(g['duration_sec'] or 0) for g in group))} |")
    else:
        lines.append("_за период ничего не завершено_")
    lines.append("")

    # --- running -------------------------------------------------------------
    running = [f for f in files if f["status"] == "in-progress"]
    lines.append(f"## Сейчас в работе: {len(running)}")
    if running:
        lines.append("| файл | проект | узел | идёт | claim |")
        lines.append("|---|---|---|---:|---|")
        lease_minutes = int(cfg.get("claim_lease_minutes", 30))
        for f in sorted(running, key=lambda x: x["started_at"] or now):
            claim = f["claim"]
            if not claim:
                verdict = "нет claim"
            elif w.is_claim_expired(claim, w.utcnow(), lease_minutes):
                verdict = "ПРОСРОЧЕН — узел молчит"
            else:
                verdict = "живой"
            lines.append(
                f"| {f['name'][:44]} | {f['pid']} | {f['host'] or '?'} | "
                f"{fmt_age(f['started_at'], now)} | {verdict} |")
    else:
        lines.append("_ничего не выполняется_")
    lines.append("")

    # --- backlog -------------------------------------------------------------
    waiting = [f for f in files if f["status"] in ("untouched", "queued")]
    waiting.sort(key=lambda f: f["mtime"], reverse=True)
    backlog_bytes = sum(f["size"] for f in waiting)
    lines.append(f"## В очереди: {len(waiting)} ({fmt_mb(backlog_bytes)})")
    if waiting:
        lines.append("| файл | проект | размер | лежит | статус |")
        lines.append("|---|---|---:|---:|---|")
        for f in waiting[:25]:
            lines.append(
                f"| {f['name'][:44]} | {f['pid']} | {fmt_mb(f['size'])} | "
                f"{fmt_age(f['mtime'], now)} | {f['status']} |")
        if len(waiting) > 25:
            lines.append(f"| … ещё {len(waiting) - 25} | | | | |")
    else:
        lines.append("_очередь пуста_")
    lines.append("")

    # --- problems ------------------------------------------------------------
    # A successful run also leaves attempts=1 (it is incremented at pickup, not on
    # error), so "attempts > 0" alone is not a failure signal — only a retry on a file
    # that has not reached a terminal-OK status is.
    problems = [
        f for f in files
        if f["status"] == "failed"
        or (f["last_error"] and f["status"] not in TERMINAL_OK)
        or ((f["attempts"] or 0) > 1 and f["status"] not in TERMINAL_OK)
    ]
    lines.append(f"## Проблемы: {len(problems)} в очереди + {len(quarantined)} в _failed/")
    if problems:
        lines.append("| файл | проект | статус | попыток | ошибка |")
        lines.append("|---|---|---|---:|---|")
        for f in problems[:15]:
            lines.append(
                f"| {f['name'][:38]} | {f['pid']} | {f['status']} | {f['attempts']} | "
                f"{(f['last_error'] or '')[:60]} |")
    for f in quarantined[:15]:
        lines.append(f"- `_failed/{f['name'][:50]}` ({fmt_mb(f['size'])}) — {f['error'][:80]}")
    if not problems and not quarantined:
        lines.append("_нет_")
    lines.append("")

    # --- sessions written ----------------------------------------------------
    fresh = [s for s in sessions if in_window(s["finished_at"]) or in_window(s["started_at"])]
    total_frames = sum(s["frames"] for s in fresh)
    lines.append(f"## Сессии за период: {len(fresh)} (кадров слайдов: {total_frames})")
    if fresh:
        lines.append("| проект | сессия | статус | транскриптов | кадров |")
        lines.append("|---|---|---|---:|---:|")
        for s in sorted(fresh, key=lambda x: x["finished_at"] or x["started_at"] or now, reverse=True):
            lines.append(
                f"| {s['pid']} | {s['sid'][:40]} | {s['status']} | {s['transcripts']} | "
                f"{s['frames'] or '-'} |")
    else:
        lines.append("_нет_")
    lines.append("")

    # --- fleet ---------------------------------------------------------------
    lines.append("## Итого по Hub")
    counter = collections.Counter(f["status"] for f in files)
    lines.append(f"всего медиафайлов в источниках: {len(files)}")
    lines.append("статусы: " + ", ".join(f"{k}={v}" for k, v in counter.most_common()))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Hub daily ASR report (read-only)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=1, help="window size ending at --date")
    parser.add_argument("--markdown", default=None, help="also write the report to this path")
    args = parser.parse_args()

    cfg_path = pathlib.Path(args.config).expanduser().resolve()
    if not cfg_path.is_file():
        sys.stderr.write(f"config not found: {cfg_path}\n")
        return 2
    cfg = w.load_config(cfg_path)
    cfg["_config_dir"] = str(cfg_path.parent)

    target = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    lines = build_report(cfg, target, max(1, args.days))
    text = "\n".join(lines)
    sys.stdout.write(text + "\n")
    if args.markdown:
        out = pathlib.Path(args.markdown).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        sys.stderr.write(f"\nwritten: {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
