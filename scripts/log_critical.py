#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""801 — выжимка критических моментов из лога узла в Hub _meta.

Локальный лог узла (C:\\work\\logs\\watch.log) замусорен построчной механикой —
глазами не найти, что реально пошло не так. Этот скрипт сканирует лог, вытаскивает
значимые события (сбои, откаты, аборты, застревания) с таймкодами, группирует по
важности и пишет компактную сводку в общий Hub, чтобы её было видно с любой машины:

    {hub_root}/_meta/801-logcrit-{HOST}.txt

Ничего не меняет — только читает лог и пишет сводку. Паттерны — по реальным
сигналам движка (ffmpeg exit 69 на webm, DIARIZATION FAILED, aborting sweep,
JSONDecodeError конфига, EDEADLK/timed out облака, cuda_unavailable, reclaim/stuck).

Запуск на узле:
    python scripts/log_critical.py
    python scripts/log_critical.py --log C:\\work\\logs\\watch.log --hours 12
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import socket
import sys

# (regex, уровень, короткий ярлык). Порядок = приоритет при нескольких совпадениях.
PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"Traceback \(most recent call last\)"), "CRITICAL", "python traceback"),
    (re.compile(r"JSONDecodeError|config (?:unreadable|not found)|конфиг не читается"), "CRITICAL", "конфиг не читается"),
    (re.compile(r"watcher exit code (?!0\b)\d+"), "CRITICAL", "watcher упал (exit≠0)"),
    (re.compile(r"ASR environment error|AsrEnvironmentError|interpreter"), "CRITICAL", "среда ASR (venv/интерпретатор)"),
    (re.compile(r"aborting sweep|Aborting sweep|aborting the sweep"), "CRITICAL", "прогон прерван"),
    (re.compile(r"moved? to _failed|move_to_failed|quarantin"), "CRITICAL", "файл в карантин _failed"),
    (re.compile(r"DIARIZATION FAILED"), "WARNING", "диаризация упала → только ASR"),
    (re.compile(r"exit status 69|Invalid data found|Conversion failed"), "WARNING", "ffmpeg/webm сбой (VP8 seek)"),
    (re.compile(r"slide_frames_capture_failed|slide_frames_stage_failed"), "WARNING", "захват кадра слайда упал"),
    (re.compile(r"speaker_clip_failed"), "WARNING", "нарезка speaker-clip упала"),
    (re.compile(r"cuda_unavailable|move_to_cuda_failed|no CUDA-capable device"), "WARNING", "CUDA недоступна → CPU"),
    (re.compile(r"EDEADLK|resource deadlock|CloudStorage wedged|scan timed out|operation timed out"), "WARNING", "облачный диск подвис (GDrive)"),
    (re.compile(r"voiceprint.*(pending|unavailable)|enroll не"), "WARNING", "voiceprint не отработал"),
    (re.compile(r"zoom_vtt.*(fallback|parse_failed)"), "WARNING", "VTT не распарсился → pyannote"),
    (re.compile(r"reclaim in-progress|stuck >|stuck_threshold|reset"), "NOTABLE", "перехват зависшей работы"),
    (re.compile(r"claim.*(expired|preempt)"), "NOTABLE", "перехват claim у другого узла"),
    (re.compile(r"graceful shutdown via \.audio-inbox-stop"), "NOTABLE", "остановлен стоп-флагом"),
    (re.compile(r"device=cpu compute=int8"), "NOTABLE", "ASR идёт на CPU"),
]
LEVELS = ["CRITICAL", "WARNING", "NOTABLE"]

