#!/usr/bin/env python3
"""Node status page — a human-readable HTML heartbeat published to the hub.

Why this exists: when a node dies, it dies silently. The 2026-07-23 incident is
the reference case — a node sat dead for an hour on a malformed config while the
queue looked merely "quiet" from every other machine. Logs only help someone
already logged into that box.

So each node publishes its own state to the shared hub, where any machine (or a
phone, straight off the cloud drive) can open it:

    {hub_root}/_status/
        {HOST}.html           this node, human-readable, self-refreshing
        {HOST}.json           the same snapshot, machine-readable
        {HOST}.history.jsonl  append-only event log (sweeps, files, crashes)

Design notes
------------
* **One file per node.** Nodes never write to each other's files, so two watchers
  on one cloud drive cannot corrupt a shared document. A fleet-wide index can be
  assembled later purely by *reading* the sibling ``*.json`` files.
* **``_status`` is skipped by the scanner** — the ``_`` prefix is already in
  ``skip_folder_prefixes``, so published pages never re-enter the queue as input.
* **Self-reporting death.** A config that fails to parse takes down the watcher
  before it knows where the hub is, so the hub path from the last good start is
  cached next to the config (``.last-known-hub``) and used to publish the crash.
* **Incident banner.** The event log makes "was there a defect before this?"
  answerable: on recovery the page shows what failed, for how long the node was
  down, and how many ticks were lost — then clears itself on the next clean run.
* No third-party deps, inline CSS, no external requests: the page has to render
  from a cloud-drive folder with no network.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import pathlib
import socket
import tempfile

STATUS_DIR_NAME = "_status"
HISTORY_KEEP_EVENTS = 4000       # trim the log so a long-lived node stays bounded
HISTORY_READ_TAIL = 2000         # events scanned when rendering a page
REFRESH_SECONDS = 30
HUB_HINT_FILENAME = ".last-known-hub"


# ---------------------------------------------------------------------------
# Small IO helpers (kept local: this module must import cleanly on its own)
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now()


def _iso(moment: dt.datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-status-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Hub-path memory: lets a node report a failure that happened before it could
# read its own config.
# ---------------------------------------------------------------------------


def remember_hub(config_path: pathlib.Path, hub_root: str, host: str | None = None) -> None:
    """Cache hub path + host label from a successful start.

    Both are needed to report a crash: a config that fails to parse yields neither
    ``hub_root`` (where to publish) nor ``host_label`` (which page is ours), and
    falling back to the machine hostname would publish a second, differently-named
    page for the same node.
    """
    try:
        payload = {"hub_root": str(hub_root), "host": host}
        (pathlib.Path(config_path).parent / HUB_HINT_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def recall_hub(config_path: pathlib.Path) -> tuple[str | None, str | None]:
    """Return (hub_root, host) from the cache. Tolerates the old plain-text form."""
    try:
        raw = (pathlib.Path(config_path).parent / HUB_HINT_FILENAME).read_text(
            encoding="utf-8").strip()
    except OSError:
        return (None, None)
    if not raw:
        return (None, None)
    try:
        data = json.loads(raw)
        return (data.get("hub_root") or None, data.get("host") or None)
    except ValueError:
        return (raw, None)  # legacy: file held just the path


def status_dir_for(hub_root: str | pathlib.Path | None) -> pathlib.Path | None:
    if not hub_root:
        return None
    return pathlib.Path(str(hub_root)).expanduser() / STATUS_DIR_NAME


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def append_event(status_dir: pathlib.Path | None, host: str, event: dict) -> None:
    """Append one event. Never raises: status publishing must not break a sweep."""
    if status_dir is None:
        return
    record = {"ts": _iso(), **event}
    path = status_dir / f"{host}.history.jsonl"
    try:
        status_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return
    _trim_history(path)


def _trim_history(path: pathlib.Path) -> None:
    try:
        if path.stat().st_size < 1_500_000:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= HISTORY_KEEP_EVENTS:
            return
        _atomic_write_text(path, "\n".join(lines[-HISTORY_KEEP_EVENTS:]) + "\n")
    except OSError:
        return


def read_history(status_dir: pathlib.Path | None, host: str) -> list[dict]:
    if status_dir is None:
        return []
    path = status_dir / f"{host}.history.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-HISTORY_READ_TAIL:]:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


# ---------------------------------------------------------------------------
# Derived views over the event log
# ---------------------------------------------------------------------------


def summarize_day(events: list[dict], day: dt.date) -> dict:
    """Files finished on ``day`` with media seconds vs machine seconds."""
    done, media_sec, proc_sec, frames = 0, 0.0, 0.0, 0
    projects: dict[str, int] = {}
    for event in events:
        if event.get("type") != "file_done":
            continue
        moment = _parse(event.get("ts"))
        if not moment or moment.date() != day:
            continue
        done += 1
        media_sec += float(event.get("media_sec") or 0)
        proc_sec += float(event.get("proc_sec") or 0)
        frames += int(event.get("frames") or 0)
        pid = str(event.get("pid") or "?")
        projects[pid] = projects.get(pid, 0) + 1
    return {"done": done, "media_sec": media_sec, "proc_sec": proc_sec,
            "frames": frames, "projects": projects}


def detect_incident(events: list[dict]) -> dict | None:
    """The unresolved failure story, if the node is recovering from one.

    Returns the last crash/error and how long the node was down, unless a clean
    sweep has completed since — in which case the banner has served its purpose
    and disappears.
    """
    last_crash = None
    last_ok_end = None
    crash_count = 0
    for event in events:
        kind = event.get("type")
        if kind in ("crash", "sweep_error"):
            if last_crash is None or (_parse(event.get("ts")) or _now()) >= (
                    _parse(last_crash.get("ts")) or _now()):
                last_crash = event
            crash_count += 1
        elif kind == "sweep_end":
            last_ok_end = event
            crash_count = 0
    if not last_crash:
        return None
    crash_at = _parse(last_crash.get("ts"))
    ok_at = _parse(last_ok_end.get("ts")) if last_ok_end else None
    if crash_at and ok_at and ok_at > crash_at:
        return None  # already recovered and reported
    return {
        "at": last_crash.get("ts"),
        "reason": last_crash.get("reason") or last_crash.get("error") or "unknown",
        "detail": last_crash.get("detail") or "",
        "count": crash_count,
        "down_for_sec": (_now() - crash_at).total_seconds() if crash_at else None,
    }


def last_recovery(events: list[dict], within_hours: float = 24.0) -> dict | None:
    """The most recent *closed* failure story: node broke, then a sweep succeeded.

    ``detect_incident`` deliberately goes quiet once the node works again, but the
    fact that it was down still matters for a while — that is the "дополнять
    статусом, если был предыдущий дефект" case. This returns the last completed
    crash→recovery pair, and only while it is recent enough to be worth showing.
    """
    pending_crash: dict | None = None
    recovery: dict | None = None
    for event in events:
        kind = event.get("type")
        if kind in ("crash", "sweep_error"):
            pending_crash = event
        elif kind == "sweep_end" and pending_crash is not None:
            crash_at = _parse(pending_crash.get("ts"))
            ok_at = _parse(event.get("ts"))
            recovery = {
                "crash_at": pending_crash.get("ts"),
                "recovered_at": event.get("ts"),
                "reason": pending_crash.get("reason") or "unknown",
                "gap_sec": (ok_at - crash_at).total_seconds() if (crash_at and ok_at) else None,
            }
            pending_crash = None
    if not recovery:
        return None
    recovered_at = _parse(recovery.get("recovered_at"))
    if recovered_at and (_now() - recovered_at).total_seconds() > within_hours * 3600:
        return None
    return recovery


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f} с"
    if seconds < 3600:
        return f"{seconds / 60:.0f} мин"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours} ч {minutes:02d} мин"


def fmt_size(num_bytes: float | None) -> str:
    if not num_bytes:
        return "—"
    mb = float(num_bytes) / 1024 / 1024
    return f"{mb / 1024:.1f} ГБ" if mb >= 1024 else f"{mb:.0f} МБ"


def fmt_ago(moment: dt.datetime | None) -> str:
    if not moment:
        return "—"
    return fmt_duration((_now() - moment).total_seconds())


def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


# ---------------------------------------------------------------------------
# Snapshot + rendering
# ---------------------------------------------------------------------------


def build_snapshot(*, host: str, phase: str, cfg: dict | None = None,
                   current: dict | None = None, queue: dict | None = None,
                   sweep: dict | None = None, events: list[dict] | None = None,
                   note: str | None = None) -> dict:
    cfg = cfg or {}
    runtime = cfg.get("runtime") or {}
    node = cfg.get("node") or {}
    events = events or []
    return {
        "host": host,
        "phase": phase,                      # running | idle | crashed | starting
        "updated_at": _iso(),
        "note": note,
        "runtime": {
            "device": runtime.get("device"),
            "compute_type": runtime.get("compute_type"),
            "diarization_device": runtime.get("diarization_device"),
            "capabilities": node.get("capabilities") or [],
            "quality_preset": cfg.get("quality_preset"),
            "speaker_mode": cfg.get("speaker_mode"),
            "slides_enabled": bool((cfg.get("video_frames") or {}).get("mode")
                                   not in (None, "", "off", "none")),
            "zoom_vtt_autodetect": cfg.get("zoom_vtt_autodetect", True),
        },
        "current": current,
        "queue": queue or {},
        "sweep": sweep or {},
        "today": summarize_day(events, _now().date()),
        "incident": detect_incident(events),
        "recovery": last_recovery(events),
    }


_CSS = """
:root{--bg:#f6f7f9;--fg:#14171a;--muted:#5b6570;--card:#fff;--line:#e3e7eb;
--ok:#1a7f45;--warn:#9a6b00;--bad:#b3261e;--accent:#1f5fa8}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--fg:#e8eaed;--muted:#9aa5b1;
--card:#1d2126;--line:#2c323a;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--accent:#7cb2f0}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:14px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);margin:0 0 12px;font-weight:600}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;
font-weight:600;vertical-align:middle}
.b-run{background:rgba(31,95,168,.14);color:var(--accent)}
.b-idle{background:rgba(91,101,112,.16);color:var(--muted)}
.b-bad{background:rgba(179,38,30,.14);color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.kv{font-size:13px}
.kv .k{color:var(--muted);display:block;margin-bottom:2px}
.kv .v{font-weight:600;font-size:15px;word-break:break-word}
.big{font-size:26px;font-weight:700;line-height:1.2}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:600;padding:6px 8px 6px 0;
border-bottom:1px solid var(--line)}
td{padding:7px 8px 7px 0;border-bottom:1px solid var(--line);word-break:break-word}
tr:last-child td{border-bottom:none}
.num{text-align:right}
.incident{border-left:4px solid var(--bad)}
.incident h2{color:var(--bad)}
.recovered{border-left:4px solid var(--warn)}
.recovered h2{color:var(--warn)}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
.scroll{overflow-x:auto}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:20px}
"""


def render_html(snap: dict) -> str:
    phase = snap.get("phase") or "idle"
    badge = {"running": ("b-run", "работает"), "idle": ("b-idle", "простаивает"),
             "crashed": ("b-bad", "УПАЛ"), "starting": ("b-run", "запускается")}.get(
                 phase, ("b-idle", phase))
    rt = snap.get("runtime") or {}
    today = snap.get("today") or {}
    current = snap.get("current")
    queue = snap.get("queue") or {}
    incident = snap.get("incident")
    parts: list[str] = []

    parts.append(f"""<div class="wrap">
