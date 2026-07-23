#!/usr/bin/env python
"""Slide-frame capture + local OCR for video sources (opt-in engine stage).

Self-contained module: it does NOT import ``media_transcribe`` (no circular
dependency). The worker calls :func:`run_stage` after the ASR result is built
and before ``write_outputs``; the returned dict is attached to
``result["slides"]`` and drives the transcript embedding.

Pipeline
--------
1. Detect *key moments* in the video. Default ``mode="slide-change"`` uses
   ffmpeg scene detection (``select='gt(scene,THRESHOLD)'`` + ``showinfo``);
   ``mode="interval"`` samples every ``interval_sec`` seconds.
2. Capture one PNG per key moment into ``<output_dir>/frames/`` via ffmpeg.
3. Run a local OCR engine (RapidOCR by default, CPU) over each PNG to extract
   the text written on the slide. Fully local — nothing leaves the machine.
4. Write ``<output_dir>/<base_name>-slides.json`` (machine-readable) and return
   metadata whose ``slides`` list the worker interleaves into the transcript.

Everything degrades gracefully: missing ffmpeg → skipped; missing RapidOCR →
frames captured without text. The ASR run is never broken by this stage.

Design canon (801): mechanism lives in the engine, default OFF; policy (enable,
which OCR engine, embed-in-transcript) is set by the consumer (258). Slide
images and OCR text are *content/data* → they live in the session folder and
are covered by ``.gitignore``, never committed. See
``2026/801/801-a1-Video-Frames-Slide-Description-Research.md``.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

DEFAULT_SCENE_THRESHOLD = 0.4
DEFAULT_MIN_GAP_SEC = 2.0        # collapse near-duplicate detections (in-slide animation)
DEFAULT_INTERVAL_SEC = 60.0
DEFAULT_OCR_ENGINE = "rapidocr"
DEFAULT_FRAMES_DIR = "frames"
MAX_FRAMES_CAP = 500             # safety bound; excess is dropped + logged (no silent cap)

_PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


def _noop_log(_message: str) -> None:
    return None


def _hms(total_seconds: float) -> str:
    whole = max(0, int(float(total_seconds or 0)))
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


def _ffmpeg_available(ffmpeg_bin: str) -> bool:
    import shutil
    return pathlib.Path(ffmpeg_bin).exists() or bool(shutil.which(ffmpeg_bin))


def resolve_config(payload: dict) -> dict | None:
    """Return a normalized config dict if the stage is enabled for this input,
    else ``None`` (disabled / not a video)."""
    vf = payload.get("video_frames")
    if not isinstance(vf, dict):
        return None
    mode = str(vf.get("mode") or "off").strip().lower()
    if mode in ("", "off", "none"):
        return None
    suffix = pathlib.Path(str(payload.get("input_path") or "")).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        return {"_not_video": True, "mode": mode}
    ocr_enabled = bool(vf.get("ocr", True))
    return {
        "mode": mode if mode in ("slide-change", "interval") else "slide-change",
        "scene_threshold": float(vf.get("scene_threshold", DEFAULT_SCENE_THRESHOLD) or DEFAULT_SCENE_THRESHOLD),
        "min_gap_sec": float(vf.get("min_gap_sec", DEFAULT_MIN_GAP_SEC) or DEFAULT_MIN_GAP_SEC),
        "interval_sec": float(vf.get("interval_sec", DEFAULT_INTERVAL_SEC) or DEFAULT_INTERVAL_SEC),
        "ocr": ocr_enabled,
        "ocr_engine": str(vf.get("ocr_engine") or DEFAULT_OCR_ENGINE).strip().lower(),
        "embed_in_transcript": bool(vf.get("embed_in_transcript", True)),
        "frames_dir": str(vf.get("frames_dir") or DEFAULT_FRAMES_DIR),
    }


def detect_slide_changes(
    video_path: str,
    ffmpeg_bin: str,
    threshold: float,
    min_gap_sec: float,
    log_fn=_noop_log,
) -> list[float]:
    """Timestamps (seconds) of scene changes via ffmpeg. Always includes t=0
    (first slide). Near-duplicates within ``min_gap_sec`` are collapsed."""
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-",
    ]
    log_fn(f"slide_frames: scene-detect threshold={threshold} -> {video_path}")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    times = [0.0]
    for match in _PTS_TIME_RE.finditer(proc.stderr or ""):
        try:
            times.append(float(match.group(1)))
        except ValueError:
            continue
    times.sort()
    deduped: list[float] = []
    for t in times:
        if not deduped or (t - deduped[-1]) >= min_gap_sec:
            deduped.append(round(t, 3))
    return deduped


def interval_timestamps(duration_sec: float, interval_sec: float) -> list[float]:
    if interval_sec <= 0 or duration_sec <= 0:
        return [0.0]
    out, t = [], 0.0
    while t < duration_sec:
        out.append(round(t, 3))
        t += interval_sec
    return out or [0.0]


_SEEK_COARSE_MARGIN_SEC = 10.0


def capture_frame(video_path: str, time_sec: float, out_png: pathlib.Path, ffmpeg_bin: str) -> bool:
    """Capture a single frame at ``time_sec``. Returns success.

    Two-stage seek: a coarse ``-ss`` before ``-i`` jumps to a keyframe just ahead
    of the target (fast), then a fine ``-ss`` after ``-i`` decodes forward to the
    exact instant. Plain input-seek lands on the nearest packet, which on VP8/VP9
    (WebM from Meet/Telemost) is usually not a keyframe — the decoder then hits
    partial frames and fails with "Invalid data found", which is why slide capture
    was silently failing on every WebM recording.
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(time_sec))
    coarse = max(0.0, start - _SEEK_COARSE_MARGIN_SEC)
    pre_ss = ["-ss", f"{coarse:.3f}"] if coarse > 0 else []
    fine = start - coarse
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-y",
        *pre_ss, "-i", video_path, "-ss", f"{fine:.3f}",
        "-frames:v", "1", "-q:v", "2", str(out_png),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return proc.returncode == 0 and out_png.exists() and out_png.stat().st_size > 0


