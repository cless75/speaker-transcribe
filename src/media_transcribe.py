#!/usr/bin/env python
import datetime as dt
import array
import hashlib
import json
import math
import ctypes
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import traceback
import time
import warnings
import re
import multiprocessing
import struct
import wave
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from faster_whisper import WhisperModel

try:
    import torch
except Exception:  # pragma: no cover - optional dependency in runtime
    torch = None

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mpga", ".mpeg"}
VOICEPRINT_SCHEMA_VERSION = "v3"
VOICEPRINT_SCHEMA_LEGACY_V2 = "v2"

# Канон media-transcription skill: не более двух параллельных чанков (CUDA/RAM).
MAX_PARALLEL_CHUNKS_HARD_CAP = 2

OUTPUT_DIR_LOCK_FILENAME = ".media-transcription.lock"
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_ERROR_INVALID_PARAMETER = 87
_WIN_ERROR_ACCESS_DENIED = 5

# Таймаут ожидания «любого» завершившегося чанка в параллельном режиме (сек).
DEFAULT_CHUNK_FUTURE_TIMEOUT_SEC = 7200.0

# Лог в stderr при потреблении сегментов из lazy-итератора transcribe (иначе «тишина» на list(segments_iter)).
SEGMENT_ITER_PROGRESS_EVERY = 50

ASR_MERGED_SCHEMA = "media-transcription-asr-merged-v1"

# Bug 1 fix v2 (canon 2026-05-18) — Whisper model keepalive list to prevent SIGSEGV.
# Background: `del shared_model` invokes WhisperModel.__del__ → ctranslate2 C++ destructor
# → CUDA destructor, which SIGSEGVs silently on Windows + RTX 3050 + CUDA 12.x + float16.
# Python try/except cannot catch SIGSEGV. The previous fix (granular logging) confirmed
# the crash happens AT del; 9 files in _failed/ over 07.05–17.05 all stop at
# `phase=sequential_whisper_release begin` with no further log line.
#
# Mitigation: keep a strong reference to the model in this module-level list to prevent
# refcount-triggered destructor calls during the run. At process exit, Python's atexit
# may still SIGSEGV, but by then all artifacts (chunks, merged JSON, transcript.md, vtt,
# run-meta.json) are flushed to disk. The watcher's existing_transcript() match recovers
# the run on next tick. VRAM is reclaimed by the OS when the process dies.
#
# Trade-off: WhisperModel stays in VRAM (~1.5GB) during diarization/alignment phase
# (~30s on RTX 3050 8GB). Pyannote loads alongside (~2GB), total ~3.5GB — well within
# 8GB budget.
#
# See: memory/feedback_media_transcribe_known_bugs_2026_05_17.md
_WHISPER_MODEL_KEEPALIVE: list = []