<h1>{_esc(snap.get('host'))} <span class="badge {badge[0]}">{badge[1]}</span></h1>
<div class="sub">обновлено {_esc(snap.get('updated_at'))}"""
                 + (f" · {_esc(snap.get('note'))}" if snap.get("note") else "")
                 + "</div>")

    if incident:
        down = fmt_duration(incident.get("down_for_sec"))
        parts.append(f"""<div class="card incident">
<h2>Сбой узла</h2>
<div class="kv"><span class="k">Причина</span><span class="v">{_esc(incident.get('reason'))}</span></div>
<div class="grid" style="margin-top:12px">
<div class="kv"><span class="k">Началось</span><span class="v">{_esc(incident.get('at'))}</span></div>
<div class="kv"><span class="k">Не работает</span><span class="v">{_esc(down)}</span></div>
<div class="kv"><span class="k">Неудачных тактов</span><span class="v">{_esc(incident.get('count'))}</span></div>
</div>"""
                     + (f'<div class="mono" style="margin-top:10px">{_esc(incident.get("detail"))}</div>'
                        if incident.get("detail") else "")
                     + "</div>")

    recovery = snap.get("recovery")
    if recovery and not incident:
        parts.append(f"""<div class="card recovered">
<h2>Восстановлен после сбоя</h2>
<div class="kv"><span class="k">Что было</span><span class="v">{_esc(recovery.get('reason'))}</span></div>
<div class="grid" style="margin-top:12px">
<div class="kv"><span class="k">Сбой</span><span class="v">{_esc(recovery.get('crash_at'))}</span></div>
<div class="kv"><span class="k">Снова в строю</span><span class="v">{_esc(recovery.get('recovered_at'))}</span></div>
<div class="kv"><span class="k">Простой</span><span class="v">{_esc(fmt_duration(recovery.get('gap_sec')))}</span></div>
</div>
</div>""")

    if current:
        parts.append(f"""<div class="card">
