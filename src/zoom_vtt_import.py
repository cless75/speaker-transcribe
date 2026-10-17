"""Import a Zoom WebVTT transcript without downloading media or running ASR.

This is deliberately a small, dependency-free path for recordings whose heavy
video is retained on the node rather than copied into a cloud inbox.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re


_TIME = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})$")
_VOICE = re.compile(r"^<v(?:\.[^ >]+)?\s+([^>]+)>(.*?)(?:</v>)?$", re.I)
_LABEL = re.compile(r"^([^:]{1,80}):\s+(.+)$")
_TAG = re.compile(r"<[^>]+>")


def _seconds(value: str) -> float:
    match = _TIME.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid VTT timestamp: {value!r}")
    hours, minutes, seconds, millis = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def _hms(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def parse_zoom_vtt(path: pathlib.Path) -> list[dict]:
    """Read every timed cue; a cue without a speaker remains unattributed, not lost."""
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    if not blocks or not blocks[0].strip().startswith("WEBVTT"):
        raise ValueError("not a WEBVTT recording")
    cues: list[dict] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing = next((idx for idx, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        left, right = lines[timing].split("-->", 1)
        start = _seconds(left)
        end = _seconds(right.strip().split()[0])
        if end < start:
            raise ValueError("VTT cue ends before it starts")
        raw = " ".join(lines[timing + 1:]).strip()
        if not raw:
            continue
        speaker = ""
        voice = _VOICE.match(raw)
        if voice:
            speaker, raw = voice.group(1).strip(), voice.group(2).strip()
        else:
            label = _LABEL.match(raw)
            if label:
                speaker, raw = label.group(1).strip(), label.group(2).strip()
        utterance = _TAG.sub("", raw).strip()
        if utterance:
            cues.append({"start": start, "end": end, "speaker": speaker, "text": utterance})
    if not cues:
        raise ValueError("Zoom VTT has no transcript cues")
    return cues


def _publish(path: pathlib.Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def import_zoom_vtt(vtt: pathlib.Path, output_dir: pathlib.Path) -> tuple[pathlib.Path, dict]:
    """Publish a transcript and run metadata in the watcher's normal output layout."""
    cues = parse_zoom_vtt(vtt)  # validate before touching outputs
    output_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    transcript = output_dir / f"{vtt.stem}-transcript.md"
    meta_path = output_dir / f"{vtt.stem}-run-meta.json"
    duration = max(cue["end"] for cue in cues)
    speakers = {cue["speaker"] for cue in cues if cue["speaker"]}
    header = ["---", f"source_file: {json.dumps(str(vtt), ensure_ascii=False)}",
              'source_type: "zoom_vtt"', 'engine: "zoom_vtt_import"',
              f"duration: {duration}", f"created_at: {json.dumps(now)}", "---",
              "", "## Transcript", ""]
    body = [f"[{_hms(cue['start'])}] "
            + (f"{cue['speaker']} [zoom_vtt]: " if cue["speaker"] else "")
            + cue["text"] for cue in cues]
    meta = {"status": "ok", "source_file": str(vtt), "source_type": "zoom_vtt",
            "engine": "zoom_vtt_import", "duration_sec": duration,
            "diarization": {"source": "zoom_vtt", "speakers_detected": len(speakers)},
            "outputs": {"md": str(transcript)}, "created_at": now}
    _publish(transcript, "\n".join(header + body) + "\n")
    _publish(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return transcript, meta