class _OcrRunner:
    """Lazy wrapper over a local OCR engine. Currently RapidOCR (ONNX, CPU)."""

    def __init__(self, engine_name: str):
        self.engine_name = engine_name
        self.available = False
        self.reason: str | None = None
        self._engine = None
        try:
            if engine_name == "rapidocr":
                from rapidocr_onnxruntime import RapidOCR  # type: ignore
                self._engine = RapidOCR()
                self.available = True
            else:
                self.reason = f"unsupported_engine:{engine_name}"
        except Exception as exc:  # pragma: no cover - optional dependency
            self.reason = f"import_failed:{exc}"

    def text_for(self, png_path: pathlib.Path) -> str:
        if not self.available or self._engine is None:
            return ""
        try:
            result, _elapse = self._engine(str(png_path))
        except Exception:
            return ""
        if not result:
            return ""
        # RapidOCR item: [box(4 points), text, score]. Sort top-to-bottom, left-to-right.
        def _sort_key(item):
            try:
                box = item[0]
                ys = [pt[1] for pt in box]
                xs = [pt[0] for pt in box]
                return (round(min(ys) / 10.0), min(xs))
            except Exception:
                return (0.0, 0.0)

        pieces = []
        for item in sorted(result, key=_sort_key):
            try:
                text = str(item[1]).strip()
            except Exception:
                text = ""
            if text:
                pieces.append(text)
        return " ".join(pieces).strip()


def build_slide_markdown(slide: dict, frames_dir_name: str) -> str:
    """Render the transcript-embedded block for one slide (portable Markdown)."""
    hms = slide.get("time_hms") or _hms(slide.get("time_sec") or 0)
    image = slide.get("image") or ""
    rel = f"{frames_dir_name}/{image}" if image else ""
    lines = [f"> **🖼 Слайд @ {hms}**", ">"]
    if rel:
        lines.append(f"> ![Слайд {hms}]({rel})")
        lines.append(">")
    text = " ".join((slide.get("ocr_text") or "").split()).strip()
    if text:
        lines.append(f"> **Текст слайда (OCR):** {text}")
    else:
        lines.append("> *(текст слайда не распознан)*")
    return "\n".join(lines) + "\n\n"