<h2>Сейчас обрабатывается</h2>
<div class="kv"><span class="k">Файл</span><span class="v">{_esc(current.get('name'))}</span></div>
<div class="grid" style="margin-top:12px">
<div class="kv"><span class="k">Проект</span><span class="v">{_esc(current.get('pid'))}</span></div>
<div class="kv"><span class="k">Размер</span><span class="v">{_esc(fmt_size(current.get('size')))}</span></div>
<div class="kv"><span class="k">Идёт</span><span class="v">{_esc(fmt_ago(_parse(current.get('started_at'))))}</span></div>
<div class="kv"><span class="k">Спикеры</span><span class="v">{_esc(current.get('speaker_source') or '—')}</span></div>
</div>
<div class="kv" style="margin-top:12px"><span class="k">Сессия</span>
<span class="v mono">{_esc(current.get('session_id'))}</span></div>
</div>""")

    proj = today.get("projects") or {}
    proj_line = ", ".join(f"{k} × {v}" for k, v in sorted(proj.items(), key=lambda kv: -kv[1])) or "—"
    media, proc = today.get("media_sec") or 0, today.get("proc_sec") or 0
    ratio = f"{proc / media:.1f}× к длительности" if media > 0 and proc > 0 else "—"
    parts.append(f"""<div class="card">