def configure_stdio_utf8() -> None:
    """Windows + перенаправленный stdout/stderr в pipe: иначе JSON с ensure_ascii=False ломается."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        enc = getattr(stream, "encoding", None) or ""
        if enc.lower() == "utf-8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError, TypeError):
            pass


def timestamp_hms(total_seconds: float) -> str:
    whole = max(0, int(float(total_seconds or 0)))
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    seconds = whole % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timestamp_vtt(total_seconds: float) -> str:
    safe = max(0.0, float(total_seconds or 0))
    hours = int(safe // 3600)
    minutes = int((safe % 3600) // 60)
    seconds = int(safe % 60)
    milliseconds = int(round((safe - int(safe)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def parse_vtt_timestamp_seconds(value: str) -> float:
    match = re.match(r"^\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*$", str(value or ""))
    if not match:
        raise RuntimeError(f"invalid_vtt_timestamp:{value}")
    hours, minutes, seconds, milliseconds = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000.0
    )


def clean_zoom_speaker_name(value: str) -> str:
    name = str(value or "").strip()
    name = re.sub(r"^\d+\.\s*", "", name)
    name = re.sub(r"\s*\([^)]*\)$", "", name)
    name = re.sub(r"\s*\(.*$", "", name)
    return name.strip()


def parse_zoom_vtt_turns(vtt_path: str) -> tuple[list[dict], dict]:
    path = pathlib.Path(vtt_path)
    raw_lines = path.read_text(encoding="utf-8").splitlines()

    start_idx = 0
    if raw_lines and raw_lines[0].strip() == "---":
        for idx in range(1, len(raw_lines)):
            if raw_lines[idx].strip() == "---":
                start_idx = idx + 1
                break
    for idx in range(start_idx, len(raw_lines)):
        if raw_lines[idx].strip().upper() == "WEBVTT":
            start_idx = idx + 1
            break

    lines = raw_lines[start_idx:]
    cues: list[dict] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or re.fullmatch(r"\d+", line):
            index += 1
            continue
        if "-->" not in line:
            index += 1
            continue
        parts = [item.strip() for item in line.split("-->")]
        if len(parts) != 2:
            index += 1
            continue
        start_sec = parse_vtt_timestamp_seconds(parts[0])
        end_sec = parse_vtt_timestamp_seconds(parts[1])
        index += 1
        text_lines: list[str] = []
        while index < len(lines):
            candidate = lines[index].strip()
            if not candidate:
                index += 1
                break
            if "-->" in candidate:
                break
            if re.fullmatch(r"\d+", candidate):
                index += 1
                break
            text_lines.append(candidate)
            index += 1
        if not text_lines:
            continue
        joined = " ".join(text_lines).strip()
        speaker_raw = ""
        text = joined
        if ":" in joined:
            left, right = joined.split(":", 1)
            if left.strip():
                speaker_raw = left.strip()
                text = right.strip()
        speaker_name = clean_zoom_speaker_name(speaker_raw)
        if not speaker_name or not text:
            continue
        cues.append(
            {
                "start": round(start_sec, 3),
                "end": round(max(start_sec, end_sec), 3),
                "raw_label": speaker_raw,
                "speaker_name": speaker_name,
                "text": text,
            }
        )

    if not cues:
        raise RuntimeError(f"zoom_vtt_has_no_named_cues:{path}")

    merged: list[dict] = []
    for cue in cues:
        if (
            merged
            and merged[-1]["speaker_name"].casefold() == cue["speaker_name"].casefold()
            and abs(float(cue["start"]) - float(merged[-1]["end"])) <= 0.25
        ):
            merged[-1]["end"] = cue["end"]
            merged[-1]["text"] = f"{merged[-1]['text']} {cue['text']}".strip()
            merged[-1]["raw_labels"].append(cue["raw_label"])
            merged[-1]["cue_count"] += 1
            continue
        merged.append(
            {
                "start": cue["start"],
                "end": cue["end"],
                "raw_labels": [cue["raw_label"]],
                "speaker_name": cue["speaker_name"],
                "text": cue["text"],
                "cue_count": 1,
            }
        )

    stable_order: list[str] = []
    alias_map: dict[str, str] = {}
    observations: dict[str, dict] = {}
    turns: list[dict] = []
    for item in merged:
        normalized = item["speaker_name"]
        token = normalized.casefold()
        if token not in alias_map:
            stable_order.append(normalized)
            alias_map[token] = f"Speaker {len(stable_order)}"
            observations[alias_map[token]] = {
                "speaker_name": normalized,
                "raw_labels": [],
                "cue_count": 0,
                "total_duration_sec": 0.0,
            }
        speaker_id = alias_map[token]
        obs = observations[speaker_id]
        obs["cue_count"] += int(item.get("cue_count", 1))
        obs["total_duration_sec"] = round(
            float(obs.get("total_duration_sec") or 0.0) + max(0.0, float(item["end"]) - float(item["start"])),
            3,
        )
        for raw_label in item["raw_labels"]:
            value = str(raw_label or "").strip()
            if value and value not in obs["raw_labels"]:
                obs["raw_labels"].append(value)
        turns.append(
            {
                "start": round(float(item["start"]), 3),
                "end": round(float(item["end"]), 3),
                "raw_label": item["raw_labels"][0] if item["raw_labels"] else normalized,
                "speaker_id": speaker_id,
                "speaker_name": normalized,
                "speaker_source": "zoom_vtt",
            }
        )

    return turns, {
        "status": "ok",
        "path": str(path.resolve()),
        "cues_parsed": len(cues),
        "turns_built": len(turns),
        "speakers_detected": len(stable_order),
        "speaker_observations": observations,
    }


KTALK_HEADER_RE = re.compile(r'^Транскрипция записи\s+"(?P<title>.*)"\s*(?P<date>.*)$')
KTALK_LINE_RE = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})\t(?P<speaker>[^\t]+)\t(?P<text>.+)$")

# A Ktalk export gives the START of each utterance but no end. Duration is estimated
# from the text length: measured across real exports the median rate is 12-16 chars/sec,
# so a deliberately low rate over-estimates rather than under-estimates. That is the safe
# direction: an over-long turn is trimmed by whatever utterance starts next (see
# _subtract_interval below), while an under-long one would leave ASR segments with no
# speaker at all.
KTALK_CHARS_PER_SEC = 10.0
# A one-word interjection ("mhm", "right") is a couple of seconds of speech, but by
# text length alone it estimates to ~1s — too short to out-overlap the long turn it
# interrupts, so the ASR segment carrying it goes to the wrong speaker. Measured on a
# sample export: 1.0s got 19/20 segments right, >=2.0s got 20/20 (at any chars/sec).
# Over-reaching is harmless here — the next utterance trims it.
KTALK_MIN_TURN_SEC = 3.0


def _subtract_interval(pieces: list[tuple[float, float]], cut_start: float, cut_end: float) -> list[tuple[float, float]]:
    """Remove [cut_start, cut_end] from a list of disjoint intervals."""
    out: list[tuple[float, float]] = []
    for start, end in pieces:
        if cut_end <= start or cut_start >= end:
            out.append((start, end))
            continue
        if cut_start > start:
            out.append((start, min(cut_start, end)))
        if cut_end < end:
            out.append((max(cut_end, start), end))
    return [(s, e) for s, e in out if e - s > 0.05]


def parse_ktalk_txt_turns(txt_path: str) -> tuple[list[dict], dict]:
    """Speaker turns from a Ktalk (Kontur.Talk) transcript export.

    Format (UTF-8, tab-separated, one utterance per line, monotonic timecodes)::

        Транскрипция записи "Some meeting" 10 июля 2026 г
        00:00:00	Ivan Petrov	Hello everyone.
        00:00:03	Maria Ivanova	Hi.

    Ktalk names the speakers, which diarization cannot do on its own, so this
    replaces the pyannote pass entirely (see resolve_speaker_turns).

    Utterances interleave: a short "mhm" lands inside a long answer, and the export
    records only where each one begins. So an estimated turn is trimmed by every
    utterance that starts later — the later start wins its own window, and the
    interrupted turn resumes after it. Without this a one-word interjection would
    take the whole span up to the next utterance away from the person still talking
    (~22% of utterances in the sample exports overrun the slot to their successor).
    """
    path = pathlib.Path(txt_path)
    # utf-8-sig: tolerate a BOM if the file was round-tripped through a Windows editor.
    lines = path.read_text(encoding="utf-8-sig").splitlines()

    header: dict = {}
    utterances: list[dict] = []
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        match = KTALK_LINE_RE.match(line)
        if not match:
            head = KTALK_HEADER_RE.match(line.strip())
            if head and not header:
                header = {"title": head.group("title"), "date": head.group("date").strip()}
            else:
                malformed += 1
            continue
        start = int(match.group("h")) * 3600 + int(match.group("m")) * 60 + int(match.group("s"))
        speaker = match.group("speaker").strip()
        text = match.group("text").strip()
        if not speaker or not text:
            malformed += 1
            continue
        utterances.append({"start": float(start), "speaker_name": speaker, "text": text})

    if not utterances:
        raise RuntimeError(f"ktalk_txt_has_no_utterances:{path}")

    utterances.sort(key=lambda item: item["start"])
    estimated = [
        (
            item["start"],
            item["start"] + max(KTALK_MIN_TURN_SEC, len(item["text"]) / KTALK_CHARS_PER_SEC),
        )
        for item in utterances
    ]

    stable_order: list[str] = []
    alias_map: dict[str, str] = {}
    observations: dict[str, dict] = {}
    turns: list[dict] = []
    for index, item in enumerate(utterances):
        start, end = estimated[index]
        pieces = [(start, end)]
        for other in range(index + 1, len(utterances)):
            next_start, next_end = estimated[other]
            if next_start >= end:
                break
            pieces = _subtract_interval(pieces, next_start, next_end)
            if not pieces:
                break
        if not pieces:
            continue

        name = item["speaker_name"]
        token = name.casefold()
        if token not in alias_map:
            stable_order.append(name)
            alias_map[token] = f"Speaker {len(stable_order)}"
            observations[alias_map[token]] = {
                "speaker_name": name,
                "raw_labels": [name],
                "cue_count": 0,
                "total_duration_sec": 0.0,
            }
        speaker_id = alias_map[token]
        obs = observations[speaker_id]
        obs["cue_count"] += 1
        for piece_start, piece_end in pieces:
            obs["total_duration_sec"] = round(float(obs["total_duration_sec"]) + (piece_end - piece_start), 3)
            turns.append(
                {
                    "start": round(piece_start, 3),
                    "end": round(piece_end, 3),
                    "raw_label": name,
                    "speaker_id": speaker_id,
                    "speaker_name": name,
                    "speaker_source": "ktalk_txt",
                }
            )

    turns.sort(key=lambda turn: turn["start"])
    return turns, {
        "status": "ok",
        "path": str(path.resolve()),
        "title": header.get("title"),
        "recorded_label": header.get("date"),
        "cues_parsed": len(utterances),
        "malformed_lines": malformed,
        "turns_built": len(turns),
        "speakers_detected": len(stable_order),
        "chars_per_sec": KTALK_CHARS_PER_SEC,
        "speaker_observations": observations,
    }


def ensure_work_root(work_root: str | None) -> pathlib.Path:
    root = pathlib.Path(work_root or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_job_root(work_root: str | None) -> pathlib.Path:
    base_dir = ensure_work_root(work_root)
    return pathlib.Path(tempfile.mkdtemp(prefix="media-transcription-job-", dir=str(base_dir)))


def stage_dir(job_root: pathlib.Path, stage_name: str) -> pathlib.Path:
    path = job_root / stage_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_audio_if_needed(payload: dict, warnings: list[str], job_root: pathlib.Path):
    input_path = pathlib.Path(payload["input_path"])
    suffix = input_path.suffix.lower()
    ffmpeg_bin = payload.get("ffmpeg_bin") or "ffmpeg"
    ffmpeg_available = pathlib.Path(ffmpeg_bin).exists() or bool(shutil.which(ffmpeg_bin))
    needs_ascii_copy = any(ord(char) > 127 for char in str(input_path))
    extract_root = stage_dir(job_root, "extract")

    if suffix not in VIDEO_EXTENSIONS and suffix == ".wav":
        if needs_ascii_copy:
            temp_audio = extract_root / "input.wav"
            shutil.copy2(input_path, temp_audio)
            warnings.append("unicode_path_copied_to_ascii_temp")
            return str(temp_audio)
        return str(input_path)

    if suffix not in VIDEO_EXTENSIONS and suffix in AUDIO_EXTENSIONS:
        if ffmpeg_available:
            temp_audio = extract_root / f"{input_path.stem}.wav"
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(input_path), "-ac", "1", "-ar", "16000", str(temp_audio)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return str(temp_audio)
        if needs_ascii_copy:
            temp_audio = extract_root / f"input{suffix}"
            shutil.copy2(input_path, temp_audio)
            warnings.append("unicode_path_copied_to_ascii_temp")
            return str(temp_audio)
        warnings.append("ffmpeg_not_found_audio_passed_directly")
        return str(input_path)

    if suffix in VIDEO_EXTENSIONS:
        if not ffmpeg_available:
            raise RuntimeError("ffmpeg is required to extract audio from video files")
        temp_audio = extract_root / f"{input_path.stem}.wav"
        subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(input_path), "-vn", "-ac", "1", "-ar", "16000", str(temp_audio)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return str(temp_audio)

    raise RuntimeError(f"Unsupported media extension: {suffix}")


def stderr_log_line(message: str) -> None:
    """Строка в stderr с локальным временем (не UTC); для chunk worker без payload."""
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[media-transcription] {ts} | {message}", file=sys.stderr, flush=True)


def log(payload: dict, message: str) -> None:
    if payload.get("enable_processing_logs", True):
        stderr_log_line(message)


def format_chunk_eta_suffix(
    success_done: int,
    queue_size: int,
    t0_monotonic: float,
    max_parallel: int,
) -> str:
    """Оценка оставшегося времени фазы ASR по успешным чанкам (грубая)."""
    if success_done < 1:
        return ""
    elapsed = time.monotonic() - t0_monotonic
    avg = elapsed / success_done
    remaining = queue_size - success_done
    if remaining <= 0:
        return " eta_chunks: phase done"
    if max_parallel <= 1:
        eta_sec = avg * remaining
    else:
        eta_sec = avg * remaining / max(1, max_parallel)
    eta_clock = dt.datetime.now() + dt.timedelta(seconds=eta_sec)
    eta_wall = eta_clock.strftime("%H:%M:%S")
    mins = int(eta_sec // 60)
    secs = int(eta_sec % 60)
    approx = f"~{mins}m{secs}s" if mins else f"~{secs}s"
    return f" eta_chunks_approx={int(eta_sec)}s ({approx}) eta_wall_local~{eta_wall}"


def probe_duration_seconds(input_path: str, ffprobe_bin: str | None) -> float | None:
    ffprobe = ffprobe_bin or "ffprobe"
    ffprobe_exists = pathlib.Path(ffprobe).exists() or bool(shutil.which(ffprobe))
    if not ffprobe_exists:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                input_path,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        value = (proc.stdout or "").strip()
        return float(value) if value else None
    except Exception:
        return None


def split_audio_into_chunks(
    audio_path: str,
    payload: dict,
    warnings: list[str],
    job_root: pathlib.Path,
) -> list[dict]:
    chunk_minutes = int(payload.get("chunk_minutes", 20) or 20)
    chunk_overlap_sec = int(payload.get("chunk_overlap_sec", 30) or 30)
    ffmpeg_bin = payload.get("ffmpeg_bin") or "ffmpeg"
    ffprobe_bin = payload.get("ffprobe_bin") or "ffprobe"
    ffmpeg_available = pathlib.Path(ffmpeg_bin).exists() or bool(shutil.which(ffmpeg_bin))
    if not ffmpeg_available:
        warnings.append("chunking_skipped_ffmpeg_unavailable")
        return [{"path": audio_path, "start": 0.0, "duration": None, "index": 0}]

    duration = probe_duration_seconds(audio_path, ffprobe_bin)
    if duration is None:
        warnings.append("chunking_skipped_duration_unknown")
        return [{"path": audio_path, "start": 0.0, "duration": None, "index": 0}]

    chunk_size = float(chunk_minutes * 60)
    overlap = float(chunk_overlap_sec)
    if duration <= chunk_size:
        return [{"path": audio_path, "start": 0.0, "duration": duration, "index": 0}]

    step = chunk_size - overlap
    if step <= 0:
        warnings.append("invalid_chunk_step_fallback_to_single")
        return [{"path": audio_path, "start": 0.0, "duration": duration, "index": 0}]

    temp_dir = stage_dir(job_root, "chunks")
    chunks: list[dict] = []
    cursor = 0.0
    chunk_index = 0
    while cursor < duration:
        chunk_duration = min(chunk_size, duration - cursor)
        chunk_file = pathlib.Path(temp_dir) / f"chunk_{chunk_index:03d}.wav"
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{cursor:.3f}",
                "-t",
                f"{chunk_duration:.3f}",
                "-i",
                audio_path,
                "-ac",
                "1",
                "-ar",
                "16000",
                str(chunk_file),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        chunks.append(
            {
                "path": str(chunk_file),
                "start": round(cursor, 3),
                "duration": round(chunk_duration, 3),
                "index": chunk_index,
            }
        )
        chunk_index += 1
        cursor += step

    return chunks


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


VOICE_EMBEDDING_EXTRACTOR_V1 = "ecapa_tdnn_v1"
VOICE_EMBEDDING_EXTRACTOR_V0_LEGACY = "acoustic_stats_v0"
VOICE_EMBEDDING_DEFAULT_EXTRACTOR = VOICE_EMBEDDING_EXTRACTOR_V1
VOICE_EMBEDDING_THRESHOLDS = {
    VOICE_EMBEDDING_EXTRACTOR_V1: 0.55,   # ECAPA-TDNN: cross-speaker max ~0.43, same-speaker ~0.7+
    VOICE_EMBEDDING_EXTRACTOR_V0_LEGACY: 0.84,  # legacy 12-dim acoustic stats (false-positive prone)
}

# Module-level singleton for the ECAPA-TDNN classifier. Lazy-loaded on first use
# to avoid import-time torch/speechbrain overhead during phases that don't need it.
_voice_embedder_singleton: dict | None = None


def _get_voice_embedder_singleton() -> dict:
    """Return {classifier, device, version} or raise. Lazy-loaded once per process.

    Resolves Bug 3 (Objective 258-20): replaces the legacy acoustic-stats extractor
    with a real voice biometric (192-dim ECAPA-TDNN from speechbrain). See also
    `feedback_voiceprint_known_bugs.md` for the full root-cause story.
    """
    global _voice_embedder_singleton
    if _voice_embedder_singleton is not None:
        return _voice_embedder_singleton
    import os as _os
    _os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    import torch as _torch
    from speechbrain.utils.fetching import LocalStrategy as _LocalStrategy
    from speechbrain.inference.speaker import EncoderClassifier as _EncoderClassifier
    device = "cuda" if _torch.cuda.is_available() else "cpu"
    savedir = _os.environ.get(
        "MEDIA_TRANSCRIBE_VOICE_EMBED_CACHE",
        _os.path.join(_os.path.expanduser("~"), ".cache", "speaker-transcribe", "speechbrain-ecapa-tdnn"),
    )
    classifier = _EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=savedir,
        run_opts={"device": device},
        local_strategy=_LocalStrategy.COPY,
    )
    _voice_embedder_singleton = {
        "classifier": classifier,
        "device": device,
        "version": VOICE_EMBEDDING_EXTRACTOR_V1,
        "dim": 192,
    }
    return _voice_embedder_singleton


def build_voice_embedding_from_wav(wav_path: str) -> tuple[list[float], dict]:
    """Extract 192-dim ECAPA-TDNN voice embedding (returns vector + meta).

    Returns (vector_list, meta) where meta has:
      - extractor: "ecapa_tdnn_v1"
      - dim: 192
      - device: "cuda"|"cpu"
      - source_sample_rate / source_channels (from wav header)
      - duration_sec (from samples / sr)

    On failure (model not loadable, wav malformed) falls back to the legacy 12-dim
    acoustic-stats extractor and returns meta with extractor="acoustic_stats_v0".
    Callers should respect the per-extractor threshold from VOICE_EMBEDDING_THRESHOLDS.
    """
    try:
        embedder = _get_voice_embedder_singleton()
    except Exception as exc:
        legacy_vector = build_acoustic_embedding_from_wav_legacy_v0(wav_path)
        return legacy_vector, {
            "extractor": VOICE_EMBEDDING_EXTRACTOR_V0_LEGACY,
            "dim": len(legacy_vector),
            "fallback_reason": f"ecapa_tdnn_load_failed: {type(exc).__name__}: {exc}",
        }
    import numpy as _np
    import soundfile as _sf
    import torch as _torch
    audio, sr = _sf.read(wav_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    duration_sec = float(len(audio)) / float(sr) if sr else 0.0
    sig = _torch.from_numpy(audio).unsqueeze(0)
    if sr != 16000:
        import torchaudio as _ta
        sig = _ta.functional.resample(sig, sr, 16000)
    classifier = embedder["classifier"]
    with _torch.no_grad():
        emb = classifier.encode_batch(sig).squeeze().cpu().numpy()
    vector = [round(float(v), 8) for v in emb.tolist()]
    return vector, {
        "extractor": embedder["version"],
        "dim": len(vector),
        "device": embedder["device"],
        "source_sample_rate": int(sr),
        "duration_sec": round(duration_sec, 3),
    }


def build_acoustic_embedding_from_wav_legacy_v0(wav_path: str) -> list[float]:
    """Legacy 12-dim acoustic-statistics extractor — kept for backward compat only.

    DO NOT USE for new enrollments — it is acoustic statistics (RMS+ZCR+Peak+duration),
    NOT voice biometric. Cross-speaker cosine ≈ 0.85-0.95 → false positive ALWAYS at the
    legacy threshold 0.84. See `feedback_voiceprint_known_bugs.md` Bug 2 root-cause.
    Use `build_voice_embedding_from_wav` (ECAPA-TDNN, 192-dim) instead.
    """
    return build_acoustic_embedding_from_wav(wav_path)


def build_acoustic_embedding_from_wav(wav_path: str) -> list[float]:
    """[LEGACY] Returns 12-dim vector of (log-RMS, ZCR, log-Peak stats + duration + framerate + const).

    DEPRECATED: kept only for backward compat with existing v0 embeddings in the store.
    The vector is NOT a voice biometric — it encodes loudness/ZCR/duration of the clip
    and yields cross-speaker cosine ≈ 0.85-0.95 (false-positive prone).
    Use `build_voice_embedding_from_wav` (ECAPA-TDNN) for new enrollments.
    """
    with wave.open(wav_path, "rb") as audio:
        nchannels = audio.getnchannels()
        sampwidth = audio.getsampwidth()
        framerate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())

    if not frames:
        return [0.0] * 12

    sample_count = len(frames) // sampwidth
    if sample_count == 0:
        return [0.0] * 12

    if sampwidth == 1:
        unpacked = [((value - 128) << 8) for value in frames]
    elif sampwidth == 2:
        unpacked = [value[0] for value in struct.iter_unpack("<h", frames)]
    elif sampwidth == 4:
        unpacked = [int(value[0] / 65536) for value in struct.iter_unpack("<i", frames)]
    else:
        return [0.0] * 12

    if nchannels > 1:
        mono = []
        for idx in range(0, len(unpacked), nchannels):
            channel_values = unpacked[idx : idx + nchannels]
            if not channel_values:
                continue
            mono.append(int(sum(channel_values) / len(channel_values)))
        unpacked = mono

    window_samples = max(160, int(framerate * 0.02))
    rms_values = []
    zcr_values = []
    peak_values = []
    for offset in range(0, len(unpacked), window_samples):
        chunk = unpacked[offset : offset + window_samples]
        if len(chunk) < max(10, window_samples // 4):
            continue
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
        peak = max(abs(v) for v in chunk)
        rms_values.append(rms)
        peak_values.append(peak)
        crossings = 0
        last_sign = 0
        for value in chunk:
            sign = 1 if value >= 0 else -1
            if last_sign != 0 and sign != last_sign:
                crossings += 1
            last_sign = sign
        zcr_values.append(crossings / max(1, len(chunk)))

    if not rms_values:
        return [0.0] * 12

    def _stats(values: list[float]) -> tuple[float, float, float]:
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        return mean, math.sqrt(variance), max(values)

    rms_mean, rms_std, rms_max = _stats(rms_values)
    zcr_mean, zcr_std, zcr_max = _stats(zcr_values)
    peak_mean, peak_std, peak_max = _stats(peak_values)
    duration_sec = len(unpacked) / framerate

    vector = [
        math.log1p(rms_mean),
        math.log1p(rms_std),
        math.log1p(rms_max),
        zcr_mean,
        zcr_std,
        zcr_max,
        math.log1p(peak_mean),
        math.log1p(peak_std),
        math.log1p(peak_max),
        duration_sec,
        float(framerate) / 10000.0,
        1.0,
    ]
    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]
    return [round(v, 8) for v in vector]


def acquire_lock(lock_path: pathlib.Path, timeout_sec: float = 10.0) -> None:
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return
        except FileExistsError:
            if time.time() - start > timeout_sec:
                raise RuntimeError(f"voiceprint store lock timeout: {lock_path}")
            time.sleep(0.1)


def release_lock(lock_path: pathlib.Path) -> None:
    if lock_path.exists():
        lock_path.unlink(missing_ok=True)


def load_voiceprint_store(store_path: str) -> dict:
    """Load voiceprint store, normalize to schema v3 (persons[].profiles[]).

    v3 store has: {schema_version: "v3", persons: [{person_id, canonical_name, profiles: [...]}]}
    Each profile inside a person has: voice_hash, embeddings, best_clip_*, clip_history.
    Identity fields (canonical_name, contact_ref, ...) are person-level.

    On-disk v2 stores are auto-migrated to v3 in-memory (one person per v2 profile).
    Save will write v3 format; the source file stays v2 until next save.
    """
    path = pathlib.Path(store_path)
    if not path.exists():
        return {"schema_version": VOICEPRINT_SCHEMA_VERSION, "persons": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    schema = raw.get("schema_version")
    if schema == VOICEPRINT_SCHEMA_VERSION and isinstance(raw.get("persons"), list):
        return _normalize_v3_store(raw)
    return _migrate_v2_to_v3_in_memory(raw)


def _normalize_v3_store(raw: dict) -> dict:
    persons = []
    for person in raw.get("persons", []):
        if not isinstance(person, dict):
            continue
        profiles = []
        for profile in person.get("profiles", []) or []:
            if not isinstance(profile, dict):
                continue
            embeddings = [item for item in profile.get("embeddings", []) if isinstance(item, dict)]
            voice_hash = str(profile.get("voice_hash") or "").strip()
            if not voice_hash and embeddings:
                voice_hash = build_voice_hash(
                    average_embedding(
                        [item.get("vector", []) for item in embeddings if item.get("vector")]
                    )
                )
            if not voice_hash:
                continue
            profiles.append({
                "voice_hash": voice_hash,
                "speaker_profile_id": profile.get("speaker_profile_id") or voice_hash,
                "embeddings": embeddings,
                "best_clip_path": profile.get("best_clip_path"),
                "best_clip_source_file": profile.get("best_clip_source_file"),
                "best_clip_score": profile.get("best_clip_score"),
                "best_clip_updated_at": profile.get("best_clip_updated_at"),
                "clip_history": [
                    item for item in profile.get("clip_history", []) if isinstance(item, dict)
                ],
                "context_label": profile.get("context_label"),
                "created_at": profile.get("created_at"),
                "updated_at": profile.get("updated_at"),
            })
        if not profiles:
            # Skip empty persons (no profiles at all and no canonical_name worth keeping)
            if not person.get("canonical_name"):
                continue
        person_id = str(person.get("person_id") or "").strip()
        if not person_id:
            person_id = f"person_{build_voice_hash([0.0])[3:15]}"
        voiceprints = []
        for vp in person.get("voiceprints") or []:
            if vp and vp not in voiceprints:
                voiceprints.append(vp)
        for prof in profiles:
            if prof["voice_hash"] not in voiceprints:
                voiceprints.append(prof["voice_hash"])
        persons.append({
            "person_id": person_id,
            "canonical_name": person.get("canonical_name"),
            "display_name": person.get("display_name"),
            "contact_ref": person.get("contact_ref"),
            "contact_name": person.get("contact_name"),
            "profiles": profiles,
            "voiceprints": voiceprints,
            "observed_aliases": [
                item for item in (person.get("observed_aliases") or []) if isinstance(item, dict)
            ],
            "created_at": person.get("created_at"),
            "updated_at": person.get("updated_at"),
        })
    return {
        "schema_version": VOICEPRINT_SCHEMA_VERSION,
        "persons": persons,
        "migrated_from": raw.get("migrated_from"),
        "migration_date": raw.get("migration_date"),
    }


def _migrate_v2_to_v3_in_memory(raw: dict) -> dict:
    """Auto-migrate v2 (profiles[]) to v3 (persons[]) on load.

    Profiles with the same canonical_name (case-insensitive, normalized) collapse into
    one person. Profiles without canonical_name become orphan persons (1-to-1).
    Idempotent: subsequent loads of the same v2 file produce the same v3 structure.
    """
    profiles_v2 = raw.get("profiles", [])
    if not isinstance(profiles_v2, list):
        profiles_v2 = []
    from collections import defaultdict
    named_groups = defaultdict(list)
    unnamed = []
    for profile in profiles_v2:
        if not isinstance(profile, dict):
            continue
        name = (profile.get("canonical_name") or "").strip()
        if name:
            named_groups[name.casefold()].append(profile)
        else:
            unnamed.append(profile)

    def _slugify(s: str) -> str:
        out = []
        for c in (s or ""):
            if c.isalnum() or c in "_":
                out.append(c.lower())
            elif c.isspace() or c in ".,":
                out.append("-")
        result = "".join(out).strip("-")
        while "--" in result:
            result = result.replace("--", "-")
        return result[:60]

    persons = []
    for nname, group in named_groups.items():
        rep = max(group, key=lambda p: len(p.get("canonical_name") or ""))
        canonical = rep.get("canonical_name")
        person_id = f"person_{_slugify(canonical) or hashlib.sha256(nname.encode()).hexdigest()[:12]}"
        v3_profiles = []
        voiceprints = []
        aliases = []
        seen_aliases = set()
        for v2p in group:
            embeddings = [it for it in (v2p.get("embeddings") or []) if isinstance(it, dict)]
            vh = str(v2p.get("voice_hash") or "").strip()
            if not vh and embeddings:
                vh = build_voice_hash(
                    average_embedding([e.get("vector", []) for e in embeddings if e.get("vector")])
                )
            if not vh:
                continue
            v3_profiles.append({
                "voice_hash": vh,
                "speaker_profile_id": v2p.get("speaker_profile_id") or vh,
                "embeddings": embeddings,
                "best_clip_path": v2p.get("best_clip_path"),
                "best_clip_source_file": v2p.get("best_clip_source_file"),
                "best_clip_score": v2p.get("best_clip_score"),
                "best_clip_updated_at": v2p.get("best_clip_updated_at"),
                "clip_history": [it for it in (v2p.get("clip_history") or []) if isinstance(it, dict)],
                "context_label": None,
                "created_at": v2p.get("updated_at"),
                "updated_at": v2p.get("updated_at"),
            })
            if vh and vh not in voiceprints:
                voiceprints.append(vh)
            for alias in v2p.get("observed_aliases") or []:
                if isinstance(alias, dict):
                    key = (alias.get("source"), str(alias.get("normalized_name") or "").casefold())
                    if key not in seen_aliases:
                        aliases.append(alias)
                        seen_aliases.add(key)
        persons.append({
            "person_id": person_id,
            "canonical_name": canonical,
            "display_name": rep.get("display_name"),
            "contact_ref": rep.get("contact_ref"),
            "contact_name": rep.get("contact_name"),
            "profiles": v3_profiles,
            "voiceprints": voiceprints,
            "observed_aliases": aliases,
            "created_at": None,
            "updated_at": None,
        })

    for v2p in unnamed:
        embeddings = [it for it in (v2p.get("embeddings") or []) if isinstance(it, dict)]
        vh = str(v2p.get("voice_hash") or "").strip()
        if not vh and embeddings:
            vh = build_voice_hash(
                average_embedding([e.get("vector", []) for e in embeddings if e.get("vector")])
            )
        if not vh:
            continue
        person_id = f"person_unnamed_{vh[3:15] if vh.startswith('vh_') else vh[:12]}"
        persons.append({
            "person_id": person_id,
            "canonical_name": None,
            "display_name": None,
            "contact_ref": None,
            "contact_name": None,
            "profiles": [{
                "voice_hash": vh,
                "speaker_profile_id": v2p.get("speaker_profile_id") or vh,
                "embeddings": embeddings,
                "best_clip_path": v2p.get("best_clip_path"),
                "best_clip_source_file": v2p.get("best_clip_source_file"),
                "best_clip_score": v2p.get("best_clip_score"),
                "best_clip_updated_at": v2p.get("best_clip_updated_at"),
                "clip_history": [it for it in (v2p.get("clip_history") or []) if isinstance(it, dict)],
                "context_label": None,
                "created_at": v2p.get("updated_at"),
                "updated_at": v2p.get("updated_at"),
            }],
            "voiceprints": [vh],
            "observed_aliases": [it for it in (v2p.get("observed_aliases") or []) if isinstance(it, dict)],
            "created_at": v2p.get("updated_at"),
            "updated_at": v2p.get("updated_at"),
        })

    return {
        "schema_version": VOICEPRINT_SCHEMA_VERSION,
        "persons": persons,
        "migrated_from": VOICEPRINT_SCHEMA_LEGACY_V2,
        "migration_date": dt.datetime.now(dt.UTC).isoformat(),
    }


def _find_person_by_voice_hash(store: dict, voice_hash: str) -> tuple[dict, dict] | None:
    """Return (person, profile) for the given voice_hash, or None."""
    for person in store.get("persons", []):
        for profile in person.get("profiles", []):
            if profile.get("voice_hash") == voice_hash:
                return person, profile
    return None


def _find_person_by_canonical_name(store: dict, canonical_name: str) -> dict | None:
    """Find a person by canonical_name (case-insensitive). Returns None if not found.

    Used for VTT auto-enroll: when a new voice_hash arrives but the speaker_name is
    already in the store under a different voice_hash, append the new profile to that
    person rather than creating a duplicate.
    """
    if not canonical_name:
        return None
    target = canonical_name.strip().casefold()
    for person in store.get("persons", []):
        if (person.get("canonical_name") or "").strip().casefold() == target:
            return person
    return None


def save_voiceprint_store_atomic(store_path: str, store: dict) -> None:
    path = pathlib.Path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def load_json_file(path: pathlib.Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default or {})
    return loaded if isinstance(loaded, dict) else dict(default or {})


def sanitize_token(value: str) -> str:
    safe = []
    for char in str(value or "").strip():
        if char.isalnum():
            safe.append(char.lower())
        elif char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    token = "".join(safe).strip("_")
    return token or "unknown"


CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def ascii_slug(value: str) -> str:
    raw = str(value or "").strip().lower()
    chunks = []
    for char in raw:
        if ("a" <= char <= "z") or ("0" <= char <= "9"):
            chunks.append(char)
        elif char in CYRILLIC_TO_LATIN:
            chunks.append(CYRILLIC_TO_LATIN[char])
        elif char in {"-", "_"}:
            chunks.append(char)
        else:
            chunks.append("_")
    token = re.sub(r"_+", "_", "".join(chunks)).strip("_")
    return token or "media"


def _copy_file_bounded(src: pathlib.Path, dst: pathlib.Path,
                       timeout_sec: int = 180, chunk: int = 4 * 1024 * 1024) -> None:
    """Copy ``src`` -> ``dst`` with a plain read/write loop under an optional timeout.

    Deliberately NOT ``shutil.copy2``: on macOS copy2 uses the ``fcopyfile`` fast-path,
    which deadlocks (EDEADLK / errno 11) or wedges on a Google Drive File Provider
    source. A chunked ``copyfileobj`` avoids fcopyfile. The SIGALRM budget turns a
    dataless source that never materializes into a fast failure — the watcher then
    defers the file (see ``_is_transient_cloud_error``) instead of the ASR job hanging
    forever. No-op timeout where SIGALRM is unavailable (Windows), which does not
    exhibit this hang.
    """
    armed = False
    if timeout_sec and hasattr(signal, "SIGALRM"):
        def _on_timeout(_signum, _frame):
            raise TimeoutError(
                f"input-ascii staging copy exceeded {timeout_sec}s — dataless source "
                f"not materialized (make it available-offline): {src}")
        signal.signal(signal.SIGALRM, _on_timeout)
        signal.alarm(timeout_sec)
        armed = True
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, chunk)
    finally:
        if armed:
            signal.alarm(0)


def stage_ascii_input(payload: dict, warnings: list[str], job_root: pathlib.Path) -> None:
    input_path = pathlib.Path(payload["input_path"])
    payload["original_input_path"] = str(input_path.resolve())
    payload["output_base_name"] = ascii_slug(input_path.stem)
    if all(ord(char) < 128 for char in str(input_path)):
        return

    stage_root = stage_dir(job_root, "input-ascii")
    staged_path = stage_root / f"{payload['output_base_name']}{input_path.suffix.lower()}"
    _copy_file_bounded(input_path, staged_path,
                       timeout_sec=int(payload.get("stage_copy_timeout_sec", 180)))
    payload["input_path"] = str(staged_path)
    warnings.append("unicode_source_path_workaround_ascii_copy")


def estimate_worker_memory_gb(payload: dict, runtime: dict) -> float:
    model = str(payload.get("selected_model") or payload.get("requested_model") or "medium")
    device = str(runtime.get("device") or "cpu")
    if device == "cuda":
        if model == "large-v3":
            return 4.0
        if model == "medium":
            return 2.5
        return 1.5
    if model == "large-v3":
        return 7.0
    if model == "medium":
        return 3.0
    return 1.5


def choose_parallelism(payload: dict, runtime: dict, queue_size: int) -> tuple[int, int]:
    requested = int(payload.get("max_parallel_chunks", 0) or 0)
    cpu_total = max(1, multiprocessing.cpu_count())
    memory = payload.get("environment", {}).get("memory", {}) if isinstance(payload.get("environment"), dict) else {}
    free_ram_gb = memory.get("freeRamGb")
    worker_memory_gb = estimate_worker_memory_gb(payload, runtime)
    if isinstance(free_ram_gb, (int, float)) and free_ram_gb > 0 and worker_memory_gb > 0:
        memory_limited = max(1, int(free_ram_gb // worker_memory_gb))
    else:
        memory_limited = 1

    cpu_limited = max(1, min(cpu_total, 8))
    if requested > 0:
        max_parallel = min(requested, queue_size, cpu_limited, memory_limited)
    else:
        max_parallel = min(queue_size, cpu_limited, memory_limited)
    max_parallel = max(1, max_parallel)
    max_parallel = min(max_parallel, MAX_PARALLEL_CHUNKS_HARD_CAP)
    cpu_threads = max(1, cpu_total // max_parallel)
    return max_parallel, cpu_threads


def resolve_chunk_future_timeout_sec(payload: dict) -> float | None:
    """Порог ожидания wait(FIRST_COMPLETED) между чанками; None = без таймаута."""
    raw = payload.get("chunk_future_timeout_sec")
    if raw is None:
        return DEFAULT_CHUNK_FUTURE_TIMEOUT_SEC
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CHUNK_FUTURE_TIMEOUT_SEC
    if value <= 0:
        return None
    return value


def create_whisper_model(job: dict) -> WhisperModel:
    model_kwargs = {
        "device": job["runtime_device"],
        "compute_type": job["runtime_compute_type"],
        "download_root": job.get("model_root"),
    }
    if job.get("cpu_threads") is not None:
        model_kwargs["cpu_threads"] = job["cpu_threads"]
    return WhisperModel(job["model_path"], **model_kwargs)


def build_speaker_review(result: dict) -> dict:
    grouped_segments: dict[str, list[dict]] = defaultdict(list)
    for segment in result.get("segments", []):
        speaker_id = segment.get("speaker_id")
        if speaker_id:
            grouped_segments[str(speaker_id)].append(segment)

    clip_by_speaker = {
        str(item.get("speaker_id")): item for item in result.get("speaker_clips", []) if item.get("speaker_id")
    }
    match_by_speaker = {
        str(key): value for key, value in (result.get("voiceprint", {}).get("matches") or {}).items()
    }

    speakers = []
    for speaker_id in sorted(set(grouped_segments) | set(clip_by_speaker)):
        segments = grouped_segments.get(speaker_id, [])
        first_text = next((seg.get("text") for seg in segments if seg.get("text")), None)
        current_name = None
        current_source = "unknown"
        for seg in segments:
            if seg.get("speaker_name"):
                current_name = seg.get("speaker_name")
                current_source = seg.get("speaker_source", current_source)
                break
        if current_name is None:
            current_name = speaker_id
        match = match_by_speaker.get(speaker_id) or {}
        clip = clip_by_speaker.get(speaker_id) or {}
        speakers.append(
            {
                "speaker_id": speaker_id,
                "current_name": current_name,
                "current_source": current_source,
                "num_segments": len(segments),
                "preview_clip_path": clip.get("clip_path"),
                "voice_hash": next((seg.get("voice_hash") for seg in segments if seg.get("voice_hash")), clip.get("profile_id")),
                "voiceprint_match": match,
                "needs_confirmation": current_source != "manual_map",
                "sample_text": first_text,
            }
        )
    return {
        "required": any(item.get("needs_confirmation") for item in speakers),
        "speakers": speakers,
    }


def build_display_blocks(segments: list[dict]) -> list[dict]:
    blocks: list[dict] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        speaker_label = segment.get("speaker_name") or segment.get("speaker_id") or segment.get("speaker")
        speaker_source = segment.get("speaker_source", "unknown")
        if (
            blocks
            and blocks[-1]["speaker_label"] == speaker_label
            and blocks[-1]["speaker_source"] == speaker_source
        ):
            blocks[-1]["end"] = segment["end"]
            blocks[-1]["texts"].append(text)
            continue
        blocks.append(
            {
                "start": segment["start"],
                "end": segment["end"],
                "speaker_label": speaker_label,
                "speaker_source": speaker_source,
                "texts": [text],
            }
        )
    return blocks


def extract_turn_embeddings(
    audio_path: str,
    turns: list[dict],
    ffmpeg_bin: str,
    job_root: pathlib.Path,
    max_samples_per_speaker: int = 5,
) -> tuple[dict[str, list[list[float]]], dict]:
    """Extract per-speaker embedding samples from audio turns.

    Returns (embeddings_by_speaker, extractor_meta) where extractor_meta describes
    which extractor was used (typically ECAPA-TDNN v1, 192-dim) so callers can
    persist the extractor version alongside the vectors.
    """
    temp_dir = stage_dir(job_root, "voiceprint-turns")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for turn in turns:
        grouped[turn["speaker_id"]].append(turn)

    result: dict[str, list[list[float]]] = defaultdict(list)
    extractor_meta: dict = {}
    for speaker_id, speaker_turns in grouped.items():
        selected = speaker_turns[:max_samples_per_speaker]
        for idx, turn in enumerate(selected):
            duration = max(0.2, float(turn["end"]) - float(turn["start"]))
            out_file = pathlib.Path(temp_dir) / f"{speaker_id.replace(' ', '_')}_{idx:02d}.wav"
            subprocess.run(
                [
                    ffmpeg_bin,
                    "-y",
                    "-ss",
                    f"{float(turn['start']):.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    audio_path,
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(out_file),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            vector, em = build_voice_embedding_from_wav(str(out_file))
            result[speaker_id].append(vector)
            if not extractor_meta:
                extractor_meta = {k: v for k, v in em.items() if k in {"extractor", "dim", "device"}}
    return result, extractor_meta


def average_embedding(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    size = len(vectors[0])
    acc = [0.0] * size
    for vector in vectors:
        if len(vector) != size:
            continue
        for idx, value in enumerate(vector):
            acc[idx] += value
    count = max(1, len(vectors))
    mean = [value / count for value in acc]
    norm = math.sqrt(sum(v * v for v in mean))
    if norm > 0:
        mean = [v / norm for v in mean]
    return [round(v, 8) for v in mean]


def build_voice_hash(vector: list[float]) -> str:
    rounded = [round(float(value), 6) for value in vector or []]
    payload = json.dumps(rounded, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"vh_{digest[:24]}"


def match_voiceprint_profiles(
    store: dict,
    speaker_vectors: dict[str, list[float]],
    threshold: float,
    extractor: str | None = None,
) -> dict:
    """Match speaker vectors against persons in the store (schema v3).

    For each query vector, iterate all persons; within each person, take the BEST
    cosine score across that person's profiles (multi-profile per person — Bug 4
    architecture). The winning person is the one with highest best-of-profiles cosine.

    When `extractor` is provided (e.g. "ecapa_tdnn_v1"), only embeddings tagged
    with the same extractor are considered for the centroid — so the new ECAPA-TDNN
    vectors are never compared against legacy 12-dim acoustic-stats embeddings.

    Bands are tied to the extractor's threshold: high ≥ threshold + 0.10,
    medium ≥ threshold, low otherwise.
    """
    def confidence_band(score: float) -> str:
        if score >= threshold + 0.10:
            return "high"
        if score >= threshold:
            return "medium"
        return "low"

    def _filter_embeddings(raw_embeddings):
        if extractor:
            return [
                item.get("vector", [])
                for item in raw_embeddings
                if item.get("vector") and (
                    item.get("extractor") == extractor
                    or (extractor == VOICE_EMBEDDING_EXTRACTOR_V0_LEGACY and not item.get("extractor"))
                )
            ]
        return [item.get("vector", []) for item in raw_embeddings if item.get("vector")]

    persons = store.get("persons", [])
    matches = {}
    for speaker_id, vector in speaker_vectors.items():
        best = None  # best across all persons
        for person in persons:
            person_best_score = 0.0
            person_best_profile = None
            for profile in person.get("profiles", []):
                filtered = _filter_embeddings(profile.get("embeddings", []) or [])
                centroid = average_embedding(filtered)
                if not centroid:
                    continue
                score = cosine_similarity(vector, centroid)
                if score > person_best_score:
                    person_best_score = score
                    person_best_profile = profile
            if person_best_profile is None:
                continue
            score = person_best_score
            if best is None or score > best["score"]:
                best = {
                    "voice_hash": person_best_profile.get("voice_hash"),
                    "speaker_profile_id": person_best_profile.get("speaker_profile_id") or person_best_profile.get("voice_hash"),
                    "person_id": person.get("person_id"),
                    "canonical_name": person.get("canonical_name"),
                    "display_name": person.get("display_name"),
                    "contact_ref": person.get("contact_ref"),
                    "contact_name": person.get("contact_name"),
                    "score": round(score, 6),
                    "matched_profile_count": len(person.get("profiles", [])),
                }
        if best and best["score"] >= threshold:
            linked = bool(best.get("contact_ref") or best.get("contact_name"))
            matches[speaker_id] = {
                "matched": True,
                "voice_hash": best["voice_hash"],
                "speaker_profile_id": best.get("speaker_profile_id"),
                "canonical_name": best.get("canonical_name"),
                "display_name": best["display_name"],
                "contact_ref": best.get("contact_ref"),
                "contact_name": best.get("contact_name"),
                "contact_link_status": "linked" if linked else "unlinked",
                "score": best["score"],
                "confidence_band": confidence_band(best["score"]),
            }
        else:
            linked = bool(best and (best.get("contact_ref") or best.get("contact_name")))
            matches[speaker_id] = {
                "matched": False,
                "voice_hash": best["voice_hash"] if best else None,
                "speaker_profile_id": best.get("speaker_profile_id") if best else None,
                "canonical_name": best.get("canonical_name") if best else None,
                "display_name": best["display_name"] if best else None,
                "contact_ref": best.get("contact_ref") if best else None,
                "contact_name": best.get("contact_name") if best else None,
                "contact_link_status": "linked" if linked else "unlinked",
                "score": best["score"] if best else 0.0,
                "confidence_band": confidence_band(best["score"] if best else 0.0),
            }
    return matches


def enroll_voiceprint_profile(
    store: dict,
    speaker_id: str,
    speaker_vectors: dict[str, list[float]],
    sample_meta: dict,
    enroll_meta: dict | None = None,
) -> dict:
    """Enroll a voiceprint profile. Optionally attach canonical identity via enroll_meta.

    enroll_meta keys (all optional): canonical_name, display_name, contact_ref, contact_name.
    On idempotent re-enroll: adds embedding to existing profile + fills missing identity
    fields from enroll_meta but does NOT overwrite already-set values (manual edits win).
    """
    vector = speaker_vectors.get(speaker_id)
    if not vector:
        raise RuntimeError(f"cannot enroll: no embedding for {speaker_id}")
    voice_hash = build_voice_hash(vector)
    extractor_tag = (sample_meta or {}).get("extractor") or VOICE_EMBEDDING_DEFAULT_EXTRACTOR
    # Delegate to v3-aware ensure_profile_entry — same logic as auto-enroll-from-VTT,
    # but with sample_meta forwarded as-is.
    profile = ensure_profile_entry(
        store,
        voice_hash,
        vector=vector,
        enroll_meta=enroll_meta,
        sample_meta=sample_meta,
        extractor=extractor_tag,
    )
    contact_link_status = "linked" if (profile.get("canonical_name") or profile.get("contact_name")) else "unlinked"
    return {
        "voice_hash": voice_hash,
        "speaker_profile_id": voice_hash,
        "person_id": profile.get("person_id"),
        "contact_link_status": contact_link_status,
        "speaker_id": speaker_id,
    }


def clip_candidate_score(intervals: list[dict], target_sec: float) -> tuple[float, float, float]:
    if not intervals:
        return (-1.0, -1.0, -1.0)
    total = sum(max(0.0, float(item["end"]) - float(item["start"])) for item in intervals)
    longest = max(max(0.0, float(item["end"]) - float(item["start"])) for item in intervals)
    fragmentation_penalty = max(0, len(intervals) - 1)
    closeness = min(total, target_sec)
    return (round(longest, 6), round(closeness, 6), -float(fragmentation_penalty))


def pick_speaker_clip_intervals(turns: list[dict], target_sec: float, min_turn_sec: float) -> tuple[list[dict], str]:
    eligible = []
    for turn in turns:
        duration = max(0.0, float(turn["end"]) - float(turn["start"]))
        if duration >= min_turn_sec:
            eligible.append(
                {
                    "start": round(float(turn["start"]), 3),
                    "end": round(float(turn["end"]), 3),
                    "duration": round(duration, 3),
                }
            )
    if not eligible:
        return [], "insufficient_turns"

    best_single = None
    for turn in eligible:
        if turn["duration"] >= target_sec:
            candidate = [{"start": turn["start"], "end": round(turn["start"] + target_sec, 3)}]
            if best_single is None or clip_candidate_score(candidate, target_sec) > clip_candidate_score(best_single, target_sec):
                best_single = candidate
    if best_single:
        return best_single, "single_window"

    best = None
    for start_idx in range(len(eligible)):
        total = 0.0
        candidate = []
        for idx in range(start_idx, len(eligible)):
            turn = eligible[idx]
            remaining = max(0.0, target_sec - total)
            if remaining <= 0:
                break
            take = min(turn["duration"], remaining)
            candidate.append({"start": turn["start"], "end": round(turn["start"] + take, 3)})
            total += take
            if total >= target_sec:
                break
        if not candidate:
            continue
        if best is None or clip_candidate_score(candidate, target_sec) > clip_candidate_score(best, target_sec):
            best = candidate

    if not best:
        return [], "insufficient_turns"
    if len(best) == 1:
        return best, "single_window"
    return best, "concat_best_fragments"


def export_speaker_video_clip(
    input_path: str,
    clip_path: pathlib.Path,
    intervals: list[dict],
    ffmpeg_bin: str,
    job_root: pathlib.Path,
    clip_crf: int,
    clip_preset: str,
) -> None:
    if not intervals:
        raise RuntimeError("no intervals selected for clip export")
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    if len(intervals) == 1:
        item = intervals[0]
        duration = max(0.05, float(item["end"]) - float(item["start"]))
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{float(item['start']):.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                input_path,
                "-c:v",
                "libx264",
                "-preset",
                clip_preset,
                "-crf",
                str(int(clip_crf)),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(clip_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return

    concat_root = stage_dir(job_root, "speaker-clip-concat")
    part_paths = []
    for idx, item in enumerate(intervals):
        duration = max(0.05, float(item["end"]) - float(item["start"]))
        part_path = concat_root / f"{clip_path.stem}_part_{idx:03d}.mp4"
        subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-ss",
                f"{float(item['start']):.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                input_path,
                "-c:v",
                "libx264",
                "-preset",
                clip_preset,
                "-crf",
                str(int(clip_crf)),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(part_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        part_paths.append(part_path)

    manifest = concat_root / f"{clip_path.stem}_concat.txt"
    manifest_lines = []
    for path in part_paths:
        safe_path = str(path).replace("'", "'\\''")
        manifest_lines.append(f"file '{safe_path}'")
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c",
            "copy",
            str(clip_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ensure_profile_entry(
    store: dict,
    voice_hash: str,
    *,
    vector: list[float] | None = None,
    enroll_meta: dict | None = None,
    sample_meta: dict | None = None,
    extractor: str | None = None,
) -> dict:
    """Find or create a voiceprint profile inside the appropriate person (schema v3).

    Resolution order:
      1. By voice_hash: search all persons, return the (person, profile) that owns it.
         Append vector to that profile's embeddings; fill missing identity on person
         from enroll_meta.
      2. By canonical_name (from enroll_meta): if voice_hash unknown but the person
         exists by name, append a NEW profile to that person (Bug 4 multi-profile
         per person — different recordings of the same speaker).
      3. Otherwise: create a new person with one profile.

    Returns a dict that combines person-level identity + profile-level fields, for
    backward compat with callers that expect a single "profile" dict.
    """
    persons = store.setdefault("persons", [])
    em = enroll_meta or {}
    now = dt.datetime.now(dt.UTC).isoformat()
    has_vector = isinstance(vector, list) and len(vector) > 0
    extractor_tag = extractor or VOICE_EMBEDDING_DEFAULT_EXTRACTOR

    def _emb_entry():
        return {
            "vector": vector,
            "created_at": now,
            "sample_meta": sample_meta or {},
            "extractor": extractor_tag,
            "dim": len(vector),
        }

    def _new_profile():
        return {
            "voice_hash": voice_hash,
            "speaker_profile_id": voice_hash,
            "embeddings": [_emb_entry()] if has_vector else [],
            "best_clip_path": None,
            "best_clip_source_file": None,
            "best_clip_score": None,
            "best_clip_updated_at": None,
            "clip_history": [],
            "context_label": None,
            "created_at": now,
            "updated_at": now if has_vector else None,
        }

    def _fill_person_identity(person: dict):
        for key in ("canonical_name", "display_name", "contact_ref", "contact_name"):
            if not person.get(key) and em.get(key):
                person[key] = em[key]
        if not person.get("canonical_name") and (
            person.get("contact_name") or person.get("display_name")
        ):
            person["canonical_name"] = person.get("contact_name") or person.get("display_name")
        person["updated_at"] = now
        if voice_hash not in (person.setdefault("voiceprints", [])):
            person["voiceprints"].append(voice_hash)

    def _annotate_profile(person: dict, profile: dict) -> dict:
        # Mutate the REAL profile dict (not a copy) so downstream maybe_update_profile_clip
        # writes propagate back to the store. Identity fields are copied as a read-only
        # convenience on the profile so legacy callers that read profile["canonical_name"]
        # still work — but identity edits should go through the person object.
        profile["person_id"] = person.get("person_id")
        profile["canonical_name"] = person.get("canonical_name")
        profile["display_name"] = person.get("display_name")
        profile["contact_ref"] = person.get("contact_ref")
        profile["contact_name"] = person.get("contact_name")
        return profile

    # 1. By voice_hash
    found = _find_person_by_voice_hash(store, voice_hash)
    if found is not None:
        person, profile = found
        if has_vector:
            profile.setdefault("embeddings", []).append(_emb_entry())
            profile["updated_at"] = now
        _fill_person_identity(person)
        return _annotate_profile(person, profile)

    # 2. By canonical_name (Bug 4 multi-profile per person)
    canonical = em.get("canonical_name")
    if canonical:
        person = _find_person_by_canonical_name(store, canonical)
        if person is not None:
            new_profile = _new_profile()
            person.setdefault("profiles", []).append(new_profile)
            _fill_person_identity(person)
            return _annotate_profile(person, new_profile)

    # 3. New person
    def _slugify(s: str) -> str:
        out = []
        for c in s or "":
            if c.isalnum() or c in "_":
                out.append(c.lower())
            elif c.isspace() or c in ".,":
                out.append("-")
        result = "".join(out).strip("-")
        while "--" in result:
            result = result.replace("--", "-")
        return result[:60]

    if canonical:
        person_id = f"person_{_slugify(canonical) or voice_hash[3:15]}"
    else:
        person_id = f"person_unnamed_{voice_hash[3:15] if voice_hash.startswith('vh_') else voice_hash[:12]}"

    new_profile = _new_profile()
    new_person = {
        "person_id": person_id,
        "canonical_name": em.get("canonical_name"),
        "display_name": em.get("display_name"),
        "contact_ref": em.get("contact_ref"),
        "contact_name": em.get("contact_name"),
        "profiles": [new_profile],
        "voiceprints": [voice_hash],
        "observed_aliases": [],
        "created_at": now,
        "updated_at": now,
    }
    persons.append(new_person)
    return _annotate_profile(new_person, new_profile)


def record_profile_alias_observation(
    profile: dict,
    *,
    raw_name: str,
    normalized_name: str,
    source: str,
    observed_at: str,
    store: dict | None = None,
) -> dict | None:
    """Record an alias observation. In schema v3, aliases live at the person level.

    If `store` is provided, the function locates the owning person by voice_hash and
    writes the alias into `person.observed_aliases`. Otherwise (legacy / direct
    invocation), it falls back to the profile-level list for backward compat.
    """
    # Schema v3: redirect to person-level alias list
    if store is not None and isinstance(store.get("persons"), list):
        vh = profile.get("voice_hash")
        if vh:
            found = _find_person_by_voice_hash(store, vh)
            if found is not None:
                profile = found[0]  # person dict — alias writes go here
    raw_value = str(raw_name or "").strip()
    normalized_value = str(normalized_name or "").strip()
    if not normalized_value:
        return None
    aliases = profile.setdefault("observed_aliases", [])
    existing = next(
        (
            item
            for item in aliases
            if str(item.get("source") or "").strip() == source
            and str(item.get("normalized_name") or "").strip().casefold() == normalized_value.casefold()
        ),
        None,
    )
    if existing is None:
        existing = {
            "source": source,
            "raw_name": raw_value or normalized_value,
            "normalized_name": normalized_value,
            "count": 0,
            "first_seen_at": observed_at,
            "last_seen_at": observed_at,
        }
        aliases.append(existing)
    existing["count"] = int(existing.get("count") or 0) + 1
    existing["last_seen_at"] = observed_at
    if raw_value:
        existing["raw_name"] = raw_value
    profile["updated_at"] = observed_at
    return {
        "speaker_profile_id": profile.get("speaker_profile_id") or profile.get("voice_hash"),
        "voice_hash": profile.get("voice_hash"),
        "source": source,
        "raw_name": raw_value or normalized_value,
        "normalized_name": normalized_value,
        "count": existing["count"],
    }


def resolve_session_artifact_dir(payload: dict) -> pathlib.Path:
    explicit = str(payload.get("session_artifact_dir") or "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    return pathlib.Path(payload["output_dir"]).resolve()


def resolve_identification(payload: dict) -> dict:
    """Build run_meta.identification: project_id, course_code, product_path, inbox_path,
    vault_target_folder, workshop_outputs_folder, source_signal, confidence.

    Reads explicit ``payload['identification']`` if provided, fills defaults from sibling
    payload fields (project_id, course_code, inbox_path, input_path). Downstream steps
    (transcription-processing / process-lecture / team-meeting) read this block from
    run_meta to pick the right vault folders without re-deriving them.

    The Inbox folder is always a parameter (CLI ``--inbox`` or ``payload.inbox_path``),
    never a vault canon — see media-transcription SKILL for the full identification policy.
    """
    explicit = payload.get("identification") if isinstance(payload.get("identification"), dict) else {}
    result = dict(explicit)

    project_id = str(result.get("project_id") or payload.get("project_id") or "").strip()
    course_code = str(result.get("course_code") or payload.get("course_code") or "").strip().lower()
    inbox_path = str(result.get("inbox_path") or payload.get("inbox_path") or "").strip()
    input_path = str(payload.get("input_path") or "").strip()

    course_to_product = {
        "mdpg": "25-CM-MDPG",
        "mdpg-4": "25-CM-MDPG/504-CM-AI-Empowered-Team",
        "504": "25-CM-MDPG/504-CM-AI-Empowered-Team",
        "stai": "12-CM-STAI",
        "514": "12-CM-STAI/514-CM-MIPT",
        "e101": "519-CM-E101",
        "519": "519-CM-E101",
    }
    product_path = result.get("product_path")
    if not product_path and course_code in course_to_product:
        product_path = course_to_product[course_code]
    if not product_path and project_id in course_to_product:
        product_path = course_to_product[project_id]

    if not result.get("vault_target_folder") and project_id:
        import datetime as _dt
        year_match = re.search(r"(20\d{2})[-_]?\d{2}[-_]?\d{2}", input_path) if input_path else None
        year = year_match.group(1) if year_match else str(_dt.date.today().year)
        result["vault_target_folder"] = f"{year}/{project_id}/"

    if not result.get("workshop_outputs_folder") and product_path:
        result["workshop_outputs_folder"] = f"MS-Courses/{product_path}/Workshop-Outputs/"

    if "source_signal" not in result:
        signals: list[str] = []
        if explicit and (explicit.get("project_id") or explicit.get("course_code")):
            signals.append("explicit")
        if payload.get("project_id"):
            signals.append("payload.project_id")
        if payload.get("course_code"):
            signals.append("payload.course_code")
        if inbox_path:
            signals.append("inbox_path")
        if input_path:
            signals.append("input_path")
        result["source_signal"] = signals or ["unresolved"]

    if "confidence" not in result:
        n = len([s for s in result.get("source_signal", []) if s != "unresolved"])
        result["confidence"] = "high" if n >= 2 else ("medium" if n == 1 else "low")

    result["project_id"] = project_id or None
    result["course_code"] = course_code or None
    result["product_path"] = product_path or None
    result["inbox_path"] = inbox_path or None
    return result


def resolve_project_speaker_registry_dir(payload: dict) -> pathlib.Path | None:
    explicit = str(payload.get("project_speaker_registry_path") or "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    session_dir = resolve_session_artifact_dir(payload)
    parent = session_dir.parent
    if not str(parent):
        return None
    return (parent / "Speakers").resolve()


def ensure_project_registry_layout(registry_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    registry_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = registry_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    index_path = registry_dir / "index.json"
    if not index_path.exists():
        write_text_atomic(
            index_path,
            json.dumps(
                {
                    "schema_version": "speaker-registry-v1",
                    "profiles": [],
                    "updated_at": None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return profiles_dir, index_path


def project_profile_card_from_voiceprint(profile: dict, project_id: str | None = None) -> dict:
    aliases = [item for item in (profile.get("observed_aliases") or []) if isinstance(item, dict)]
    return {
        "schema_version": "speaker-profile-v1",
        "speaker_profile_id": profile.get("speaker_profile_id") or profile.get("voice_hash"),
        "canonical_name": profile.get("canonical_name"),
        "display_name": profile.get("display_name"),
        "contact_ref": profile.get("contact_ref"),
        "contact_name": profile.get("contact_name"),
        "voice_hash": profile.get("voice_hash"),
        "voiceprints": [str(item).strip() for item in (profile.get("voiceprints") or []) if str(item).strip()],
        "observed_aliases": aliases,
        "best_clip_path": profile.get("best_clip_path"),
        "best_clip_source_file": profile.get("best_clip_source_file"),
        "best_clip_updated_at": profile.get("best_clip_updated_at"),
        "best_clip_score": profile.get("best_clip_score"),
        "clip_history": [item for item in (profile.get("clip_history") or []) if isinstance(item, dict)],
        "updated_at": profile.get("updated_at"),
        "revision": profile.get("updated_at"),
        "origin_project_id": profile.get("origin_project_id") or project_id,
        "import_history": [item for item in (profile.get("import_history") or []) if isinstance(item, dict)],
    }


def sync_profile_to_project_registry(profile: dict, registry_dir: pathlib.Path, project_id: str | None = None) -> dict:
    profiles_dir, index_path = ensure_project_registry_layout(registry_dir)
    speaker_profile_id = str(profile.get("speaker_profile_id") or profile.get("voice_hash") or "").strip()
    if not speaker_profile_id:
        raise RuntimeError("project_registry_sync_requires_speaker_profile_id")
    card = project_profile_card_from_voiceprint(profile, project_id=project_id)
    profile_path = profiles_dir / f"{sanitize_token(speaker_profile_id)}.json"
    write_text_atomic(profile_path, json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    index = load_json_file(index_path, {"schema_version": "speaker-registry-v1", "profiles": []})
    entries = index.get("profiles", [])
    if not isinstance(entries, list):
        entries = []
    summary = {
        "speaker_profile_id": speaker_profile_id,
        "canonical_name": card.get("canonical_name"),
        "contact_name": card.get("contact_name"),
        "display_name": card.get("display_name"),
        "voice_hash": card.get("voice_hash"),
        "voiceprints": card.get("voiceprints", []),
        "aliases": [
            item.get("normalized_name")
            for item in card.get("observed_aliases", [])
            if isinstance(item, dict) and item.get("normalized_name")
        ],
        "path": str(profile_path),
        "updated_at": card.get("updated_at"),
        "revision": card.get("revision"),
    }
    replaced = False
    for idx, item in enumerate(entries):
        if not isinstance(item, dict):
            continue
        if str(item.get("speaker_profile_id") or "").strip() == speaker_profile_id:
            entries[idx] = summary
            replaced = True
            break
    if not replaced:
        entries.append(summary)
    index["profiles"] = sorted(entries, key=lambda item: str(item.get("speaker_profile_id") or ""))
    index["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    write_text_atomic(index_path, json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "speaker_profile_id": speaker_profile_id,
        "profile_path": str(profile_path),
        "index_path": str(index_path),
        "canonical_name": card.get("canonical_name"),
        "voice_hash": card.get("voice_hash"),
    }


def sync_profile_to_global_registry(
    profile: dict, global_dir: pathlib.Path, project_id: str | None = None
) -> dict:
    """Write the FULL v3 profile (WITH embeddings) to the shared hub registry.

    Unlike the project projection card (project_profile_card_from_voiceprint, which
    strips vectors), the global registry keeps the canonical embeddings so any node can
    read them. Layout: {global_dir}/profiles/{voice_hash}.json (the profile) plus
    {global_dir}/registry.json (a per-voice_hash index for pull-on-claim). Open, not
    encrypted (MVP); kept out of git via .gitignore (**/_voiceprints/).

    In this phase the write is a plain overwrite by the enrolling node — no merge-union
    across concurrent nodes yet (that is phase 3).
    """
    voice_hash = str(profile.get("voice_hash") or "").strip()
    if not voice_hash:
        raise RuntimeError("global_registry_sync_requires_voice_hash")
    now = dt.datetime.now(dt.UTC).isoformat()
    profiles_dir = global_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    record = {
        "schema_version": "voiceprint-global-v1",
        "voice_hash": voice_hash,
        "person_id": profile.get("person_id"),
        "canonical_name": profile.get("canonical_name"),
        "display_name": profile.get("display_name"),
        "contact_ref": profile.get("contact_ref"),
        "contact_name": profile.get("contact_name"),
        "speaker_profile_id": profile.get("speaker_profile_id") or voice_hash,
        "embeddings": [item for item in (profile.get("embeddings") or []) if isinstance(item, dict)],
        "clip_history": [item for item in (profile.get("clip_history") or []) if isinstance(item, dict)],
        "origin_project_id": profile.get("origin_project_id") or project_id,
        "created_at": profile.get("created_at"),
        "updated_at": now,
    }
    profile_path = profiles_dir / f"{sanitize_token(voice_hash)}.json"
    write_text_atomic(profile_path, json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    registry_path = global_dir / "registry.json"
    registry = load_json_file(
        registry_path,
        {"schema_version": "voiceprint-registry-v1", "persons": [], "updated_at": None},
    )
    entries = registry.get("persons")
    if not isinstance(entries, list):
        entries = []
    summary = {
        "voice_hash": voice_hash,
        "person_id": record["person_id"],
        "canonical_name": record["canonical_name"],
        "profile_path": str(profile_path),
        "origin_project_id": record["origin_project_id"],
        "embedding_count": len(record["embeddings"]),
        "updated_at": now,
    }
    replaced = False
    for idx, item in enumerate(entries):
        if isinstance(item, dict) and str(item.get("voice_hash") or "").strip() == voice_hash:
            entries[idx] = summary
            replaced = True
            break
    if not replaced:
        entries.append(summary)
    registry["persons"] = sorted(entries, key=lambda item: str(item.get("voice_hash") or ""))
    registry["updated_at"] = now
    write_text_atomic(registry_path, json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"voice_hash": voice_hash, "profile_path": str(profile_path), "registry_path": str(registry_path)}


def sync_project_members(
    person_id: str | None,
    canonical_name: str | None,
    voice_hash: str,
    registry_dir: pathlib.Path,
    project_id: str | None = None,
) -> dict:
    """Upsert a project member (person_id → voice_hashes + link to the global profile).

    members.json is the per-project roster used to bound matching to project participants
    (phase 2). Stored alongside the projection (index.json/profiles/) in registry_dir.
    voice_hashes is append-only (union); canonical_name is not overwritten once set.
    """
    voice_hash = str(voice_hash or "").strip()
    person_id = str(person_id or "").strip() or f"person_unnamed_{voice_hash[:12]}"
    now = dt.datetime.now(dt.UTC).isoformat()
    registry_dir.mkdir(parents=True, exist_ok=True)
    members_path = registry_dir / "members.json"
    doc = load_json_file(
        members_path,
        {"schema_version": "members-v1", "project_id": project_id, "members": [], "updated_at": None},
    )
    if not doc.get("project_id") and project_id:
        doc["project_id"] = project_id
    members = doc.get("members")
    if not isinstance(members, list):
        members = []
    global_link = f"_voiceprints/profiles/{voice_hash}.json" if voice_hash else None
    existing = None
    for item in members:
        if isinstance(item, dict) and str(item.get("person_id") or "").strip() == person_id:
            existing = item
            break
    if existing is None:
        existing = {
            "person_id": person_id,
            "canonical_name": canonical_name,
            "voice_hashes": [],
            "global_link": global_link,
            "created_at": now,
        }
        members.append(existing)
    hashes = [h for h in existing.get("voice_hashes", []) if isinstance(h, str)]
    if voice_hash and voice_hash not in hashes:
        hashes.append(voice_hash)
    existing["voice_hashes"] = hashes
    if not existing.get("canonical_name") and canonical_name:
        existing["canonical_name"] = canonical_name
    if global_link:
        existing["global_link"] = global_link
    existing["updated_at"] = now
    doc["members"] = sorted(members, key=lambda item: str(item.get("person_id") or ""))
    doc["updated_at"] = now
    write_text_atomic(members_path, json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"person_id": person_id, "members_path": str(members_path), "voice_hash": voice_hash}


def _embedding_signature(embedding: dict) -> str:
    """Content signature of an embedding for merge-union dedup (idempotent pulls)."""
    vector = embedding.get("vector") if isinstance(embedding, dict) else None
    if not isinstance(vector, list) or not vector:
        return ""
    payload = json.dumps([round(float(x), 6) for x in vector], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sync_hub_to_local_cache(
    store: dict, project_registry_dir: pathlib.Path, global_registry_dir: pathlib.Path
) -> dict:
    """Pull canonical profiles from the shared hub into the node-local store (in-memory).

    The reverse of sync_profile_to_global_registry: reads the project roster
    ({project_registry_dir}/members.json), loads each participant's canonical profile
    from {global_registry_dir}/profiles/{voice_hash}.json, and merges it into `store`
    (schema v3) by voice_hash — union of embeddings (dedup by content signature so
    repeated pulls are idempotent). Matching stays bounded to project participants
    because the local store is per-project ({cache_root}/{pid}/voiceprints.json).

    Manual local edits win: an existing non-empty canonical_name is never overwritten.
    Mutates `store` in place; the caller persists it. Returns a meta summary.
    """
    members_path = project_registry_dir / "members.json"
    doc = load_json_file(members_path, {"members": []})
    members = doc.get("members") if isinstance(doc.get("members"), list) else []
    profiles_dir = global_registry_dir / "profiles"
    now = dt.datetime.now(dt.UTC).isoformat()
    persons_pulled = 0
    embeddings_merged = 0
    missing = 0

    for member in members:
        if not isinstance(member, dict):
            continue
        for voice_hash in member.get("voice_hashes") or []:
            voice_hash = str(voice_hash or "").strip()
            if not voice_hash:
                continue
            record_path = profiles_dir / f"{sanitize_token(voice_hash)}.json"
            if not record_path.exists():
                missing += 1
                continue
            record = load_json_file(record_path, {})
            if not record:
                missing += 1
                continue
            incoming = [e for e in (record.get("embeddings") or []) if isinstance(e, dict) and e.get("vector")]

            found = _find_person_by_voice_hash(store, voice_hash)
            if found is not None:
                person, profile = found
            else:
                person = None
                person_id = str(record.get("person_id") or "").strip()
                if person_id:
                    for candidate in store.get("persons", []):
                        if str(candidate.get("person_id") or "").strip() == person_id:
                            person = candidate
                            break
                new_profile = {
                    "voice_hash": voice_hash,
                    "speaker_profile_id": record.get("speaker_profile_id") or voice_hash,
                    "embeddings": [],
                    "best_clip_path": None,
                    "best_clip_source_file": None,
                    "best_clip_score": None,
                    "best_clip_updated_at": None,
                    "clip_history": [c for c in (record.get("clip_history") or []) if isinstance(c, dict)],
                    "context_label": None,
                    "created_at": record.get("created_at") or now,
                    "updated_at": now,
                }
                if person is None:
                    person = {
                        "person_id": person_id or f"person_unnamed_{voice_hash[:12]}",
                        "canonical_name": record.get("canonical_name"),
                        "display_name": record.get("display_name"),
                        "contact_ref": record.get("contact_ref"),
                        "contact_name": record.get("contact_name"),
                        "profiles": [],
                        "voiceprints": [],
                        "observed_aliases": [],
                        "created_at": now,
                        "updated_at": now,
                    }
                    store.setdefault("persons", []).append(person)
                    persons_pulled += 1
                person.setdefault("profiles", []).append(new_profile)
                profile = new_profile
                if voice_hash not in person.setdefault("voiceprints", []):
                    person["voiceprints"].append(voice_hash)

            # Fill identity only where locally empty (manual edits win).
            for key in ("canonical_name", "display_name", "contact_ref", "contact_name"):
                if not person.get(key) and record.get(key):
                    person[key] = record[key]

            # Union embeddings by content signature (idempotent across repeated pulls).
            existing_sigs = {_embedding_signature(e) for e in profile.get("embeddings", [])}
            for emb in incoming:
                sig = _embedding_signature(emb)
                if sig and sig not in existing_sigs:
                    profile.setdefault("embeddings", []).append(emb)
                    existing_sigs.add(sig)
                    embeddings_merged += 1
            profile["updated_at"] = now

    status = "updated" if (persons_pulled or embeddings_merged) else "not_needed"
    return {
        "status": status,
        "persons_pulled": persons_pulled,
        "embeddings_merged": embeddings_merged,
        "records_missing": missing,
    }


def maybe_update_profile_clip(
    profile: dict,
    *,
    source_file: str,
    clip_path: str,
    intervals: list[dict],
    selection_method: str,
) -> dict:
    now = dt.datetime.now(dt.UTC).isoformat()
    total = sum(max(0.0, float(item["end"]) - float(item["start"])) for item in intervals)
    longest = max((max(0.0, float(item["end"]) - float(item["start"])) for item in intervals), default=0.0)
    fragments = len(intervals)
    candidate_score = [round(longest, 6), round(total, 6), -int(fragments)]
    history_item = {
        "clip_path": clip_path,
        "source_file": source_file,
        "selection_method": selection_method,
        "intervals": intervals,
        "updated_at": now,
        "score": candidate_score,
    }
    profile.setdefault("clip_history", []).append(history_item)
    current_score = profile.get("best_clip_score")
    if current_score is None or tuple(candidate_score) > tuple(current_score):
        profile["best_clip_path"] = clip_path
        profile["best_clip_source_file"] = source_file
        profile["best_clip_updated_at"] = now
        profile["best_clip_score"] = candidate_score
        profile["updated_at"] = now
        return {"updated": True, "reason": "better_clip"}
    return {"updated": False, "reason": "existing_clip_kept"}


def generate_speaker_clips(
    payload: dict,
    turns: list[dict],
    result: dict,
    warnings: list[str],
    job_root: pathlib.Path,
    voiceprint_store: dict | None = None,
) -> dict:
    if pathlib.Path(payload["input_path"]).suffix.lower() not in VIDEO_EXTENSIONS:
        return {
            "status": "skipped",
            "reason": "source_not_video",
            "target_sec": float(payload.get("speaker_clip_target_sec", 60) or 60),
            "storage_mode": payload.get("speaker_clip_dir_mode", "both"),
            "clips_generated": 0,
            "clips_failed": 0,
            "clips": [],
        }
    if not turns:
        return {
            "status": "skipped",
            "reason": "skipped_no_diarization_turns",
            "target_sec": float(payload.get("speaker_clip_target_sec", 60) or 60),
            "storage_mode": payload.get("speaker_clip_dir_mode", "both"),
            "clips_generated": 0,
            "clips_failed": 0,
            "clips": [],
        }

    target_sec = float(payload.get("speaker_clip_target_sec", 60) or 60)
    min_turn_sec = float(payload.get("speaker_clip_min_turn_sec", 2.0) or 2.0)
    clip_crf = int(payload.get("speaker_clip_crf", 18) or 18)
    clip_preset = str(payload.get("speaker_clip_preset") or "medium")
    output_dir = pathlib.Path(payload["output_dir"])
    clip_dir = pathlib.Path(payload.get("speaker_clip_output_dir") or (output_dir.parent / "speaker-clips"))
    clip_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = payload.get("ffmpeg_bin") or "ffmpeg"
    storage_mode = str(payload.get("speaker_clip_dir_mode") or "both")
    speaker_groups: dict[str, list[dict]] = defaultdict(list)
    for turn in turns:
        speaker_groups[str(turn["speaker_id"])].append(turn)

    clips = []
    clips_generated = 0
    clips_failed = 0
    source_file = result["source_file"]
    base_name = pathlib.Path(payload["input_path"]).stem

    for speaker_id, speaker_turns in sorted(speaker_groups.items()):
        intervals, selection_method = pick_speaker_clip_intervals(speaker_turns, target_sec, min_turn_sec)
        speaker_name = next((seg.get("speaker_name") for seg in result["segments"] if seg.get("speaker_id") == speaker_id and seg.get("speaker_name")), speaker_id)
        speaker_source = next((seg.get("speaker_source") for seg in result["segments"] if seg.get("speaker_id") == speaker_id and seg.get("speaker_source")), "unknown")
        clip_entry = {
            "speaker_id": speaker_id,
            "speaker_name": speaker_name,
            "speaker_source": speaker_source,
            "selection_method": selection_method,
            "source_intervals": intervals,
            "duration_sec": round(sum(max(0.0, float(item["end"]) - float(item["start"])) for item in intervals), 3),
            "clip_path": None,
            "profile_id": None,
            "profile_link_status": "unlinked",
            "profile_clip_path": None,
        }
        if not intervals:
            clips_failed += 1
            clip_entry["error"] = "insufficient_turns"
            clips.append(clip_entry)
            continue

        clip_filename = f"{sanitize_token(base_name)}-{sanitize_token(speaker_id)}-clip.mp4"
        clip_path = clip_dir / clip_filename
        try:
            export_speaker_video_clip(
                payload["input_path"],
                clip_path,
                intervals,
                ffmpeg_bin,
                job_root,
                clip_crf,
                clip_preset,
            )
            clip_entry["clip_path"] = str(clip_path)
            clips_generated += 1
        except Exception as exc:
            clips_failed += 1
            clip_entry["error"] = str(exc)
            warnings.append(f"speaker_clip_failed[{speaker_id}]: {exc}")
            clips.append(clip_entry)
            continue

        voice_hash = None
        speaker_vector = None
        if result.get("voiceprint", {}).get("speaker_hashes"):
            voice_hash = result["voiceprint"]["speaker_hashes"].get(speaker_id)
        if result.get("voiceprint", {}).get("speaker_embeddings"):
            speaker_vector = result["voiceprint"]["speaker_embeddings"].get(speaker_id)
        if voice_hash:
            clip_entry["profile_id"] = voice_hash
            clip_entry["profile_link_status"] = "linked"
            if storage_mode in {"profile", "both"} and voiceprint_store is not None:
                # Auto-enroll with a real identity whenever the speaker is NAMED — by a
                # Zoom VTT, a Ktalk export, a manual speaker-map, or --enroll-name (a
                # single-speaker sample). pyannote's own labels are "Speaker N" == the
                # speaker_id, so has_real_name stays False for anonymous diarization and
                # only genuinely-named speakers are enrolled by name.
                has_real_name = bool(speaker_name) and str(speaker_name) != str(speaker_id)
                enroll_meta = None
                if has_real_name:
                    name_str = str(speaker_name).strip()
                    enroll_meta = {
                        "canonical_name": name_str,
                        "display_name": name_str.split()[0] if name_str else None,
                        "contact_ref": f"[[{name_str}]]" if name_str else None,
                        "contact_name": name_str,
                    }
                clip_extractor = (result.get("voiceprint", {}) or {}).get("extractor")
                profile = ensure_profile_entry(
                    voiceprint_store,
                    voice_hash,
                    vector=speaker_vector,
                    enroll_meta=enroll_meta,
                    sample_meta={
                        "source_file": source_file,
                        "speaker_id": speaker_id,
                        "speaker_source": speaker_source,
                        "mode": "auto_enroll_named" if has_real_name else "auto_clip",
                    },
                    extractor=clip_extractor,
                )
                update_state = maybe_update_profile_clip(
                    profile,
                    source_file=source_file,
                    clip_path=str(clip_path),
                    intervals=intervals,
                    selection_method=selection_method,
                )
                clip_entry["profile_clip_path"] = profile.get("best_clip_path")
                clip_entry["profile_link_status"] = "updated" if update_state["updated"] else "linked"
        clips.append(clip_entry)

    return {
        "status": "ok" if clips_generated else "skipped",
        "reason": None if clips_generated else "clips_not_generated",
        "target_sec": target_sec,
        "storage_mode": storage_mode,
        "clips_generated": clips_generated,
        "clips_failed": clips_failed,
        "clips": clips,
    }


def resolve_model_path(model_root: str | None, selected_model: str) -> str:
    if not model_root:
        return selected_model
    root = pathlib.Path(model_root)
    candidate = root / f"models--Systran--faster-whisper-{selected_model}" / "snapshots"
    if not candidate.exists():
        return selected_model
    snapshots = [item for item in candidate.iterdir() if item.is_dir()]
    if not snapshots:
        return selected_model
    snapshots.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return str(snapshots[0])


def _hf_pyannote_access_hint_from_exc(exc: BaseException) -> str:
    """Подсказка при ошибках доступа к gated-моделям pyannote на Hugging Face Hub."""
    text = f"{type(exc).__name__}: {exc}".lower()
    keys = (
        "gated",
        "401",
        "403",
        "cannot access",
        "repository not found",
        "invalid username or password",
        "bad credentials",
        "not logged in",
    )
    if not any(k in text for k in keys):
        return ""
    return (
        " | HF/pyannote: задайте HF_TOKEN/HUGGINGFACE_TOKEN (Read) и в браузере под тем же "
        "аккаунтом примите условия на https://huggingface.co/pyannote/segmentation-3.0 и "
        "https://huggingface.co/pyannote/speaker-diarization-3.1 — проверить токен: "
        "`python -c \"from huggingface_hub import whoami; print(whoami())\"`. "
        "Гайд: docs/hf-token.html"
    )


def normalize_speaker_map(payload: dict) -> dict[str, str]:
    raw = payload.get("speaker_map") or {}
    if not isinstance(raw, dict):
        return {}
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        if key is None or value is None:
            continue
        source = str(key).strip()
        target = str(value).strip()
        if source and target:
            mapping[source] = target
    return mapping


def build_diarization_pipeline(payload: dict):
    # PyTorch 2.6+ defaults torch.load(weights_only=True); pyannote 3.4 checkpoints
    # ship objects (TorchVersion etc.) that aren't on the safe-globals allowlist.
    # Pyannote models are gated/trusted HF artifacts → patch to legacy default.
    # Mirror of diarize_segments_only.py.
    import torch as _torch
    if not getattr(_torch.load, "_pyannote_compat", False):
        _orig_torch_load = _torch.load

        def _torch_load_compat(*a, **kw):
            kw["weights_only"] = False
            return _orig_torch_load(*a, **kw)

        _torch_load_compat._pyannote_compat = True  # type: ignore[attr-defined]
        _torch.load = _torch_load_compat
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency in runtime
        raise RuntimeError(f"pyannote.audio is not installed: {exc}") from exc
    diarization_model = payload.get("diarization_model") or "pyannote/speaker-diarization-3.1"
    hf_token = payload.get("hf_token")
    if not hf_token:
        hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        hf_token = hf_token or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    local_model = pathlib.Path(str(diarization_model))
    if local_model.exists():
        try:
            return Pipeline.from_pretrained(str(local_model))
        except Exception as exc:
            hint = _hf_pyannote_access_hint_from_exc(exc)
            raise RuntimeError(f"{exc}{hint}") from exc
    try:
        if hf_token:
            # pyannote.Pipeline.from_pretrained не всегда пробрасывает token/use_auth_token в hub;
            # при смешанных версиях pyannote/huggingface_hub оба kwargs дают TypeError.
            # Надёжно: залогиниться в hub и грузить модель без токена в вызове.
            try:
                from huggingface_hub import login as _hf_login

                _hf_login(token=hf_token, add_to_git_credential=False)
            except Exception:
                _saved = os.environ.get("HF_TOKEN")
                try:
                    os.environ["HF_TOKEN"] = hf_token
                    return Pipeline.from_pretrained(diarization_model)
                finally:
                    if _saved is None:
                        os.environ.pop("HF_TOKEN", None)
                    else:
                        os.environ["HF_TOKEN"] = _saved
        return Pipeline.from_pretrained(diarization_model)
    except Exception as exc:
        hint = _hf_pyannote_access_hint_from_exc(exc)
        raise RuntimeError(f"{exc}{hint}") from exc


def load_waveform_for_diarization(audio_path: str, ffmpeg_bin: str, job_root: pathlib.Path) -> dict:
    if torch is None:
        raise RuntimeError("torch is not installed")

    wav_path = stage_dir(job_root, "diarization") / "diarization.wav"
    subprocess.run(
        [ffmpeg_bin, "-y", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", str(wav_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with wave.open(str(wav_path), "rb") as reader:
        sample_rate = int(reader.getframerate())
        sample_width = int(reader.getsampwidth())
        if sample_width != 2:
            raise RuntimeError(f"unsupported sample width: {sample_width}")
        frames = reader.readframes(reader.getnframes())
    pcm16 = array.array("h")
    pcm16.frombytes(frames)
    waveform = torch.tensor(pcm16, dtype=torch.float32).unsqueeze(0) / 32768.0
    return {"waveform": waveform, "sample_rate": sample_rate}


def load_torchaudio_module():
    try:
        import torchaudio  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency in runtime
        raise RuntimeError(f"torchaudio import failed: {exc}") from exc
    return torchaudio


def run_diarization(audio_path: str, payload: dict, job_root: pathlib.Path) -> tuple[list[dict], dict]:
    kwargs = {}
    if payload.get("min_speakers") is not None:
        kwargs["min_speakers"] = int(payload["min_speakers"])
    if payload.get("max_speakers") is not None:
        kwargs["max_speakers"] = int(payload["max_speakers"])
    diarization_input = audio_path
    input_mode = "path"
    input_warning = None
    ffmpeg_bin = payload.get("ffmpeg_bin") or "ffmpeg"
    if torch is not None:
        try:
            diarization_input = load_waveform_for_diarization(audio_path, ffmpeg_bin, job_root)
            input_mode = "waveform_ffmpeg"
        except Exception as ffmpeg_exc:
            input_warning = f"diarization_waveform_ffmpeg_decode_failed: {ffmpeg_exc}"
            try:
                torchaudio = load_torchaudio_module()
                waveform, sample_rate = torchaudio.load(audio_path)
                if waveform.dim() == 1:
                    waveform = waveform.unsqueeze(0)
                if waveform.size(0) > 1:
                    waveform = waveform.mean(dim=0, keepdim=True)
                diarization_input = {"waveform": waveform, "sample_rate": int(sample_rate)}
                input_mode = "waveform_torchaudio"
                input_warning = None
            except Exception as ta_exc:
                input_warning = (
                    f"{input_warning}; diarization_waveform_torchaudio_decode_failed: {ta_exc}"
                )

    log(
        payload,
        f"phase=diarization input_mode={input_mode} "
        f"(waveform/path → встроенный файловый декодер pyannote/torchcodec не обязателен)",
    )
    # pyannote 3.x на Windows часто печатает гигантский UserWarning про torchcodec при импорте io,
    # хотя при подаче {"waveform", "sample_rate"} декод файла через torchcodec не используется.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="pyannote.audio.core.io")
        pipeline = build_diarization_pipeline(payload)
        diarization = pipeline(diarization_input, **kwargs)
    annotation = diarization
    if not hasattr(annotation, "itertracks") and hasattr(diarization, "speaker_diarization"):
        annotation = diarization.speaker_diarization
    if not hasattr(annotation, "itertracks"):
        raise RuntimeError(
            f"unsupported diarization output type: {type(diarization).__name__} (itertracks not found)"
        )
    turns: list[dict] = []
    stable_order: list[str] = []
    alias_map: dict[str, str] = {}

    for turn, _, label in annotation.itertracks(yield_label=True):
        raw_label = str(label).strip() if label is not None else "unknown"
        if raw_label not in alias_map:
            stable_order.append(raw_label)
            alias_map[raw_label] = f"Speaker {len(stable_order)}"
        turns.append(
            {
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "raw_label": raw_label,
                "speaker_id": alias_map[raw_label],
            }
        )

    return turns, {
        "enabled": True,
        "status": "ok",
        "model": payload.get("diarization_model"),
        "min_speakers": payload.get("min_speakers"),
        "max_speakers": payload.get("max_speakers"),
        "speakers_detected": len(stable_order),
        "raw_labels": stable_order,
        "input_mode": input_mode,
        "warning": input_warning,
    }


def overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers_to_segments(segments: list[dict], turns: list[dict], speaker_map: dict[str, str]) -> dict:
    if not turns:
        for segment in segments:
            segment["speaker_id"] = None
            segment["speaker_name"] = None
            segment["speaker"] = None
            segment["speaker_source"] = "unknown"
            segment["voice_hash"] = None
        return {
            "assigned_segments": 0,
            "unassigned_segments": len(segments),
            "speaker_turns": 0,
            "method": "overlap-max",
        }

    speaker_turn_labels: dict[str, tuple[str | None, str]] = {}
    for turn in turns:
        speaker_id = str(turn.get("speaker_id") or "").strip()
        if not speaker_id or speaker_id in speaker_turn_labels:
            continue
        label = str(turn.get("speaker_name") or turn.get("raw_label") or "").strip() or None
        source = str(turn.get("speaker_source") or "unknown").strip() or "unknown"
        speaker_turn_labels[speaker_id] = (label, source)

    assigned = 0
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        coverage_by_speaker: dict[str, float] = defaultdict(float)
        for turn in turns:
            overlap = overlap_seconds(start, end, float(turn["start"]), float(turn["end"]))
            if overlap > 0:
                coverage_by_speaker[turn["speaker_id"]] += overlap

        if coverage_by_speaker:
            speaker_id = max(coverage_by_speaker.items(), key=lambda item: item[1])[0]
            inferred_label, inferred_source = speaker_turn_labels.get(speaker_id, (None, "unknown"))
            speaker_name = speaker_map.get(speaker_id) or inferred_label or speaker_id
            segment["speaker_id"] = speaker_id
            segment["speaker_name"] = speaker_name
            segment["speaker"] = speaker_name
            segment["speaker_source"] = "manual_map" if speaker_id in speaker_map else inferred_source
            segment["voice_hash"] = None
            assigned += 1
        else:
            segment["speaker_id"] = None
            segment["speaker_name"] = None
            segment["speaker"] = None
            segment["speaker_source"] = "unknown"
            segment["voice_hash"] = None

    return {
        "assigned_segments": assigned,
        "unassigned_segments": len(segments) - assigned,
        "speaker_turns": len(turns),
        "method": "overlap-max",
    }


def resolve_speaker_turns(audio_path: str, payload: dict, job_root: pathlib.Path) -> tuple[list[dict], dict, dict]:
    zoom_meta = {
        "enabled": bool(str(payload.get("zoom_vtt_path") or "").strip()),
        "status": "disabled" if not str(payload.get("zoom_vtt_path") or "").strip() else "pending",
        "reason": "zoom_vtt_not_provided" if not str(payload.get("zoom_vtt_path") or "").strip() else None,
        "path": None,
        "cues_parsed": 0,
        "turns_built": 0,
        "speakers_detected": 0,
        "speaker_observations": {},
    }
    zoom_vtt_path = str(payload.get("zoom_vtt_path") or "").strip()
    if zoom_vtt_path:
        try:
            turns, parsed_meta = parse_zoom_vtt_turns(zoom_vtt_path)
            zoom_meta.update(parsed_meta)
            diarization_meta = {
                "enabled": True,
                "status": "ok",
                "reason": "speaker_turns_loaded_from_zoom_vtt",
                "model": None,
                "source": "zoom_vtt",
                "speaker_turns": len(turns),
                "speakers_detected": parsed_meta.get("speakers_detected", 0),
            }
            return turns, diarization_meta, zoom_meta
        except Exception as exc:
            zoom_meta["status"] = "fallback"
            zoom_meta["reason"] = "zoom_vtt_parse_failed"
            zoom_meta["error"] = str(exc)
            log(payload, f"phase=zoom_vtt fallback error={exc}")

    # Ktalk export: named speakers already, so diarization would only re-derive
    # anonymous labels for voices we can name. Parse failure falls through to
    # diarization rather than losing speakers entirely.
    ktalk_txt_path = str(payload.get("ktalk_txt_path") or "").strip()
    if ktalk_txt_path:
        try:
            turns, parsed_meta = parse_ktalk_txt_turns(ktalk_txt_path)
            diarization_meta = {
                "enabled": True,
                "status": "ok",
                "reason": "speaker_turns_loaded_from_ktalk_txt",
                "model": None,
                "source": "ktalk_txt",
                "speaker_turns": len(turns),
                "speakers_detected": parsed_meta.get("speakers_detected", 0),
                "external": parsed_meta,
            }
            log(
                payload,
                f"phase=ktalk_txt ok utterances={parsed_meta.get('cues_parsed')} "
                f"turns={len(turns)} speakers={parsed_meta.get('speakers_detected')} "
                f"(diarization skipped)",
            )
            return turns, diarization_meta, zoom_meta
        except Exception as exc:
            log(payload, f"phase=ktalk_txt fallback error={exc}")

    turns, diarization_meta = run_diarization(audio_path, payload, job_root)
    diarization_meta["source"] = "diarization"
    return turns, diarization_meta, zoom_meta


def transcribe_chunk_worker(job: dict, model: WhisperModel | None = None) -> dict:
    reuse = model is not None
    stderr_log_line(
        f"chunk worker start: index={job['chunk_index']} device={job['runtime_device']} "
        f"compute={job['runtime_compute_type']} reuse_model={reuse}",
    )
    own_model = model is None
    if own_model:
        stderr_log_line(f"chunk model init begin: index={job['chunk_index']} path={job['model_path']}")
        model = create_whisper_model(job)
        stderr_log_line(f"chunk model init done: index={job['chunk_index']}")
    else:
        stderr_log_line(f"chunk model reuse skip init: index={job['chunk_index']}")
    stderr_log_line(f"chunk transcribe begin: index={job['chunk_index']} audio={job['audio_path']}")
    segments, info = model.transcribe(job["audio_path"], **job["kwargs"])
    stderr_log_line(f"chunk transcribe done: index={job['chunk_index']}")
    collected = []
    stderr_log_line(f"chunk segment iteration begin: index={job['chunk_index']}")
    stderr_log_line(f"chunk segment iterator create: index={job['chunk_index']}")
    segments_iter = iter(segments)
    stderr_log_line(f"chunk segment first next begin: index={job['chunk_index']}")
    first_item = None
    try:
        first_item = next(segments_iter)
        stderr_log_line(
            f"chunk segment first next ok: index={job['chunk_index']} "
            f"start={float(first_item.start):.3f} end={float(first_item.end):.3f}",
        )
    except StopIteration:
        stderr_log_line(f"chunk segment first next empty: index={job['chunk_index']}")

    def segment_chain():
        if first_item is not None:
            yield first_item
        yield from segments_iter

    stderr_log_line(
        f"chunk segment pull loop begin: index={job['chunk_index']} "
        f"(lazy; раньше list(iterator) давал долгую тишину в логе)",
    )
    t_pull0 = time.monotonic()
    for idx, item in enumerate(segment_chain()):
        if idx > 0 and (idx + 1) % SEGMENT_ITER_PROGRESS_EVERY == 0:
            elapsed = time.monotonic() - t_pull0
            stderr_log_line(
                f"chunk segment pull progress: index={job['chunk_index']} "
                f"pulled={idx + 1} wall_sec={elapsed:.1f} last_end={float(item.end):.1f}s",
            )
        text = (item.text or "").strip()
        if not text:
            continue
        start = round(float(item.start), 3)
        end = round(float(item.end), 3)
        if job["chunk_index"] > 0:
            overlap = float(job["chunk_overlap_sec"])
            if end <= overlap:
                continue
            if start < overlap:
                start = overlap
        collected.append(
            {
                "start": round(start + float(job["chunk_start"]), 3),
                "end": round(end + float(job["chunk_start"]), 3),
                "text": text,
                "speaker": None,
                "chunk_index": job["chunk_index"],
            }
        )
    stderr_log_line(f"chunk segment iteration done: index={job['chunk_index']} kept={len(collected)}")
    return {
        "chunk_index": job["chunk_index"],
        "language": getattr(info, "language", None),
        "segments": collected,
    }


def write_text_atomic(target: pathlib.Path, text: str, encoding: str = "utf-8") -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, target)


def write_chunk_asr_artifacts_to_output(payload: dict, job: dict, chunk_result: dict) -> None:
    """Сразу после ASR чанка: копия WAV + JSON сегментов в output_dir (без ожидания финального write_outputs)."""
    if not payload.get("copy_asr_chunk_artifacts", True):
        return
    output_dir = pathlib.Path(payload["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    source_path = pathlib.Path(payload["input_path"])
    base_name = str(payload.get("output_base_name") or source_path.stem)
    idx = int(job["chunk_index"])
    wav_dst = output_dir / f"{base_name}-asr-chunk-{idx:03d}.wav"
    json_dst = output_dir / f"{base_name}-asr-chunk-{idx:03d}.json"
    try:
        shutil.copy2(job["audio_path"], wav_dst)
    except Exception as exc:
        log(payload, f"asr_chunk_artifacts copy wav failed chunk={idx}: {exc}")
    chunk_doc = {
        "chunk_index": idx,
        "chunk_start_sec": float(job["chunk_start"]),
        "chunk_overlap_sec": float(job.get("chunk_overlap_sec") or 0),
        "language": chunk_result.get("language"),
        "segment_count": len(chunk_result.get("segments") or []),
        "segments": chunk_result.get("segments") or [],
        "asr_fingerprint": payload.get("_asr_fingerprint_cache"),
    }
    try:
        write_text_atomic(json_dst, json.dumps(chunk_doc, ensure_ascii=False, indent=2))
        log(
            payload,
            f"asr_chunk_artifacts ok chunk={idx} wav={wav_dst.name} json={json_dst.name}",
        )
    except Exception as exc:
        log(payload, f"asr_chunk_artifacts write json failed chunk={idx}: {exc}")


def _windows_process_exists(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    err = kernel32.GetLastError()
    if err == _WIN_ERROR_ACCESS_DENIED:
        return True
    return False


def process_pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_output_dir_lock(lock_path: pathlib.Path) -> dict | None:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "pid" in data:
            return data
    except Exception:
        return None
    return None


def acquire_output_dir_lock(output_dir: pathlib.Path, execution_mode: str, warnings: list[str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / OUTPUT_DIR_LOCK_FILENAME
    our_pid = os.getpid()
    if lock_path.exists():
        data = _read_output_dir_lock(lock_path)
        if data is not None:
            old_pid = int(data.get("pid", 0))
            if process_pid_exists(old_pid):
                raise RuntimeError(
                    "output_dir_already_locked: "
                    f"pid={old_pid} lock={lock_path} "
                    f"started_at={data.get('started_at')} mode={data.get('mode')}"
                )
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    lock_body = {
        "pid": our_pid,
        "started_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": execution_mode,
    }
    tmp = lock_path.with_name(lock_path.name + ".tmp")
    tmp.write_text(json.dumps(lock_body, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, lock_path)


def release_output_dir_lock(output_dir: pathlib.Path) -> None:
    lock_path = output_dir / OUTPUT_DIR_LOCK_FILENAME
    if not lock_path.exists():
        return
    data = _read_output_dir_lock(lock_path)
    our_pid = os.getpid()
    if data is not None and int(data.get("pid", -1)) == our_pid:
        try:
            lock_path.unlink()
        except OSError:
            pass


def media_path_key(p: str | pathlib.Path | None) -> str | None:
    if p is None:
        return None
    s = str(p).strip()
    if not s:
        return None
    try:
        return os.path.normcase(str(pathlib.Path(s).resolve(strict=False)))
    except OSError:
        return os.path.normcase(str(pathlib.Path(s)))


def paths_match_media(left: str | pathlib.Path | None, right: str | pathlib.Path | None) -> bool:
    a = media_path_key(left)
    b = media_path_key(right)
    return bool(a and b and a == b)


def expected_chunk_count_for_audio(audio_path: str, payload: dict, warnings: list[str]) -> int:
    """Число чанков без создания файлов (должно совпадать с split_audio_into_chunks)."""
    chunk_minutes = int(payload.get("chunk_minutes", 20) or 20)
    chunk_overlap_sec = int(payload.get("chunk_overlap_sec", 30) or 30)
    ffmpeg_bin = payload.get("ffmpeg_bin") or "ffmpeg"
    ffprobe_bin = payload.get("ffprobe_bin") or "ffprobe"
    ffmpeg_available = pathlib.Path(ffmpeg_bin).exists() or bool(shutil.which(ffmpeg_bin))
    if not ffmpeg_available:
        return 1
    duration = probe_duration_seconds(audio_path, ffprobe_bin)
    if duration is None:
        warnings.append("expected_chunk_count_duration_unknown_assume_1")
        return 1
    chunk_size = float(chunk_minutes * 60)
    overlap = float(chunk_overlap_sec)
    if duration <= chunk_size:
        return 1
    step = chunk_size - overlap
    if step <= 0:
        return 1
    count = 0
    cursor = 0.0
    while cursor < duration:
        count += 1
        cursor += step
    return count


def build_asr_recovery_fingerprint(payload: dict, audio_path: str) -> dict:
    orig = payload.get("original_input_path") or payload["input_path"]
    src = pathlib.Path(orig).expanduser()
    proc = pathlib.Path(payload["input_path"]).expanduser()
    try:
        src_r = str(src.resolve(strict=False))
    except OSError:
        src_r = str(src)
    try:
        proc_r = str(proc.resolve(strict=False))
    except OSError:
        proc_r = str(proc)
    size = None
    try:
        if src.is_file():
            size = src.stat().st_size
    except OSError:
        pass
    duration = probe_duration_seconds(audio_path, payload.get("ffprobe_bin") or "ffprobe")
    return {
        "source_file_resolved": src_r,
        "processing_input_resolved": proc_r,
        "source_size_bytes": size,
        "duration_sec": duration,
        "chunk_minutes": int(payload.get("chunk_minutes", 20) or 20),
        "chunk_overlap_sec": int(payload.get("chunk_overlap_sec", 30) or 30),
        "selected_model": payload.get("selected_model"),
        "quality_preset": payload.get("quality_preset"),
    }


def _fp_scalar_match(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) < 0.05
    return a == b


def fingerprint_matches(stored: dict | None, current: dict) -> bool:
    if not stored or not isinstance(stored, dict):
        return False
    keys = (
        "source_file_resolved",
        "processing_input_resolved",
        "source_size_bytes",
        "duration_sec",
        "chunk_minutes",
        "chunk_overlap_sec",
        "selected_model",
        "quality_preset",
    )
    for k in keys:
        if not _fp_scalar_match(stored.get(k), current.get(k)):
            return False
    return True


def try_recover_from_asr_merged(
    output_dir: pathlib.Path,
    base_name: str,
    current_fp: dict,
    payload: dict,
    warnings: list[str],
) -> tuple[list[dict] | None, str | None]:
    path = output_dir / f"{base_name}-asr-merged.json"
    if not path.is_file():
        return None, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"asr_merged_read_failed:{exc}")
        return None, None
    if doc.get("schema") != ASR_MERGED_SCHEMA:
        warnings.append("asr_merged_unknown_schema")
        return None, None
    fp = doc.get("fingerprint")
    if not fingerprint_matches(fp, current_fp):
        warnings.append("asr_recover_skipped_source_mismatch")
        return None, None
    segs = doc.get("segments")
    if not isinstance(segs, list) or not segs:
        return None, None
    return segs, doc.get("language_detected")


def try_recover_from_asr_chunks(
    output_dir: pathlib.Path,
    base_name: str,
    expected_n: int,
    current_fp: dict,
    payload: dict,
    warnings: list[str],
) -> tuple[list[dict] | None, str | None]:
    if expected_n < 1:
        return None, None
    by_idx: dict[int, pathlib.Path] = {}
    for p in output_dir.glob(f"{base_name}-asr-chunk-*.json"):
        stem = p.stem
        marker = "-asr-chunk-"
        if marker not in stem:
            continue
        tail = stem.split(marker, 1)[1]
        try:
            idx = int(tail)
        except ValueError:
            continue
        by_idx[idx] = p
    if len(by_idx) != expected_n:
        return None, None
    for i in range(expected_n):
        if i not in by_idx:
            return None, None
    lang = None
    for i in range(expected_n):
        try:
            doc = json.loads(by_idx[i].read_text(encoding="utf-8"))
        except Exception:
            return None, None
        if i == 0:
            cfp = doc.get("asr_fingerprint")
            if cfp is not None and not fingerprint_matches(cfp, current_fp):
                warnings.append("asr_recover_skipped_chunk_fingerprint_mismatch")
                return None, None
        if lang is None and doc.get("language"):
            lang = str(doc["language"])
        if not isinstance(doc.get("segments"), list):
            return None, None
    merged: list[dict] = []
    for i in range(expected_n):
        doc = json.loads(by_idx[i].read_text(encoding="utf-8"))
        merged.extend(doc.get("segments") or [])
    return merged, lang


def write_asr_merged_checkpoint(
    payload: dict,
    segments: list[dict],
    language_detected: str | None,
    audio_path: str,
    warnings: list[str],
) -> None:
    if not segments:
        return
    output_dir = pathlib.Path(payload["output_dir"])
    source_path = pathlib.Path(payload["input_path"])
    base_name = str(payload.get("output_base_name") or source_path.stem)
    fp = build_asr_recovery_fingerprint(payload, audio_path)
    body = {
        "schema": ASR_MERGED_SCHEMA,
        "fingerprint": fp,
        "language_detected": language_detected,
        "segments": segments,
    }
    path = output_dir / f"{base_name}-asr-merged.json"
    try:
        write_text_atomic(path, json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(payload, f"phase=asr_merged_checkpoint written path={path.name} segments={len(segments)}")
    except Exception as exc:
        warnings.append(f"asr_merged_checkpoint_write_failed:{exc}")


def load_segments_from_jsonl(path: pathlib.Path) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def load_segments_from_raw_doc(raw: dict, output_dir: pathlib.Path) -> list[dict]:
    ref = raw.get("segments_ref")
    if isinstance(ref, dict) and str(ref.get("format") or "").lower() == "jsonl":
        rel = ref.get("file")
        if not rel:
            raise RuntimeError("segments_ref_missing_file")
        jpath = output_dir / str(rel)
        if not jpath.is_file():
            raise RuntimeError(f"segments_jsonl_not_found:{jpath}")
        return load_segments_from_jsonl(jpath)
    segs = raw.get("segments")
    if isinstance(segs, list) and segs:
        return segs
    raise RuntimeError("raw_has_no_segments_or_segments_ref")


def derive_asr_variant_id(payload: dict, result: dict) -> str:
    explicit = str(payload.get("asr_variant_id") or "").strip()
    if explicit:
        return sanitize_token(explicit)
    existing = result.get("asr_variant") or {}
    existing_id = str(existing.get("variant_id") or "").strip()
    if existing_id:
        return sanitize_token(existing_id)
    created_at = str(result.get("created_at") or dt.datetime.now(dt.UTC).isoformat())
    created_token = re.sub(r"[^0-9T]", "", created_at.replace(":", "").replace("-", ""))
    return sanitize_token(
        f"{result.get('model') or payload.get('selected_model')}-"
        f"{result.get('quality_preset') or payload.get('quality_preset')}-"
        f"{result.get('execution_profile') or payload.get('execution_profile')}-"
        f"{created_token}"
    )


def build_session_transcript_markdown(result: dict) -> str:
    asr_variant = result.get("asr_variant") or {}
    lines = [
        "---",
        f"source_file: {json.dumps(result.get('source_file'))}",
        f"session_artifact_dir: {json.dumps(result.get('session_artifact_dir'))}",
        f"source_type: {json.dumps(result.get('source_type'))}",
        f"language: {json.dumps(result.get('language_detected'))}",
        f"engine: {json.dumps(result.get('engine'))}",
        f"model: {json.dumps(result.get('model'))}",
        f"quality_preset: {json.dumps(result.get('quality_preset'))}",
        f"execution_profile: {json.dumps(result.get('execution_profile'))}",
        f"asr_variant_id: {json.dumps(asr_variant.get('variant_id'))}",
        f"speaker_turns_source: {json.dumps((result.get('speaker_turns') or {}).get('source'))}",
        f"created_at: {json.dumps(result.get('created_at'))}",
        "---",
        "",
        "## Session Transcript",
        "",
    ]
    for block in build_display_blocks(result.get("segments") or []):
        speaker_label = block["speaker_label"]
        source_suffix = f" [{block['speaker_source']}]" if speaker_label else ""
        speaker_prefix = f"{speaker_label}{source_suffix}: " if speaker_label else ""
        block_text = " ".join(block["texts"]).strip()
        lines.append(f"[{timestamp_hms(block['start'])}] {speaker_prefix}{block_text}".strip())
    return "\n".join(lines).rstrip() + "\n"


def write_asr_transcript_checkpoint(payload: dict, segments: list, language_detected) -> None:
    """Write a usable ``{base}-transcript.md`` from ASR segments BEFORE diarization.

    Guarantees a ready-to-use transcript always exists even if diarization is slow,
    hangs, or the process is interrupted. It writes to the SAME path the final
    ``write_outputs`` uses, so once diarization completes the speaker-labelled
    version overwrites it. Non-fatal and gated by ``asr_transcript_checkpoint``.
    """
    if not payload.get("asr_transcript_checkpoint", True):
        return
    try:
        output_dir = pathlib.Path(payload["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        base_name = str(payload.get("output_base_name") or pathlib.Path(payload["input_path"]).stem)
        md_path = output_dir / f"{base_name}-transcript.md"
        header = (
            "---\n"
            f"source_file: {json.dumps(str(payload.get('input_path')))}\n"
            f"language: {json.dumps(language_detected)}\n"
            "stage: asr-checkpoint\n"
            "note: transcript from ASR before diarization; speaker labels added later\n"
            "---\n\n## Transcript\n\n"
        )
        tmp = md_path.with_name(md_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(header)
            for block in build_display_blocks(segments):
                block_text = " ".join(block["texts"]).strip()
                f.write(f"[{timestamp_hms(block['start'])}] {block_text}".strip() + "\n")
        os.replace(tmp, md_path)
        log(payload, f"asr_transcript_checkpoint written: {md_path.name} ({len(segments)} segments)")
    except Exception as exc:
        log(payload, f"asr_transcript_checkpoint failed (non-fatal): {exc}")


def write_outputs(payload: dict, result: dict) -> dict:
    output_dir = pathlib.Path(payload["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    session_artifact_dir = resolve_session_artifact_dir(payload)
    session_artifact_dir.mkdir(parents=True, exist_ok=True)
    source_path = pathlib.Path(payload["input_path"])
    base_name = str(payload.get("output_base_name") or source_path.stem)
    segments = result.get("segments") or []
    seg_count = len(segments)
    asr_variant_id = derive_asr_variant_id(payload, result)
    asr_variant = {
        "variant_id": asr_variant_id,
        "model": result.get("model"),
        "quality_preset": result.get("quality_preset"),
        "execution_profile": result.get("execution_profile"),
        "engine": result.get("engine"),
        "created_at": result.get("created_at"),
    }
    result["asr_variant"] = asr_variant
    result["session_artifact_dir"] = str(session_artifact_dir)

    raw_path = output_dir / f"{base_name}-raw.json"
    txt_path = output_dir / f"{base_name}-transcript.txt"
    md_path = output_dir / f"{base_name}-transcript.md"
    vtt_path = output_dir / f"{base_name}-segments.vtt"
    run_meta_path = output_dir / f"{base_name}-run-meta.json"
    jsonl_path = output_dir / f"{base_name}-segments.jsonl"

    formats = payload.get("output_formats") or ["md", "json", "vtt"]
    if not isinstance(formats, (list, tuple)):
        formats = ["md", "json", "vtt"]
    want = {str(x).lower() for x in formats}
    if payload.get("write_plain_transcript_txt"):
        want.add("txt")

    split_seg = bool(payload.get("split_raw_segments", True))
    compact = bool(payload.get("compact_output_json", True))

    log(
        payload,
        f"write_outputs: begin base_name={base_name} segments={seg_count} "
        f"formats={sorted(want)} split_raw_segments={split_seg}",
    )

    frontmatter = "\n".join(
        [
            "---",
            f"source_file: {json.dumps(result['source_file'])}",
            f"processing_input_path: {json.dumps(result.get('processing_input_path'))}",
            f"source_type: {json.dumps(result['source_type'])}",
            f"language: {json.dumps(result['language_detected'])}",
            f"engine: {json.dumps(result['engine'])}",
            f"model: {json.dumps(result['model'])}",
            f"quality_preset: {json.dumps(result['quality_preset'])}",
            f"execution_profile: {json.dumps(result['execution_profile'])}",
            f"duration: {json.dumps(result['duration_sec'])}",
            f"created_at: {json.dumps(result['created_at'])}",
            "---",
            "",
            "## Transcript",
            "",
        ]
    )

    if "md" in want:
        md_tmp = md_path.with_name(md_path.name + ".tmp")
        try:
            with md_tmp.open("w", encoding="utf-8") as f_md:
                f_md.write(frontmatter)
                for block in build_display_blocks(segments):
                    speaker_label = block["speaker_label"]
                    source_suffix = f" [{block['speaker_source']}]" if speaker_label else ""
                    speaker_prefix = f"{speaker_label}{source_suffix}: " if speaker_label else ""
                    block_text = " ".join(block["texts"]).strip()
                    f_md.write(f"[{timestamp_hms(block['start'])}] {speaker_prefix}{block_text}".strip() + "\n")
            os.replace(md_tmp, md_path)
        except Exception:
            md_tmp.unlink(missing_ok=True)
            raise

    if "vtt" in want:
        vtt_tmp = vtt_path.with_name(vtt_path.name + ".tmp")
        try:
            with vtt_tmp.open("w", encoding="utf-8") as f_vtt:
                f_vtt.write("WEBVTT\n\n")
                for block in build_display_blocks(segments):
                    speaker_label = block["speaker_label"]
                    source_suffix = f" [{block['speaker_source']}]" if speaker_label else ""
                    speaker_prefix = f"{speaker_label}{source_suffix}: " if speaker_label else ""
                    block_text = " ".join(block["texts"]).strip()
                    f_vtt.write(f"{timestamp_vtt(block['start'])} --> {timestamp_vtt(block['end'])}\n")
                    f_vtt.write(f"{speaker_prefix}{block_text}".strip() + "\n\n")
            os.replace(vtt_tmp, vtt_path)
        except Exception:
            vtt_tmp.unlink(missing_ok=True)
            raise

    txt_written = False
    if "txt" in want:
        tmp_t = txt_path.with_name(txt_path.name + ".tmp")
        try:
            with tmp_t.open("w", encoding="utf-8") as f_txt:
                first = True
                for segment in segments:
                    if not first:
                        f_txt.write("\n")
                    first = False
                    f_txt.write(str(segment.get("text") or ""))
            os.replace(tmp_t, txt_path)
            txt_written = True
        except Exception:
            tmp_t.unlink(missing_ok=True)
            raise

    raw_body: dict | None = None
    if split_seg:
        jl_tmp = jsonl_path.with_name(jsonl_path.name + ".tmp")
        try:
            with jl_tmp.open("w", encoding="utf-8") as jf:
                for seg in segments:
                    jf.write(json.dumps(seg, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(jl_tmp, jsonl_path)
        except Exception:
            jl_tmp.unlink(missing_ok=True)
            raise
        raw_body = {k: v for k, v in result.items() if k != "segments"}
        raw_body["segments"] = []
        raw_body["segments_ref"] = {"file": jsonl_path.name, "format": "jsonl", "count": seg_count}
        if compact:
            raw_json_text = json.dumps(raw_body, ensure_ascii=False, separators=(",", ":"))
        else:
            raw_json_text = json.dumps(raw_body, ensure_ascii=False, indent=2)
    else:
        if compact:
            raw_json_text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        else:
            raw_json_text = json.dumps(result, ensure_ascii=False, indent=2)

    raw_bytes = len(raw_json_text.encode("utf-8"))
    log(payload, f"write_outputs: raw.json utf8_bytes={raw_bytes} path={raw_path}")
    write_text_atomic(raw_path, raw_json_text, encoding="utf-8")
    log(payload, f"write_outputs: raw.json committed ok path={raw_path}")

    run_meta = {
        "source_file": result["source_file"],
        "processing_input_path": result.get("processing_input_path"),
        "work_root": result.get("work_root"),
        "job_work_dir": result.get("job_work_dir"),
        "engine": result["engine"],
        "model": result["model"],
        "quality_preset": result["quality_preset"],
        "execution_profile": result["execution_profile"],
        "asr_variant": asr_variant,
        "language_detected": result["language_detected"],
        "timestamps": payload.get("timestamps") or "hms",
        "speaker_labels": payload.get("speaker_labels", payload.get("speaker_mode", "off") != "off"),
        "warnings": result["warnings"],
        "diarization": result.get("diarization", {}),
        "speaker_turns": result.get("speaker_turns", {}),
        "alignment": result.get("alignment", {}),
        "speaker_map": result.get("speaker_map", {}),
        "voiceprint": result.get("voiceprint", {}),
        "zoom_vtt": result.get("zoom_vtt", {}),
        "profile_updates": result.get("profile_updates", []),
        "profile_sync": result.get("profile_sync", {}),
        "project_registry": result.get("project_registry", {}),
        "machine_local_store": result.get("machine_local_store", {}),
        "speaker_clips": result.get("speaker_clips", []),
        "speaker_review": result.get("speaker_review", {}),
        "clip_generation": result.get("clip_generation", {}),
        "environment": payload.get("environment", {}),
        "split_raw_segments": split_seg,
        "segments_ref": (raw_body.get("segments_ref") if raw_body else None),
        "identification": payload.get("identification", {}),
    }
    log(payload, f"write_outputs: writing run-meta.json path={run_meta_path}")
    if compact:
        run_meta_text = json.dumps(run_meta, ensure_ascii=False, separators=(",", ":"))
    else:
        run_meta_text = json.dumps(run_meta, ensure_ascii=False, indent=2)
    write_text_atomic(run_meta_path, run_meta_text, encoding="utf-8")
    log(payload, "write_outputs: done")

    structured_outputs: dict[str, str | None] = {}
    asr_dir = session_artifact_dir / "asr"
    source_dir = session_artifact_dir / "source"
    improvement_dir = session_artifact_dir / "improvement"
    asr_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    improvement_dir.mkdir(parents=True, exist_ok=True)

    structured_run_meta_path = asr_dir / f"{asr_variant_id}-run-meta.json"
    write_text_atomic(structured_run_meta_path, run_meta_text, encoding="utf-8")
    structured_outputs["asr_run_meta"] = str(structured_run_meta_path)

    structured_raw_body = dict(raw_body) if isinstance(raw_body, dict) else None
    if split_seg:
        structured_jsonl_path = asr_dir / f"{asr_variant_id}-segments.jsonl"
        shutil.copy2(jsonl_path, structured_jsonl_path)
        structured_outputs["asr_segments_jsonl"] = str(structured_jsonl_path)
        if structured_raw_body is not None:
            structured_raw_body["segments_ref"] = {
                "file": structured_jsonl_path.name,
                "format": "jsonl",
                "count": seg_count,
            }
    else:
        structured_outputs["asr_segments_jsonl"] = None

    structured_raw_path = asr_dir / f"{asr_variant_id}-raw.json"
    if structured_raw_body is not None:
        structured_raw_json_text = (
            json.dumps(structured_raw_body, ensure_ascii=False, separators=(",", ":"))
            if compact
            else json.dumps(structured_raw_body, ensure_ascii=False, indent=2)
        )
    else:
        structured_raw_json_text = raw_json_text
    write_text_atomic(structured_raw_path, structured_raw_json_text, encoding="utf-8")
    structured_outputs["asr_raw"] = str(structured_raw_path)

    if "md" in want and md_path.exists():
        structured_md_path = asr_dir / f"{asr_variant_id}-transcript.md"
        shutil.copy2(md_path, structured_md_path)
        structured_outputs["asr_transcript_md"] = str(structured_md_path)
    else:
        structured_outputs["asr_transcript_md"] = None

    if "vtt" in want and vtt_path.exists():
        structured_vtt_path = asr_dir / f"{asr_variant_id}-segments.vtt"
        shutil.copy2(vtt_path, structured_vtt_path)
        structured_outputs["asr_segments_vtt"] = str(structured_vtt_path)
    else:
        structured_outputs["asr_segments_vtt"] = None

    if txt_written and txt_path.exists():
        structured_txt_path = asr_dir / f"{asr_variant_id}-transcript.txt"
        shutil.copy2(txt_path, structured_txt_path)
        structured_outputs["asr_transcript_txt"] = str(structured_txt_path)
    else:
        structured_outputs["asr_transcript_txt"] = None

    zoom_vtt_path = str(payload.get("zoom_vtt_path") or "").strip()
    if zoom_vtt_path:
        copied_source_vtt = source_dir / "source.vtt"
        shutil.copy2(pathlib.Path(zoom_vtt_path), copied_source_vtt)
        structured_outputs["source_vtt"] = str(copied_source_vtt)
    else:
        structured_outputs["source_vtt"] = None

    session_transcript_path = session_artifact_dir / "session-transcript.md"
    write_text_atomic(session_transcript_path, build_session_transcript_markdown(result), encoding="utf-8")
    structured_outputs["session_transcript_md"] = str(session_transcript_path)

    improvement_eval = {
        "schema_version": "session-improvement-v1",
        "source_file": result.get("source_file"),
        "session_artifact_dir": str(session_artifact_dir),
        "created_at": result.get("created_at"),
        "execution_mode": result.get("execution_mode"),
        "asr_variant": asr_variant,
        "speaker_turns": result.get("speaker_turns", {}),
        "diarization": result.get("diarization", {}),
        "alignment": result.get("alignment", {}),
        "voiceprint": result.get("voiceprint", {}),
        "zoom_vtt": result.get("zoom_vtt", {}),
        "profile_updates": result.get("profile_updates", []),
        "profile_sync": result.get("profile_sync", {}),
        "project_registry": result.get("project_registry", {}),
        "machine_local_store": result.get("machine_local_store", {}),
    }
    session_improvement_path = improvement_dir / "session-improvement.json"
    write_text_atomic(
        session_improvement_path,
        json.dumps(improvement_eval, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    structured_outputs["session_improvement_json"] = str(session_improvement_path)

    return {
        "json": str(raw_path),
        "txt": str(txt_path) if txt_written else None,
        "md": str(md_path) if "md" in want else None,
        "vtt": str(vtt_path) if "vtt" in want else None,
        "segments_jsonl": str(jsonl_path) if split_seg else None,
        "run_meta": str(run_meta_path),
        "asr_variant_id": asr_variant_id,
        "session_artifact_dir": str(session_artifact_dir),
        "structured": structured_outputs,
    }


def resolve_execution_mode(payload: dict) -> str:
    mode = str(payload.get("execution_mode") or "full").strip().lower()
    if mode not in {"asr_only", "speaker_pass", "full", "merge_asr_chunks"}:
        return "full"
    return mode


def artifact_paths_for_payload(payload: dict) -> dict:
    output_dir = pathlib.Path(payload["output_dir"])
    session_artifact_dir = resolve_session_artifact_dir(payload)
    source_path = pathlib.Path(payload["input_path"])
    base_name = str(payload.get("output_base_name") or source_path.stem)
    asr_variant_id = sanitize_token(str(payload.get("asr_variant_id") or "").strip()) if str(payload.get("asr_variant_id") or "").strip() else None
    return {
        "raw": output_dir / f"{base_name}-raw.json",
        "txt": output_dir / f"{base_name}-transcript.txt",
        "md": output_dir / f"{base_name}-transcript.md",
        "vtt": output_dir / f"{base_name}-segments.vtt",
        "segments_jsonl": output_dir / f"{base_name}-segments.jsonl",
        "run_meta": output_dir / f"{base_name}-run-meta.json",
        "structured_raw": (session_artifact_dir / "asr" / f"{asr_variant_id}-raw.json") if asr_variant_id else None,
    }


def resolve_existing_raw_path(payload: dict) -> tuple[pathlib.Path, str]:
    explicit = payload.get("asr_raw_json_path")
    if explicit is not None and str(explicit).strip():
        p = pathlib.Path(str(explicit).strip()).expanduser().resolve()
        if p.is_file():
            return p, "asr_raw_json_path"
        raise RuntimeError(f"asr_raw_json_path_not_found: {p}")

    paths = artifact_paths_for_payload(payload)
    canonical = paths["raw"]
    output_dir = pathlib.Path(payload["output_dir"])
    structured_raw = paths.get("structured_raw")
    if isinstance(structured_raw, pathlib.Path) and structured_raw.is_file():
        try:
            raw_preview = json.loads(structured_raw.read_text(encoding="utf-8"))
            segs_ok = bool(isinstance(raw_preview.get("segments"), list) and raw_preview["segments"])
            ref = raw_preview.get("segments_ref")
            structured_output_dir = structured_raw.parent
            ref_ok = (
                isinstance(ref, dict)
                and str(ref.get("format") or "").lower() == "jsonl"
                and ref.get("file")
                and (structured_output_dir / str(ref["file"])).is_file()
            )
            if segs_ok or ref_ok:
                return structured_raw, "structured_asr_variant"
        except Exception:
            pass
    if canonical.is_file():
        try:
            raw_preview = json.loads(canonical.read_text(encoding="utf-8"))
            segs_ok = bool(isinstance(raw_preview.get("segments"), list) and raw_preview["segments"])
            ref = raw_preview.get("segments_ref")
            ref_ok = (
                isinstance(ref, dict)
                and str(ref.get("format") or "").lower() == "jsonl"
                and ref.get("file")
                and (output_dir / str(ref["file"])).is_file()
            )
            if segs_ok or ref_ok:
                return canonical, "canonical_output_base_name"
        except Exception:
            pass

    original = payload.get("original_input_path") or payload.get("input_path")
    matches: list[tuple[float, str, pathlib.Path]] = []
    candidate_paths = list(output_dir.glob("*-raw.json"))
    structured_asr_dir = resolve_session_artifact_dir(payload) / "asr"
    if structured_asr_dir.is_dir():
        candidate_paths.extend(structured_asr_dir.glob("*-raw.json"))
    for p in candidate_paths:
        try:
            raw_preview = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sf = raw_preview.get("source_file") or raw_preview.get("processing_input_path")
        if not paths_match_media(sf, original):
            continue
        segs_ok = bool(isinstance(raw_preview.get("segments"), list) and raw_preview["segments"])
        ref = raw_preview.get("segments_ref")
        ref_base_dir = p.parent
        ref_ok = (
            isinstance(ref, dict)
            and str(ref.get("format") or "").lower() == "jsonl"
            and ref.get("file")
            and (ref_base_dir / str(ref["file"])).is_file()
        )
        if not segs_ok and not ref_ok:
            continue
        st = p.stat()
        created = str(raw_preview.get("created_at") or "")
        matches.append((st.st_mtime, created, p))
    if matches:
        matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return matches[0][2], "glob_source_file_match"

    found_names = sorted(f.name for f in output_dir.glob("*-raw.json"))
    raise RuntimeError(
        "speaker_pass_requires_existing_raw_json: "
        f"no usable *-raw.json for input in {output_dir}; "
        f"expected ~{canonical.name}; "
        f"found *-raw.json: {found_names or '[]'}. "
        "Hint: run the same media with execution_mode asr_only or full (same output_dir) first, then speaker_pass; "
        "or pass asr_raw_json_path to an existing *-raw.json whose source_file matches this video."
    )


def load_existing_asr_result(payload: dict) -> tuple[dict, pathlib.Path, str]:
    raw_path, tag = resolve_existing_raw_path(payload)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    try:
        segments = load_segments_from_raw_doc(raw, raw_path.parent)
    except RuntimeError as exc:
        raise RuntimeError(f"speaker_pass_raw_has_no_segments: {raw_path} ({exc})") from exc
    raw_loaded = dict(raw)
    raw_loaded["segments"] = segments
    return raw_loaded, raw_path, tag


def normalize_payload(payload: dict) -> dict:
    """Fill defaults for keys consumed in build_result/write_outputs to prevent KeyErrors.

    Idempotent: existing non-None values are preserved. Called once at start of main()
    so the rest of the pipeline can rely on these keys being present. Defense-in-depth:
    individual code paths may still use .get() for clarity.

    Why: media_transcribe_cli.py builds payload from a subset of CLI flags, while
    media_transcribe.py historically expected the watcher's full JSON payload. CLI
    runs would hit KeyError in build_result; watcher runs were safe by accident.
    """
    requested_model = payload.get("selected_model") or payload.get("requested_model") or payload.get("model")
    quality_preset = payload.get("quality_preset") or "medium"
    execution_profile_default = f"default-{quality_preset}"
    speaker_mode = payload.get("speaker_mode") or "diarize"

    safe_defaults: dict = {
        "selected_model": requested_model or "medium",
        "model": requested_model or "medium",
        "requested_model": requested_model or "medium",
        "quality_preset": quality_preset,
        "execution_profile": execution_profile_default,
        "execution_mode": payload.get("execution_mode") or "full",
        "speaker_mode": speaker_mode,
        "speaker_labels": speaker_mode not in (None, "off", "", False),
        "timestamps": "hms",
        "voiceprint_mode": "match",
        "generate_speaker_clips": speaker_mode == "diarize",
        "speaker_clip_target_sec": 60,
        "speaker_clip_dir_mode": "both",
        "speaker_clip_min_turn_sec": 2.0,
        "speaker_clip_crf": 18,
        "speaker_clip_preset": "medium",
        "chunk_minutes": 20,
        "chunk_overlap_sec": 30,
    }
    nullable_keys = {
        "language_hint", "language_detected", "model_root",
        "min_speakers", "max_speakers",
        "speaker_map_path", "zoom_vtt_path",
        "session_artifact_dir", "project_speaker_registry_path",
        "profile_store_path", "machine_local_voiceprint_store_path",
        "global_registry_dir",
        "original_input_path",
    }

    for key, default in safe_defaults.items():
        if payload.get(key) is None:
            payload[key] = default
    for key in nullable_keys:
        payload.setdefault(key, None)
    return payload


def main() -> None:
    configure_stdio_utf8()
    raw_stdin = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw_stdin.decode("utf-8-sig"))
    except UnicodeDecodeError:
        payload = json.loads(raw_stdin.decode(sys.getdefaultencoding(), errors="replace"))
    normalize_payload(payload)
    # Карта спикеров из UTF-8 JSON на диске (надёжно на Windows; PS 5.1 портит кириллицу в stdin JSON).
    smp = payload.get("speaker_map_path")
    if isinstance(smp, str) and smp.strip():
        map_path = pathlib.Path(smp.strip()).expanduser().resolve()
        if map_path.is_file():
            loaded = json.loads(map_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base = payload.get("speaker_map") if isinstance(payload.get("speaker_map"), dict) else {}
                merged = dict(base)
                for key, value in loaded.items():
                    if key is None or value is None:
                        continue
                    ks, vs = str(key).strip(), str(value).strip()
                    if ks and vs:
                        merged[ks] = vs
                payload["speaker_map"] = merged
                warnings = list(payload.get("environment", {}).get("warnings", []))
                warnings.append(f"speaker_map_loaded_from:{map_path}")
                payload.setdefault("environment", {})["warnings"] = warnings
    warnings = list(payload.get("environment", {}).get("warnings", []))
    speaker_map = normalize_speaker_map(payload)
    payload["execution_mode"] = resolve_execution_mode(payload)
    payload["speaker_mode"] = str(payload.get("speaker_mode") or "diarize")
    payload["machine_local_voiceprint_store_path"] = str(
        payload.get("machine_local_voiceprint_store_path") or payload.get("profile_store_path") or ""
    ).strip() or None
    if payload["machine_local_voiceprint_store_path"]:
        payload["profile_store_path"] = payload["machine_local_voiceprint_store_path"]
    if payload.get("session_artifact_dir"):
        payload["session_artifact_dir"] = str(
            pathlib.Path(str(payload["session_artifact_dir"]).strip()).expanduser().resolve()
        )
    else:
        payload["session_artifact_dir"] = str(resolve_session_artifact_dir(payload))
    if payload.get("project_speaker_registry_path"):
        payload["project_speaker_registry_path"] = str(
            pathlib.Path(str(payload["project_speaker_registry_path"]).strip()).expanduser().resolve()
        )
    else:
        derived_registry = resolve_project_speaker_registry_dir(payload)
        payload["project_speaker_registry_path"] = str(derived_registry) if derived_registry else None
    if payload.get("global_registry_dir"):
        payload["global_registry_dir"] = str(
            pathlib.Path(str(payload["global_registry_dir"]).strip()).expanduser().resolve()
        )
    else:
        payload["global_registry_dir"] = None
    if payload.get("zoom_vtt_path"):
        payload["zoom_vtt_path"] = str(pathlib.Path(str(payload["zoom_vtt_path"]).strip()).expanduser().resolve())
    payload["generate_speaker_clips"] = bool(payload.get("generate_speaker_clips", payload.get("speaker_mode") == "diarize"))
    payload["speaker_clip_target_sec"] = float(payload.get("speaker_clip_target_sec", 60) or 60)
    payload["speaker_clip_dir_mode"] = str(payload.get("speaker_clip_dir_mode") or "both")
    payload["speaker_clip_min_turn_sec"] = float(payload.get("speaker_clip_min_turn_sec", 2.0) or 2.0)
    payload["speaker_clip_crf"] = int(payload.get("speaker_clip_crf", 18) or 18)
    payload["speaker_clip_preset"] = str(payload.get("speaker_clip_preset") or "medium")
    payload["voiceprint_mode"] = str(payload.get("voiceprint_mode") or "match")
    if payload["execution_mode"] == "asr_only":
        payload["speaker_mode"] = "off"
        payload["voiceprint_mode"] = "off"
        payload["generate_speaker_clips"] = False
    elif payload["execution_mode"] == "speaker_pass":
        payload["speaker_mode"] = "diarize"
        payload["generate_speaker_clips"] = True
    elif payload["execution_mode"] == "merge_asr_chunks":
        payload["speaker_mode"] = "off"
        payload["voiceprint_mode"] = "off"
        payload["generate_speaker_clips"] = False
    if not payload.get("output_formats"):
        payload["output_formats"] = ["md", "json", "vtt"]
    payload.setdefault("split_raw_segments", True)
    payload.setdefault("compact_output_json", True)
    payload.setdefault("auto_recover_asr", True)
    payload.setdefault("force_fresh_asr", False)
    payload.setdefault("stdout_summary_segment_threshold", 800)
    payload.setdefault("stdout_summary_only", False)
    payload["identification"] = resolve_identification(payload)
    work_root = str(ensure_work_root(payload.get("work_root")))
    job_root = create_job_root(work_root)

    if payload.get("speaker_labels"):
        warnings.append("speaker_labels_deprecated_use_speaker_mode")

    output_dir_lock_root = pathlib.Path(payload["output_dir"]).resolve()

    try:
        log(payload, f"job work dir: {job_root}")
        acquire_output_dir_lock(output_dir_lock_root, payload["execution_mode"], warnings)
        stage_ascii_input(payload, warnings, job_root)
        audio_path = extract_audio_if_needed(payload, warnings, job_root)
        runtime = payload.get("runtime", {})
        if payload["execution_mode"] == "merge_asr_chunks":
            output_dir_m = pathlib.Path(payload["output_dir"])
            base_name_m = str(payload.get("output_base_name") or pathlib.Path(payload["input_path"]).stem)
            exp_n_m = expected_chunk_count_for_audio(audio_path, payload, warnings)
            fp_m = build_asr_recovery_fingerprint(payload, audio_path)
            merged_try, _lang_m = try_recover_from_asr_chunks(
                output_dir_m, base_name_m, exp_n_m, fp_m, payload, warnings
            )
            if not merged_try:
                raise RuntimeError(
                    "merge_asr_chunks: no complete matching chunk set in "
                    f"{output_dir_m} (expected {exp_n_m} files {base_name_m}-asr-chunk-*.json)"
                )
            merged_try.sort(key=lambda item: (item["start"], item["end"]))
            write_asr_merged_checkpoint(payload, merged_try, _lang_m, audio_path, warnings)
            out_merge = {
                "status": "ok",
                "execution_mode": "merge_asr_chunks",
                "segments_recovered": len(merged_try),
                "asr_merged_path": str(output_dir_m / f"{base_name_m}-asr-merged.json"),
            }
            print(json.dumps(out_merge, ensure_ascii=False, separators=(",", ":")))
            return
        if payload["execution_mode"] == "speaker_pass":
            existing_raw, raw_used_path, raw_tag = load_existing_asr_result(payload)
            if raw_tag in ("asr_raw_json_path", "glob_source_file_match"):
                warnings.append(f"asr_raw_json_resolved_via:{raw_tag}")
            collected = existing_raw.get("segments", [])
            detected_languages = [existing_raw.get("language_detected")] if existing_raw.get("language_detected") else []
            log(payload, f"speaker_pass loaded existing ASR segments: {len(collected)} from {raw_used_path}")
            log(payload, "phase=asr_skip speaker_pass")
        else:
            base_name = str(payload.get("output_base_name") or pathlib.Path(payload["input_path"]).stem)
            output_dir = pathlib.Path(payload["output_dir"])
            collected = []
            detected_languages = []
            recovered = False
            force_fresh = bool(payload.get("force_fresh_asr", False))
            auto_recover = bool(payload.get("auto_recover_asr", True))
            if (
                payload["execution_mode"] in ("full", "asr_only")
                and auto_recover
                and not force_fresh
            ):
                fp = build_asr_recovery_fingerprint(payload, audio_path)
                exp_n = expected_chunk_count_for_audio(audio_path, payload, warnings)
                msegs, mlang = try_recover_from_asr_merged(output_dir, base_name, fp, payload, warnings)
                if msegs is not None:
                    collected = msegs
                    if mlang:
                        detected_languages = [mlang]
                    recovered = True
                    log(payload, "phase=asr_recovered source=asr_merged")
                if not recovered:
                    csegs, clang = try_recover_from_asr_chunks(
                        output_dir, base_name, exp_n, fp, payload, warnings
                    )
                    if csegs is not None:
                        collected = csegs
                        if clang:
                            detected_languages = [clang]
                        recovered = True
                        log(payload, "phase=asr_recovered source=asr_chunks")
                        write_asr_merged_checkpoint(payload, collected, clang, audio_path, warnings)
            if not recovered:
                chunks = split_audio_into_chunks(audio_path, payload, warnings, job_root)
                payload["_asr_fingerprint_cache"] = build_asr_recovery_fingerprint(payload, audio_path)
            else:
                chunks = []
            log(
                payload,
                f"chunks prepared: {len(chunks) if not recovered else 0} (recovered={recovered}, "
                f"chunk_minutes={payload.get('chunk_minutes', 20)}, overlap={payload.get('chunk_overlap_sec', 30)}s)",
            )
            selected_model = payload.get("selected_model") or payload.get("requested_model") or payload.get("model") or "medium"
            payload["selected_model"] = selected_model
            model_path = resolve_model_path(payload.get("model_root"), selected_model)
            kwargs = {
                "beam_size": 5,
                "vad_filter": True,
                "condition_on_previous_text": True,
            }
            if payload.get("language_hint"):
                kwargs["language"] = payload["language_hint"]
            max_parallel, cpu_threads = choose_parallelism(payload, runtime, len(chunks))
            jobs = [
                {
                    "chunk_index": chunk["index"],
                    "chunk_start": chunk["start"],
                    "chunk_overlap_sec": payload.get("chunk_overlap_sec", 30),
                    "audio_path": chunk["path"],
                    "runtime_device": runtime.get("device", "cpu"),
                    "runtime_compute_type": runtime.get("compute_type", "int8"),
                    "model_path": model_path,
                    "model_root": payload.get("model_root"),
                    "cpu_threads": cpu_threads if runtime.get("device", "cpu") == "cpu" else None,
                    "kwargs": kwargs,
                }
                for chunk in chunks
            ]
            queue_size = len(jobs)
            log(payload, f"queue created: {queue_size} chunk jobs, max_parallel={max_parallel}, cpu_threads={cpu_threads}")
            chunk_phase_t0 = time.monotonic()
            if queue_size > 0:
                log(payload, "phase=asr_chunks start")
            chunk_success_done = 0
            done_count = 0
            chunk_future_timeout_sec = resolve_chunk_future_timeout_sec(payload)
            if queue_size > 0 and max_parallel <= 1:
                shared_model: WhisperModel | None = None
                try:
                    for position, job in enumerate(jobs, start=1):
                        log(
                            payload,
                            f"chunk begin {position}/{queue_size}: index={job['chunk_index']} audio={job['audio_path']}",
                        )
                        if shared_model is None:
                            log(payload, "sequential mode: loading single WhisperModel for all chunks (avoids repeated CUDA init)")
                            log(payload, "sequential shared model init begin")
                            shared_model = create_whisper_model(job)
                            log(payload, "sequential shared model init done")
                        try:
                            result = transcribe_chunk_worker(job, model=shared_model)
                        except Exception as exc:
                            msg = f"chunk_failed index={job['chunk_index']}: {exc}"
                            warnings.append(msg)
                            log(payload, f"chunk error {position}/{queue_size}: {msg}")
                            shared_model = None
                            continue
                        chunk_success_done += 1
                        eta = format_chunk_eta_suffix(chunk_success_done, queue_size, chunk_phase_t0, 1)
                        log(
                            payload,
                            f"chunk done {position}/{queue_size}: index={job['chunk_index']}, segments={len(result['segments'])}{eta}",
                        )
                        write_chunk_asr_artifacts_to_output(payload, job, result)
                        collected.extend(result["segments"])
                        if result.get("language"):
                            detected_languages.append(result["language"])
                finally:
                    # Bug 1 fix v2 2026-05-18: keepalive pattern instead of `del`.
                    # Previous fix (granular logging 2026-05-17) confirmed crash at
                    # `del shared_model` — SIGSEGV in ctranslate2/CUDA destructor on
                    # Windows + float16 + RTX 3050. Python try/except can't catch it.
                    # Solution: park the model in module-level keepalive list to prevent
                    # refcount-triggered __del__ during the run. VRAM stays allocated
                    # through diarization+alignment (~30s, ~1.5GB), reclaimed at process exit.
                    # No gc.collect() / torch.cuda.empty_cache() — same SIGSEGV path.
                    # См. memory/feedback_media_transcribe_known_bugs_2026_05_17.md
                    log(payload, "phase=sequential_whisper_release begin")
                    if shared_model is not None:
                        _WHISPER_MODEL_KEEPALIVE.append(shared_model)
                        log(
                            payload,
                            f"phase=sequential_whisper_release model_kept_alive "
                            f"(keepalive_count={len(_WHISPER_MODEL_KEEPALIVE)}) — VRAM released at process exit",
                        )
                    shared_model = None
                    log(payload, "phase=sequential_whisper_release model_dereferenced")
                    log(payload, "phase=sequential_whisper_release end")
            elif queue_size > 0:
                executor = ThreadPoolExecutor(max_workers=max_parallel)
                try:
                    future_map = {executor.submit(transcribe_chunk_worker, job): job for job in jobs}
                    pending = set(future_map.keys())
                    while pending:
                        timeout_arg = chunk_future_timeout_sec
                        done_set, pending = wait(pending, timeout=timeout_arg, return_when=FIRST_COMPLETED)
                        if not done_set:
                            warnings.append(
                                "chunk_parallel_stall: ни один чанк не завершился за "
                                f"{chunk_future_timeout_sec}s; отмена оставшихся futures ({len(pending)})."
                            )
                            log(
                                payload,
                                f"parallel chunk wait timeout after {chunk_future_timeout_sec}s, pending={len(pending)}",
                            )
                            for fut in pending:
                                fut.cancel()
                            break
                        for future in done_set:
                            job = future_map[future]
                            done_count += 1
                            try:
                                result = future.result()
                            except Exception as exc:
                                msg = f"chunk_failed index={job['chunk_index']}: {exc}"
                                warnings.append(msg)
                                log(payload, f"chunk error {done_count}/{queue_size}: {msg}")
                                continue
                            chunk_success_done += 1
                            eta = format_chunk_eta_suffix(
                                chunk_success_done, queue_size, chunk_phase_t0, max_parallel
                            )
                            log(
                                payload,
                                f"chunk done {done_count}/{queue_size}: index={job['chunk_index']}, segments={len(result['segments'])}{eta}",
                            )
                            write_chunk_asr_artifacts_to_output(payload, job, result)
                            collected.extend(result["segments"])
                            if result.get("language"):
                                detected_languages.append(result["language"])
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)

            if (
                not recovered
                and queue_size > 0
                and torch is not None
                and str(runtime.get("device", "cpu")) == "cuda"
            ):
                # Bug 1 fix v2 2026-05-18: skip explicit gc.collect() + torch.cuda.empty_cache().
                # Both can trigger destructors on WhisperModel orphan refs → CUDA SIGSEGV
                # (same crash path as `del shared_model` — see _WHISPER_MODEL_KEEPALIVE docstring).
                # Pyannote diarization that follows can allocate VRAM alongside the kept-alive
                # WhisperModel; RTX 3050 8GB has plenty of room (Whisper-medium ~1.5GB + pyannote ~2GB).
                log(payload, "phase=gpu_cache_skip after asr (bug-1 avoidance: process-exit VRAM reclaim)")

        log(payload, f"phase=after_chunk_loop start collected_segments={len(collected)} detected_languages={len(detected_languages)}")
        log(payload, "phase=sort_collected begin")
        collected.sort(key=lambda item: (item["start"], item["end"]))
        log(payload, "phase=sort_collected done")
        if payload.get("execution_mode") == "speaker_pass":
            log(
                payload,
                f"phase=post_load_raw segments={len(collected)} speaker_mode={payload.get('speaker_mode')} "
                "(далее диаризация/клипы — может занять много времени без chunk-логов)",
            )
        else:
            log(
                payload,
                f"phase=post_asr segments={len(collected)} speaker_mode={payload.get('speaker_mode')} "
                "(ASR chunks done; далее диаризация/выравнивание — может занять много времени без chunk-логов)",
            )
        language_detected = detected_languages[0] if detected_languages else None
        if payload["execution_mode"] in ("full", "asr_only") and collected:
            write_asr_merged_checkpoint(payload, collected, language_detected, audio_path, warnings)
            # Ready-to-use transcript BEFORE diarization — always leaves a usable
            # .md even if diarization is slow/hangs/interrupted (overwritten with
            # speaker labels by write_outputs once diarization completes).
            write_asr_transcript_checkpoint(payload, collected, language_detected)

        diarization_meta = {
            "enabled": payload.get("speaker_mode") == "diarize",
            "status": "disabled" if payload.get("speaker_mode") != "diarize" else "pending",
            "reason": "speaker_mode_off" if payload.get("speaker_mode") != "diarize" else None,
            "model": payload.get("diarization_model"),
        }
        speaker_turns_meta = {
            "enabled": payload.get("speaker_mode") == "diarize",
            "status": "disabled" if payload.get("speaker_mode") != "diarize" else "pending",
            "source": None,
            "speaker_turns": 0,
            "speakers_detected": 0,
            "reason": "speaker_mode_off" if payload.get("speaker_mode") != "diarize" else None,
        }
        zoom_vtt_meta = {
            "enabled": bool(str(payload.get("zoom_vtt_path") or "").strip()),
            "status": "disabled" if not str(payload.get("zoom_vtt_path") or "").strip() else "pending",
            "reason": "zoom_vtt_not_provided" if not str(payload.get("zoom_vtt_path") or "").strip() else None,
            "path": str(payload.get("zoom_vtt_path") or "").strip() or None,
        }
        alignment_meta = {
            "assigned_segments": 0,
            "unassigned_segments": len(collected),
            "speaker_turns": 0,
            "method": None,
        }
        turns: list[dict] = []
        profile_updates: list[dict] = []

        if payload.get("speaker_mode") == "diarize":
            try:
                log(payload, "phase=speaker_turns start")
                _t_spk = time.time()
                turns, diarization_meta, zoom_vtt_meta = resolve_speaker_turns(audio_path, payload, job_root)
                alignment_meta = assign_speakers_to_segments(collected, turns, speaker_map)
                _spk_elapsed = round(time.time() - _t_spk, 1)
                diarization_meta["speaker_turns"] = len(turns)
                diarization_meta["elapsed_sec"] = _spk_elapsed
                _src = diarization_meta.get("source") or "diarization"
                _assigned = alignment_meta.get("assigned_segments", 0)
                _total_seg = _assigned + alignment_meta.get("unassigned_segments", 0)
                # Highlighted, greppable line: where the speakers came from (pyannote vs an
                # external transcript), how many, and how long it took — the signal a user
                # needs to confirm diarization actually ran on their project/hub.
                log(payload,
                    f"phase=speaker_turns done | ✓ speakers resolved: source={_src} "
                    f"speakers={diarization_meta.get('speakers_detected', 0)} turns={len(turns)} "
                    f"segments_labeled={_assigned}/{_total_seg} elapsed={_spk_elapsed}s")
                speaker_turns_meta = {
                    "enabled": True,
                    "status": "ok",
                    "source": _src,
                    "speaker_turns": len(turns),
                    "speakers_detected": diarization_meta.get("speakers_detected", 0),
                    "elapsed_sec": _spk_elapsed,
                    "reason": diarization_meta.get("reason"),
                }
            except Exception as exc:
                diarization_meta = {
                    "enabled": True,
                    "status": "fallback",
                    "reason": "diarization_failed_fallback_to_asr_only",
                    "error": str(exc),
                    "model": payload.get("diarization_model"),
                    "source": "diarization",
                }
                warnings.append(f"diarization_failed: {exc}")
                speaker_turns_meta = {
                    "enabled": True,
                    "status": "fallback",
                    "source": "diarization",
                    "speaker_turns": 0,
                    "speakers_detected": 0,
                    "reason": "speaker_turns_unavailable_fallback_to_asr_only",
                }
                alignment_meta = assign_speakers_to_segments(collected, [], speaker_map)
        else:
            alignment_meta = assign_speakers_to_segments(collected, [], speaker_map)

        log(
            payload,
            f"phase=after_speaker_turns speaker_turns_status={speaker_turns_meta.get('status')} "
            f"turns={len(turns)} next=voiceprint_or_clips_or_write",
        )

        project_registry_meta = {
            "path": payload.get("project_speaker_registry_path"),
            "status": "disabled" if not payload.get("project_speaker_registry_path") else "pending",
            "profiles_updated": 0,
            "profiles": [],
        }
        global_registry_meta = {
            "path": payload.get("global_registry_dir"),
            "status": "disabled" if not payload.get("global_registry_dir") else "pending",
            "profiles_written": 0,
        }
        machine_local_store_meta = {
            "path": payload.get("machine_local_voiceprint_store_path") or payload.get("profile_store_path"),
            "status": "disabled" if not (payload.get("machine_local_voiceprint_store_path") or payload.get("profile_store_path")) else "pending",
            "profiles_updated": 0,
        }

        voiceprint_meta = {
            "enabled": payload.get("voiceprint_mode") in {"match", "enroll"},
            "mode": payload.get("voiceprint_mode", "off"),
            "status": "disabled" if payload.get("voiceprint_mode", "off") == "off" else "pending",
            "threshold": payload.get("voiceprint_threshold"),
            "store_path": payload.get("machine_local_voiceprint_store_path") or payload.get("profile_store_path"),
            "matches": {},
            "speaker_hashes": {},
            "reason": None,
        }
        voiceprint_store = None
        if payload.get("voiceprint_mode") in {"match", "enroll"}:
            if not turns:
                voiceprint_meta["status"] = "fallback"
                voiceprint_meta["reason"] = "voiceprint_requires_speaker_turns"
                warnings.append("voiceprint_skipped_no_speaker_turns")
            else:
                try:
                    log(payload, "phase=voiceprint start")
                    ffmpeg_bin = payload.get("ffmpeg_bin") or "ffmpeg"
                    speaker_embeddings_raw, extractor_meta = extract_turn_embeddings(audio_path, turns, ffmpeg_bin, job_root)
                    extractor_version = extractor_meta.get("extractor") or VOICE_EMBEDDING_DEFAULT_EXTRACTOR
                    # Per-extractor threshold; explicit payload value wins (e.g. legacy callers
                    # passing 0.84 for v0). Default for ECAPA-TDNN v1 is 0.55 (cross-speaker max ~0.43).
                    payload_threshold = payload.get("voiceprint_threshold")
                    if payload_threshold is not None:
                        threshold = float(payload_threshold)
                    else:
                        threshold = VOICE_EMBEDDING_THRESHOLDS.get(extractor_version, 0.84)
                    voiceprint_meta["extractor"] = extractor_version
                    voiceprint_meta["embedding_dim"] = extractor_meta.get("dim")
                    voiceprint_meta["threshold"] = threshold
                    speaker_embeddings = {
                        speaker_id: average_embedding(vectors)
                        for speaker_id, vectors in speaker_embeddings_raw.items()
                        if vectors
                    }
                    voiceprint_meta["speaker_hashes"] = {
                        speaker_id: build_voice_hash(vector)
                        for speaker_id, vector in speaker_embeddings.items()
                    }
                    # Expose embeddings to downstream phases (speaker_clips → ensure_profile_entry).
                    # Without this, profile creation in clip-phase loses the vector that
                    # produced voice_hash and writes embeddings=[] (Bug 7 in 258-23).
                    voiceprint_meta["speaker_embeddings"] = speaker_embeddings
                    store_path = payload.get("profile_store_path")
                    if not store_path:
                        raise RuntimeError("profile_store_path is required for voiceprint mode")
                    lock_path = pathlib.Path(str(store_path) + ".lock")
                    acquire_lock(lock_path)
                    try:
                        store = load_voiceprint_store(str(store_path))
                        voiceprint_store = store
                        # Pull-on-claim: merge shared-hub canonical profiles into the
                        # node-local store before match/enroll, so a voice enrolled on
                        # another node is recognized here (voiceprint-in-Hub, 801-o1 ph2).
                        gr_dir = str(payload.get("global_registry_dir") or "").strip()
                        proj_dir = str(payload.get("project_speaker_registry_path") or "").strip()
                        if gr_dir and proj_dir:
                            try:
                                pull_meta = sync_hub_to_local_cache(
                                    store, pathlib.Path(proj_dir), pathlib.Path(gr_dir)
                                )
                                voiceprint_meta["hub_pull"] = pull_meta
                                log(
                                    payload,
                                    "phase=voiceprint hub_pull "
                                    f"persons={pull_meta.get('persons_pulled')} "
                                    f"embeddings={pull_meta.get('embeddings_merged')} "
                                    f"missing={pull_meta.get('records_missing')}",
                                )
                            except Exception as exc:
                                voiceprint_meta["hub_pull"] = {"status": "error", "error": str(exc)}
                                warnings.append(f"hub_pull_failed: {exc}")
                        if payload.get("voiceprint_mode") == "enroll":
                            speaker_totals: dict[str, float] = defaultdict(float)
                            for turn in turns:
                                speaker_totals[turn["speaker_id"]] += max(
                                    0.0, float(turn["end"]) - float(turn["start"])
                                )
                            if not speaker_totals:
                                raise RuntimeError("no speaker turns available for enroll")
                            target_speaker = max(speaker_totals.items(), key=lambda item: item[1])[0]
                            # Build enroll_meta from VTT speaker_observations or payload
                            # voiceprint_enroll_name (CLI flag). VTT-name takes precedence
                            # when both are present; closes Bug 1 in 258-23.
                            vtt_observations = (
                                zoom_vtt_meta.get("speaker_observations", {})
                                if isinstance(zoom_vtt_meta, dict) else {}
                            )
                            vtt_name = None
                            obs_for_speaker = vtt_observations.get(target_speaker) or {}
                            if isinstance(obs_for_speaker, dict):
                                vtt_name = (obs_for_speaker.get("speaker_name") or "").strip() or None
                            payload_enroll_name = (str(payload.get("voiceprint_enroll_name") or "").strip() or None)
                            payload_contact_ref = (str(payload.get("voiceprint_contact_ref") or "").strip() or None)
                            payload_contact_name = (str(payload.get("voiceprint_contact_name") or "").strip() or None)
                            chosen_name = vtt_name or payload_enroll_name
                            enroll_meta_for_target = None
                            if chosen_name:
                                enroll_meta_for_target = {
                                    "canonical_name": chosen_name,
                                    "display_name": chosen_name.split()[0] if chosen_name else None,
                                    "contact_ref": payload_contact_ref or f"[[{chosen_name}]]",
                                    "contact_name": payload_contact_name or chosen_name,
                                }
                            enrolled = enroll_voiceprint_profile(
                                store=store,
                                speaker_id=target_speaker,
                                speaker_vectors=speaker_embeddings,
                                sample_meta={
                                    "source_file": payload.get("input_path"),
                                    "speaker_id": target_speaker,
                                    "mode": "enroll",
                                    "name_source": "zoom_vtt" if vtt_name else ("payload" if payload_enroll_name else "none"),
                                },
                                enroll_meta=enroll_meta_for_target,
                            )
                            save_voiceprint_store_atomic(str(store_path), store)
                            voiceprint_meta["enrolled"] = enrolled
                            voiceprint_meta["status"] = "ok"
                            voiceprint_meta["reason"] = "enrolled"
                        else:
                            matches = match_voiceprint_profiles(store, speaker_embeddings, threshold, extractor=extractor_version)
                            voiceprint_meta["matches"] = matches
                            voiceprint_meta["status"] = "ok"
                            voiceprint_meta["reason"] = "matched"
                    finally:
                        release_lock(lock_path)

                    if payload.get("voiceprint_mode") == "match":
                        for segment in collected:
                            if segment.get("speaker_source") == "manual_map":
                                continue
                            speaker_id = segment.get("speaker_id")
                            if not speaker_id:
                                continue
                            match = voiceprint_meta["matches"].get(speaker_id)
                            if match and match.get("matched"):
                                segment["voice_hash"] = match.get("voice_hash")
                                if match.get("contact_name"):
                                    segment["speaker_name"] = match.get("contact_name")
                                    segment["speaker"] = match.get("contact_name")
                                    segment["speaker_source"] = "voiceprint_contact"
                                else:
                                    segment["speaker_source"] = "voiceprint_hash"

                    observed_names = zoom_vtt_meta.get("speaker_observations", {}) if isinstance(zoom_vtt_meta, dict) else {}
                    observed_at = dt.datetime.now(dt.UTC).isoformat()
                    if voiceprint_store is not None and observed_names:
                        matched_profiles: dict[str, dict] = {}
                        if payload.get("voiceprint_mode") == "match":
                            for match in voiceprint_meta.get("matches", {}).values():
                                voice_hash = str(match.get("voice_hash") or "").strip()
                                if not voice_hash:
                                    continue
                                matched_profiles[voice_hash] = ensure_profile_entry(voiceprint_store, voice_hash)
                        elif payload.get("voiceprint_mode") == "enroll":
                            enrolled_hash = str((voiceprint_meta.get("enrolled") or {}).get("voice_hash") or "").strip()
                            if enrolled_hash:
                                matched_profiles[enrolled_hash] = ensure_profile_entry(voiceprint_store, enrolled_hash)

                        for speaker_id, observation in observed_names.items():
                            voice_hash = None
                            if payload.get("voiceprint_mode") == "match":
                                voice_hash = str(
                                    ((voiceprint_meta.get("matches") or {}).get(speaker_id) or {}).get("voice_hash") or ""
                                ).strip()
                            elif payload.get("voiceprint_mode") == "enroll":
                                enrolled = voiceprint_meta.get("enrolled") or {}
                                if str(enrolled.get("speaker_id") or "").strip() == str(speaker_id):
                                    voice_hash = str(enrolled.get("voice_hash") or "").strip()
                            if not voice_hash or voice_hash not in matched_profiles:
                                continue
                            profile = matched_profiles[voice_hash]
                            update = record_profile_alias_observation(
                                profile,
                                raw_name=(observation.get("raw_labels") or [observation.get("speaker_name")])[0],
                                normalized_name=observation.get("speaker_name"),
                                source="zoom_vtt",
                                observed_at=observed_at,
                                store=voiceprint_store,
                            )
                            if update:
                                update["speaker_id"] = speaker_id
                                profile_updates.append(update)
                except Exception as exc:
                    voiceprint_meta["status"] = "fallback"
                    voiceprint_meta["reason"] = "voiceprint_failed_fallback_to_diarization_only"
                    voiceprint_meta["error"] = str(exc)
                    warnings.append(f"voiceprint_failed: {exc}")

        # Speaker clips exist to put a name to an anonymous voice — reviewing it by ear,
        # or enrolling a voiceprint. An export that names its speakers leaves the first
        # job with nothing to do, so cutting clips is pure ffmpeg time. Voiceprints still
        # need the audio, so only skip the cut when they are off.
        speakers_named_by_export = (
            diarization_meta.get("source") in ("ktalk_txt", "zoom_vtt")
            and str(payload.get("voiceprint_mode") or "off") == "off"
        )
        if speakers_named_by_export and payload.get("generate_speaker_clips"):
            payload["generate_speaker_clips"] = False
            log(payload, "phase=speaker_clips skipped (speakers named by export, voiceprints off)")

        clip_generation_meta = {
            "status": "disabled" if not payload.get("generate_speaker_clips") else "pending",
            "reason": (
                ("speakers_named_by_export" if speakers_named_by_export else "clip_generation_disabled")
                if not payload.get("generate_speaker_clips") else None
            ),
            "target_sec": payload.get("speaker_clip_target_sec"),
            "storage_mode": payload.get("speaker_clip_dir_mode"),
            "clips_generated": 0,
            "clips_failed": 0,
        }
        speaker_clips = []
        if payload.get("generate_speaker_clips") and diarization_meta.get("status") == "ok":
            try:
                log(payload, "phase=speaker_clips start")
                clip_state = generate_speaker_clips(
                    payload,
                    turns,
                    {"source_file": str(pathlib.Path(payload["input_path"]).resolve()), "segments": collected, "voiceprint": voiceprint_meta},
                    warnings,
                    job_root,
                    voiceprint_store=voiceprint_store,
                )
                clip_generation_meta = {
                    "status": clip_state.get("status"),
                    "reason": clip_state.get("reason"),
                    "target_sec": clip_state.get("target_sec"),
                    "storage_mode": clip_state.get("storage_mode"),
                    "clips_generated": clip_state.get("clips_generated", 0),
                    "clips_failed": clip_state.get("clips_failed", 0),
                }
                speaker_clips = clip_state.get("clips", [])
                if voiceprint_store is not None and payload.get("profile_store_path"):
                    save_voiceprint_store_atomic(str(payload["profile_store_path"]), voiceprint_store)
                    machine_local_store_meta["status"] = "updated"
                    machine_local_store_meta["profiles_updated"] = sum(
                        len(p.get("profiles", [])) for p in voiceprint_store.get("persons", [])
                    )
            except Exception as exc:
                clip_generation_meta = {
                    "status": "fallback",
                    "reason": "speaker_clip_generation_failed",
                    "error": str(exc),
                    "target_sec": payload.get("speaker_clip_target_sec"),
                    "storage_mode": payload.get("speaker_clip_dir_mode"),
                    "clips_generated": 0,
                    "clips_failed": 0,
                }
                warnings.append(f"speaker_clip_generation_failed: {exc}")
        elif payload.get("generate_speaker_clips") and diarization_meta.get("status") != "ok":
            clip_generation_meta = {
                "status": "skipped",
                "reason": f"speaker_clips_require_successful_speaker_turns:{diarization_meta.get('status')}",
                "target_sec": payload.get("speaker_clip_target_sec"),
                "storage_mode": payload.get("speaker_clip_dir_mode"),
                "clips_generated": 0,
                "clips_failed": 0,
            }

        registry_dir_str = str(payload.get("project_speaker_registry_path") or "").strip()
        if registry_dir_str and voiceprint_store is not None:
            try:
                registry_dir = pathlib.Path(registry_dir_str)
                candidate_hashes: set[str] = {
                    str(item.get("voice_hash") or "").strip()
                    for item in profile_updates
                    if str(item.get("voice_hash") or "").strip()
                }
                enrolled = voiceprint_meta.get("enrolled") or {}
                if str(enrolled.get("voice_hash") or "").strip():
                    candidate_hashes.add(str(enrolled.get("voice_hash") or "").strip())
                for match in (voiceprint_meta.get("matches") or {}).values():
                    voice_hash = str((match or {}).get("voice_hash") or "").strip()
                    if voice_hash:
                        candidate_hashes.add(voice_hash)
                project_id_val = str(payload.get("project_id") or "").strip() or None
                global_dir_str = str(payload.get("global_registry_dir") or "").strip()
                global_dir = pathlib.Path(global_dir_str) if global_dir_str else None
                synced_profiles = []
                global_written = 0
                for voice_hash in sorted(candidate_hashes):
                    profile = ensure_profile_entry(voiceprint_store, voice_hash)
                    synced_profiles.append(
                        sync_profile_to_project_registry(
                            profile,
                            registry_dir,
                            project_id=project_id_val,
                        )
                    )
                    if global_dir is not None:
                        # Full profile (with embeddings) → shared hub; roster link → project.
                        sync_profile_to_global_registry(profile, global_dir, project_id=project_id_val)
                        sync_project_members(
                            profile.get("person_id"),
                            profile.get("canonical_name"),
                            voice_hash,
                            registry_dir,
                            project_id=project_id_val,
                        )
                        global_written += 1
                project_registry_meta["status"] = "updated" if synced_profiles else "not_needed"
                project_registry_meta["profiles_updated"] = len(synced_profiles)
                project_registry_meta["profiles"] = synced_profiles
                if global_dir is not None:
                    global_registry_meta["status"] = "updated" if global_written else "not_needed"
                    global_registry_meta["profiles_written"] = global_written
            except Exception as exc:
                project_registry_meta["status"] = "error"
                project_registry_meta["error"] = str(exc)
                if payload.get("global_registry_dir"):
                    global_registry_meta["status"] = "error"
                    global_registry_meta["error"] = str(exc)
                warnings.append(f"project_registry_sync_failed: {exc}")
        elif registry_dir_str:
            project_registry_meta["status"] = "not_needed"

        if (
            voiceprint_store is not None
            and payload.get("profile_store_path")
            and machine_local_store_meta["status"] == "pending"
            and (profile_updates or payload.get("voiceprint_mode") == "match")
        ):
            save_voiceprint_store_atomic(str(payload["profile_store_path"]), voiceprint_store)
            machine_local_store_meta["status"] = "updated"
            machine_local_store_meta["profiles_updated"] = len(voiceprint_store.get("profiles", []))

        if machine_local_store_meta["status"] == "pending":
            machine_local_store_meta["status"] = "not_needed"

        log(payload, "phase=build_result begin")
        duration = round(collected[-1]["end"], 3) if collected else 0.0
        result = {
            "status": "ok",
            "engine": "faster-whisper",
            "source_file": str(pathlib.Path(payload.get("original_input_path") or payload["input_path"]).resolve()),
            "processing_input_path": str(pathlib.Path(payload["input_path"]).resolve()),
            "work_root": work_root,
            "job_work_dir": str(job_root),
            "source_type": "video" if pathlib.Path(payload["input_path"]).suffix.lower() in VIDEO_EXTENSIONS else "audio",
            "model": payload.get("selected_model") or payload.get("model") or "medium",
            "quality_preset": payload.get("quality_preset") or "medium",
            "execution_profile": payload.get("execution_profile") or f"default-{payload.get('quality_preset') or 'medium'}",
            "execution_mode": payload.get("execution_mode") or "full",
            "language_hint": payload.get("language_hint"),
            "language_detected": language_detected,
            "duration_sec": duration,
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "warnings": warnings,
            "speaker_map": speaker_map,
            "diarization": diarization_meta,
            "speaker_turns": speaker_turns_meta,
            "alignment": alignment_meta,
            "voiceprint": voiceprint_meta,
            "zoom_vtt": zoom_vtt_meta,
            "profile_updates": profile_updates,
            "profile_sync": {
                "status": (
                    "project_and_machine_local"
                    if project_registry_meta.get("status") == "updated" and machine_local_store_meta.get("status") == "updated"
                    else "project_only"
                    if project_registry_meta.get("status") == "updated"
                    else "machine_local_only"
                    if machine_local_store_meta.get("status") == "updated"
                    else "not_needed"
                ),
                "reason": "project_registry_primary_machine_local_cache_secondary",
                "updated_profiles": len({item.get("voice_hash") for item in profile_updates if item.get("voice_hash")}),
            },
            "project_registry": project_registry_meta,
            "global_registry": global_registry_meta,
            "machine_local_store": machine_local_store_meta,
            "speaker_clips": speaker_clips,
            "speaker_review": {},
            "clip_generation": clip_generation_meta,
            "segments": collected,
        }
        result["speaker_review"] = build_speaker_review(result)
        log(payload, "phase=build_result done")
        log(payload, "phase=write_outputs start")
        result["outputs"] = write_outputs(payload, result)
        log(
            payload,
            f"phase=stdout_json ok outputs={json.dumps(result.get('outputs', {}), ensure_ascii=False)}",
        )
        n_stdout_seg = len(result.get("segments") or [])
        th = int(payload.get("stdout_summary_segment_threshold") or 800)
        if bool(payload.get("stdout_summary_only")) or n_stdout_seg >= th:
            summary_out = {k: v for k, v in result.items() if k != "segments"}
            summary_out["segments_omitted"] = True
            summary_out["segment_count"] = n_stdout_seg
            print(json.dumps(summary_out, ensure_ascii=False, separators=(",", ":")))
        elif bool(payload.get("compact_output_json", True)):
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        try:
            log(payload, f"fatal_error: {exc.__class__.__name__}: {exc}")
            log(payload, traceback.format_exc())
        except Exception:
            pass
        raise
    finally:
        release_output_dir_lock(output_dir_lock_root)
        if job_root.exists():
            shutil.rmtree(job_root, ignore_errors=True)


if __name__ == "__main__":
    main()