def run_stage(
    payload: dict,
    ffmpeg_bin: str = "ffmpeg",
    warnings: list | None = None,
    log_fn=_noop_log,
    duration_sec: float | None = None,
) -> dict:
    """Orchestrate capture + OCR. Never raises — returns a status dict."""
    if warnings is None:
        warnings = []
    config = resolve_config(payload)
    if config is None:
        return {"enabled": False, "status": "disabled"}
    if config.get("_not_video"):
        warnings.append("slide_frames_skipped_source_not_video")
        return {"enabled": True, "status": "skipped", "reason": "source_not_video"}

    if not _ffmpeg_available(ffmpeg_bin):
        warnings.append("slide_frames_skipped_ffmpeg_unavailable")
        return {"enabled": True, "status": "skipped", "reason": "ffmpeg_unavailable"}

    input_path = str(pathlib.Path(payload["input_path"]).resolve())
    output_dir = pathlib.Path(payload["output_dir"]).resolve()
    base_name = str(payload.get("output_base_name") or pathlib.Path(input_path).stem)
    frames_dir_name = config["frames_dir"]
    frames_dir = output_dir / frames_dir_name

    try:
        if config["mode"] == "interval":
            times = interval_timestamps(float(duration_sec or 0.0), config["interval_sec"])
        else:
            times = detect_slide_changes(
                input_path, ffmpeg_bin, config["scene_threshold"], config["min_gap_sec"], log_fn
            )
    except Exception as exc:
        warnings.append(f"slide_frames_detect_failed: {exc}")
        return {"enabled": True, "status": "error", "reason": f"detect_failed:{exc}"}

    dropped = 0
    if len(times) > MAX_FRAMES_CAP:
        dropped = len(times) - MAX_FRAMES_CAP
        times = times[:MAX_FRAMES_CAP]
        warnings.append(f"slide_frames_capped: kept {MAX_FRAMES_CAP}, dropped {dropped}")
        log_fn(f"slide_frames: capped at {MAX_FRAMES_CAP} frames, dropped {dropped}")

    log_fn(f"slide_frames: {len(times)} key moments (mode={config['mode']}) -> capturing")

    ocr = _OcrRunner(config["ocr_engine"]) if config["ocr"] else None
    if config["ocr"] and ocr is not None and not ocr.available:
        warnings.append(f"slide_frames_ocr_unavailable: {ocr.reason}")

    slides: list[dict] = []
    for index, t in enumerate(times, start=1):
        hms = _hms(t)
        image_name = f"slide-{index:03d}-{hms.replace(':', '-')}.png"
        out_png = frames_dir / image_name
        if not capture_frame(input_path, t, out_png, ffmpeg_bin):
            warnings.append(f"slide_frames_capture_failed_at:{hms}")
            continue
        ocr_text = ocr.text_for(out_png) if (ocr and ocr.available) else ""
        slides.append({
            "index": index,
            "time_sec": t,
            "time_hms": hms,
            "image": image_name,
            "image_rel": f"{frames_dir_name}/{image_name}",
            "ocr_text": ocr_text,
        })

    ocr_meta = {
        "enabled": bool(config["ocr"]),
        "engine": config["ocr_engine"] if config["ocr"] else None,
        "status": (
            "disabled" if not config["ocr"]
            else "ok" if (ocr and ocr.available)
            else "unavailable"
        ),
        "reason": (ocr.reason if (ocr and not ocr.available) else None),
    }

    meta = {
        "enabled": True,
        "status": "ok" if slides else "error",
        "mode": config["mode"],
        "frames_dir": frames_dir_name,
        "embed_in_transcript": config["embed_in_transcript"],
        "count": len(slides),
        "dropped": dropped,
        "ocr": ocr_meta,
        "slides": slides,
    }
    if not slides:
        meta["reason"] = "no_frames_captured"
        warnings.append("slide_frames_no_frames_captured")
        return meta

    json_path = output_dir / f"{base_name}-slides.json"
    try:
        json_path.write_text(
            json.dumps(
                {k: v for k, v in meta.items() if k != "embed_in_transcript"},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        meta["json_path"] = str(json_path)
    except Exception as exc:
        warnings.append(f"slide_frames_json_write_failed: {exc}")

    ocred = sum(1 for s in slides if s.get("ocr_text"))
    log_fn(
        f"slide_frames: done ✓ frames={len(slides)} ocr_text={ocred}/{len(slides)} "
        f"engine={ocr_meta['engine']} status={ocr_meta['status']} dir={frames_dir}"
    )
    return meta
