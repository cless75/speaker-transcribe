#!/usr/bin/env python3
"""Merge per-speaker ASR tracks into a single diarized transcript.

Сценарий multi-track (canon media-transcription, 2026-06-10): Zoom local
recording с включённой опцией «Record a separate audio file for each
participant» кладёт в ``Audio Record/`` отдельный трек каждого участника.
ASR каждого трека по отдельности (``execution_mode=asr_only``,
``speaker_mode=off``) даёт 100% атрибуцию спикера без pyannote-диаризации:
весь трек = один известный голос.

Этот скрипт сливает per-track сегменты в единый транскрипт:

1. Читает ``*-segments.jsonl`` (или ``*-raw.json`` / ``*-asr-merged.json``)
   каждого трека.
2. Присваивает всем сегментам трека имя спикера, заданное в ``--track``.
3. Фильтрует галлюцинации Whisper на тишине (см. ``--keep-hallucinations``):
   пустой текст, известные junk-паттерны («Субтитры сделал …», «Продолжение
   следует» и т.п.), длинные прогоны одинакового текста подряд в одном треке.
4. Сортирует по ``start`` и пишет merged-артефакты.

Выход (в ``--output-dir``):

- ``{base}-merged-segments.jsonl`` — схема полей как у канонического worker
  (``speaker`` / ``speaker_id`` / ``speaker_name`` / ``speaker_source=track``).
- ``{base}-transcript.md`` — markdown с заголовками смены спикера.
- ``{base}-segments.vtt`` — WebVTT с ``<v Спикер>``.
- ``{base}-run-meta.json`` — параметры слияния и статистика фильтров.

Пример::

    python merge_speaker_tracks.py ^
        --track "Alice=C:/work/output/track-alice-segments.jsonl" ^
        --track "Bob=C:/work/output/track-bob-segments.jsonl" ^
        --output-dir "C:/work/output" ^
        --base-name "2026-01-01-interview" ^
        --title "Interview 2026-01-01"
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import sys
from datetime import timedelta


# Junk-паттерны Whisper на тишине / музыке (русская локаль + универсальные).
HALLUCINATION_PATTERNS = [
    re.compile(r"^субтитры", re.IGNORECASE),
    re.compile(r"^редактор субтитров", re.IGNORECASE),
    re.compile(r"^корректор", re.IGNORECASE),
    re.compile(r"продолжение следует", re.IGNORECASE),
    re.compile(r"^спасибо за просмотр", re.IGNORECASE),
    re.compile(r"^подписывайтесь", re.IGNORECASE),
    re.compile(r"^ставьте лайк", re.IGNORECASE),
    re.compile(r"^\s*\[?(музыка|аплодисменты|шум|тишина)\]?\s*$", re.IGNORECASE),
    re.compile(r"^(thanks for watching|subscribe)", re.IGNORECASE),
    re.compile(r"^динь+[\s,.!-]*$", re.IGNORECASE),
]

# Прогон одинакового текста подряд в одном треке: дольше N подряд = loop-галлюцинация.
REPEAT_RUN_THRESHOLD = 3


def configure_stdio_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
        elif hasattr(stream, "buffer"):
            setattr(sys, stream_name, io.TextIOWrapper(stream.buffer, encoding="utf-8"))


def load_segments(path: pathlib.Path) -> list[dict]:
    """Load ASR segments from segments.jsonl, raw.json or asr-merged.json."""
    if not path.is_file():
        raise FileNotFoundError(f"track source not found: {path}")
    if path.suffix == ".jsonl":
        segments = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    segments.append(json.loads(line))
        return segments
    doc = json.loads(path.read_text(encoding="utf-8"))
    segments = doc.get("segments") or []
    if not segments and isinstance(doc.get("segments_ref"), dict):
        ref = doc["segments_ref"]
        ref_path = path.parent / ref.get("file", "")
        if ref_path.is_file():
            return load_segments(ref_path)
        raise FileNotFoundError(
            f"segments_ref points to missing file: {ref_path} (from {path})"
        )
    return segments


def is_hallucination(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return any(p.search(stripped) for p in HALLUCINATION_PATTERNS)


def filter_track_segments(segments: list[dict]) -> tuple[list[dict], dict]:
    """Drop silence hallucinations; return (clean, stats)."""
    stats = {"input": len(segments), "empty_or_junk": 0, "repeat_run": 0}
    clean: list[dict] = []
    run_text, run_start_idx = None, 0
    flagged_runs: set[int] = set()

    # Сначала находим индексы loop-прогонов (одинаковый текст ≥ threshold подряд).
    norm = [s.get("text", "").strip().lower() for s in segments]
    i = 0
    while i < len(norm):
        j = i
        while j + 1 < len(norm) and norm[j + 1] == norm[i] and norm[i]:
            j += 1
        if j - i + 1 >= REPEAT_RUN_THRESHOLD:
            flagged_runs.update(range(i, j + 1))
        i = j + 1

    for idx, seg in enumerate(segments):
        text = seg.get("text", "")
        if is_hallucination(text):
            stats["empty_or_junk"] += 1
            continue
        if idx in flagged_runs:
            stats["repeat_run"] += 1
            continue
        clean.append(seg)
    stats["kept"] = len(clean)
    return clean, stats


def sec_to_hms(sec: float) -> str:
    return str(timedelta(seconds=int(sec)))


def sec_to_vtt(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="Merge per-speaker ASR tracks into one diarized transcript.",
    )
    parser.add_argument(
        "--track", action="append", required=True, metavar="NAME=PATH",
        help="Speaker name and path to its segments.jsonl/raw.json. Repeatable.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--base-name", required=True)
    parser.add_argument("--title", default=None, help="H1 for transcript.md.")
    parser.add_argument(
        "--keep-hallucinations", action="store_true",
        help="Skip the silence-hallucination filter (keep all segments).",
    )
    args = parser.parse_args()

    out_dir = pathlib.Path(args.output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tracks: list[tuple[str, pathlib.Path]] = []
    for spec in args.track:
        name, sep, raw_path = spec.partition("=")
        if not sep or not name.strip() or not raw_path.strip():
            parser.error(f"--track expects NAME=PATH, got: {spec!r}")
        tracks.append((name.strip(), pathlib.Path(raw_path.strip()).expanduser().resolve()))

    merged: list[dict] = []
    track_meta = []
    for speaker_idx, (speaker, src) in enumerate(tracks, start=1):
        segments = load_segments(src)
        if args.keep_hallucinations:
            clean, stats = segments, {"input": len(segments), "kept": len(segments)}
        else:
            clean, stats = filter_track_segments(segments)
        for seg in clean:
            merged.append({
                "start": seg["start"],
                "end": seg["end"],
                "text": seg.get("text", "").strip(),
                "speaker": speaker,
                "speaker_id": f"Speaker {speaker_idx}",
                "speaker_name": speaker,
                "speaker_source": "track",
                "track_file": src.name,
            })
        track_meta.append({"speaker": speaker, "source": str(src), "stats": stats})
        print(f"[merge-speaker-tracks] track '{speaker}': {stats}")

    if not merged:
        print("[merge-speaker-tracks] no segments after filtering", file=sys.stderr)
        return 2

    merged.sort(key=lambda s: (s["start"], s["end"]))
    base = args.base_name

    jsonl_path = out_dir / f"{base}-merged-segments.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for seg in merged:
            fh.write(json.dumps(seg, ensure_ascii=False) + "\n")

    title = args.title or base
    duration_min = int(merged[-1]["end"] / 60)
    speakers = ", ".join(name for name, _ in tracks)
    md_lines = [
        f"# {title}",
        "",
        f"> Источник: multi-track ASR ({len(tracks)} трека, атрибуция по дорожкам — без диаризации).",
        f"> Длительность: ~{duration_min} мин. Сегментов: {len(merged)}. Спикеры: {speakers}.",
        "",
        "---",
    ]
    last_speaker = None
    for seg in merged:
        if seg["speaker"] != last_speaker:
            md_lines.append("")
            md_lines.append(f"### {sec_to_hms(seg['start'])} — {seg['speaker']}")
            md_lines.append("")
            last_speaker = seg["speaker"]
        md_lines.append(seg["text"])
    md_path = out_dir / f"{base}-transcript.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    vtt_lines = ["WEBVTT", ""]
    for i, seg in enumerate(merged, start=1):
        vtt_lines.append(str(i))
        vtt_lines.append(f"{sec_to_vtt(seg['start'])} --> {sec_to_vtt(seg['end'])}")
        vtt_lines.append(f"<v {seg['speaker']}>{seg['text']}</v>")
        vtt_lines.append("")
    vtt_path = out_dir / f"{base}-segments.vtt"
    vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")

    run_meta = {
        "schema": "merge-speaker-tracks-v1",
        "mode": "multi_track_merge",
        "tracks": track_meta,
        "segments_total": len(merged),
        "hallucination_filter": not args.keep_hallucinations,
        "outputs": {
            "merged_segments_jsonl": str(jsonl_path),
            "transcript_md": str(md_path),
            "segments_vtt": str(vtt_path),
        },
    }
    meta_path = out_dir / f"{base}-run-meta.json"
    meta_path.write_text(
        json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(run_meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