_TS = re.compile(r"^\[media-transcription\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
                 r"|^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def line_time(line: str) -> dt.datetime | None:
    m = _TS.search(line)
    if not m:
        return None
    stamp = m.group(1) or m.group(2)
    try:
        return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def scan(lines: list[str], since: dt.datetime | None) -> tuple[list[dict], dict]:
    events: list[dict] = []
    counts = {lvl: 0 for lvl in LEVELS}
    last_ts: dt.datetime | None = None
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        ts = line_time(raw)
        if ts:
            last_ts = ts
        # окно по времени: пропускаем события старше since (по последнему известному ts)
        in_window = (since is None) or (last_ts is None) or (last_ts >= since)
        for pat, level, label in PATTERNS:
            if pat.search(raw):
                if not in_window:
                    break
                snippet = raw.strip()
                # для traceback подтянуть последнюю строку исключения (суть ошибки)
                if label == "python traceback":
                    tail = _traceback_tail(lines, i)
                    if tail:
                        snippet = f"{snippet} … {tail}"
                events.append({"ts": last_ts.strftime("%m-%d %H:%M") if last_ts else "??",
                               "level": level, "label": label, "text": snippet[:200]})
                counts[level] += 1
                break  # одно совпадение на строку (по приоритету)
        i += 1
    return events, counts


def _traceback_tail(lines: list[str], start: int) -> str:
    """Последняя непустая строка traceback-блока = тип+сообщение исключения."""
    last = ""
    for j in range(start + 1, min(start + 60, len(lines))):
        s = lines[j].strip()
        if not s or line_time(lines[j]):
            break
        if re.match(r"^[A-Za-z_.]+(Error|Exception|Warning):", s):
            last = s
    return last[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=r"C:\work\logs\watch.log", help="путь к логу узла")
    ap.add_argument("--repo", default=r"C:\work\speaker-transcribe", help="репо (для hub_root/host из конфига)")
    ap.add_argument("--hours", type=float, default=24.0, help="окно анализа в часах (0 = весь лог)")
    ap.add_argument("--max-lines", type=int, default=200000, help="читать последние N строк лога")
    args = ap.parse_args()

    log_path = pathlib.Path(args.log)
    if not log_path.is_file():
        sys.stderr.write(f"лог не найден: {log_path}\n")
        return 2
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        sys.stderr.write(f"не прочитать лог: {exc}\n")
        return 2
    if len(lines) > args.max_lines:
        lines = lines[-args.max_lines:]

    since = None
    now = dt.datetime.now()
    if args.hours > 0:
        since = now - dt.timedelta(hours=args.hours)

    events, counts = scan(lines, since)

    # host_label + hub_root из конфига (для имени файла и места записи)
    host = socket.gethostname()
    hub_meta: pathlib.Path | None = None
    cfg_path = pathlib.Path(args.repo) / "config" / "node.local.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            host = (cfg.get("node") or {}).get("host_label") or host
            hub = cfg.get("hub_root")
            if hub:
                cand = pathlib.Path(str(hub)).expanduser() / "_meta"
                if cand.parent.exists():
                    hub_meta = cand
        except Exception:  # noqa: BLE001
            pass

    win = "весь лог" if args.hours == 0 else f"последние {args.hours:g} ч"
    out: list[str] = []
    out.append("=" * 60)
    out.append(f" 801 КРИТИЧЕСКИЕ МОМЕНТЫ ЛОГА — {host}")
    out.append("=" * 60)
    out.append(f" лог     : {log_path}")
    out.append(f" окно    : {win}   сформировано {now:%Y-%m-%d %H:%M}")
    out.append(f" итог    : CRITICAL={counts['CRITICAL']}  WARNING={counts['WARNING']}  NOTABLE={counts['NOTABLE']}")
    if not events:
        out.append("\n Ничего значимого в окне не найдено — узел работал чисто.")
    for level in LEVELS:
        group = [e for e in events if e["level"] == level]
        if not group:
            continue
        out.append(f"\n── {level} ({len(group)}) " + "─" * 34)
        # свежие сверху, но не раздувать: до 25 на уровень
        for e in group[-25:][::-1]:
            out.append(f"  {e['ts']}  [{e['label']}]")
            out.append(f"           {e['text']}")
        if len(group) > 25:
            out.append(f"  … ещё {len(group) - 25} (сузь --hours)")
    out.append("\n" + "=" * 60)
    report = "\n".join(out)
    print(report)

    if hub_meta is not None:
        try:
            hub_meta.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in host)
            dest = hub_meta / f"801-logcrit-{safe}.txt"
            dest.write_text(report + "\n", encoding="utf-8")
            print(f"\n>>> сводка записана в Hub: {dest}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n(не удалось записать в Hub _meta: {exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