<h2>Сделано за сегодня</h2>
<div class="grid">
<div class="kv"><span class="k">Файлов</span><span class="v big">{_esc(today.get('done', 0))}</span></div>
<div class="kv"><span class="k">Записей</span><span class="v big">{_esc(fmt_duration(media))}</span></div>
<div class="kv"><span class="k">Машинного времени</span><span class="v big">{_esc(fmt_duration(proc))}</span></div>
<div class="kv"><span class="k">Кадров слайдов</span><span class="v big">{_esc(today.get('frames', 0))}</span></div>
</div>
<div class="grid" style="margin-top:14px">
<div class="kv"><span class="k">Скорость</span><span class="v">{_esc(ratio)}</span></div>
<div class="kv"><span class="k">По проектам</span><span class="v">{_esc(proj_line)}</span></div>
</div>
</div>""")

    items = queue.get("items") or []
    parts.append(f"""<div class="card">
<h2>Очередь — {_esc(queue.get('count', 0))} шт., {_esc(fmt_size(queue.get('bytes')))}</h2>""")
    if items:
        rows = "".join(
            f"<tr><td>{_esc(i.get('name'))}</td><td>{_esc(i.get('pid'))}</td>"
            f"<td class='num'>{_esc(fmt_size(i.get('size')))}</td>"
            f"<td class='num'>{_esc(fmt_ago(_parse(i.get('mtime'))))}</td></tr>"
            for i in items[:15])
        parts.append('<div class="scroll"><table><tr><th>Файл</th><th>Проект</th>'
                     "<th class='num'>Размер</th><th class='num'>Ждёт</th></tr>"
                     + rows + "</table></div>")
        if len(items) > 15:
            parts.append(f'<div class="sub" style="margin:10px 0 0">…ещё {len(items) - 15}</div>')
    else:
        parts.append('<div class="sub" style="margin:0">пусто</div>')
    parts.append("</div>")

    caps = ", ".join(rt.get("capabilities") or []) or "—"
    parts.append(f"""<div class="card">
<h2>Узел</h2>
<div class="grid">
<div class="kv"><span class="k">Вычислитель</span><span class="v">{_esc(rt.get('device'))} / {_esc(rt.get('compute_type'))}</span></div>
<div class="kv"><span class="k">Диаризация</span><span class="v">{_esc(rt.get('diarization_device'))}</span></div>
<div class="kv"><span class="k">Модель</span><span class="v">{_esc(rt.get('quality_preset'))}</span></div>
<div class="kv"><span class="k">Слайды</span><span class="v">{'включены' if rt.get('slides_enabled') else 'выключены'}</span></div>
<div class="kv"><span class="k">Подхват VTT</span><span class="v">{'да' if rt.get('zoom_vtt_autodetect') else 'нет'}</span></div>
<div class="kv"><span class="k">Возможности</span><span class="v">{_esc(caps)}</span></div>
</div>
</div>""")

    parts.append(f'<div class="foot">страница обновляется сама раз в {REFRESH_SECONDS} с</div></div>')
    body = "\n".join(parts)
    return (f"<!doctype html>\n<html lang=\"ru\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta http-equiv=\"refresh\" content=\"{REFRESH_SECONDS}\">"
            f"<title>{_esc(snap.get('host'))} — узел ASR</title>"
            f"<style>{_CSS}</style></head><body>\n{body}\n</body></html>\n")


# ---------------------------------------------------------------------------
# Public entry points used by the watcher
# ---------------------------------------------------------------------------


def publish(status_dir: pathlib.Path | None, snapshot: dict) -> None:
    """Write {HOST}.json + {HOST}.html. Never raises."""
    if status_dir is None:
        return
    host = snapshot.get("host") or socket.gethostname()
    try:
        status_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(status_dir / f"{host}.json",
                           json.dumps(snapshot, ensure_ascii=False, indent=2))
        _atomic_write_text(status_dir / f"{host}.html", render_html(snapshot))
    except Exception:
        return  # a status page is never worth failing a sweep for


def publish_crash(config_path: pathlib.Path, reason: str, detail: str = "",
                  host: str | None = None) -> pathlib.Path | None:
    """Report a failure that happened before/outside a normal sweep.

    Uses the cached hub path and host label so a node that cannot even parse its
    config still tells the fleet why it is gone — under its own name.
    """
    cached_hub, cached_host = recall_hub(config_path)
    host = host or cached_host or socket.gethostname()
    status_dir = status_dir_for(cached_hub)
    if status_dir is None:
        return None
    append_event(status_dir, host, {"type": "crash", "reason": reason, "detail": detail})
    events = read_history(status_dir, host)
    snapshot = build_snapshot(host=host, phase="crashed", events=events,
                              note=reason)
    publish(status_dir, snapshot)
    return status_dir
