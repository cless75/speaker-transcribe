#!/usr/bin/env python3
"""Standalone CLI wrapper for media_transcribe.py.

Builds a JSON payload from argparse-friendly CLI flags and pipes it to the
existing worker (``media_transcribe.py``) via stdin. The wrapper exists so
that media transcription can be launched from a terminal, a scheduler, or a
shell script — not only from an agent that already speaks the JSON-payload
protocol.

The Inbox folder is a regular CLI parameter (``--inbox PATH``), never a
vault-canonical location. It is forwarded to the worker as
``payload.inbox_path`` (and into ``run_meta.identification`` for downstream
processing).

Example::

    python media_transcribe_cli.py \
        --input "C:/work/recordings/meeting.m4a" \
        --output-dir "C:/work/output/my-project" \
        --project-id my-project
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


_DEFAULT_AUDIO_INBOX_CONFIG = (
    pathlib.Path(__file__).resolve().parent.parent / "audio-inbox-config.json"
)


def _resolve_default_runtime() -> dict:
    """Default runtime for CLI: read `runtime` block from audio-inbox-config.json
    if present; otherwise auto-detect CUDA (prefer GPU if available).

    This mirrors the watcher's default so /process-audio-inbox and CLI behave the
    same way out of the box (GPU on this host). Explicit --device / --compute-type
    flags always override.
    """
    runtime: dict = {}
    try:
        if _DEFAULT_AUDIO_INBOX_CONFIG.is_file():
            data = json.loads(_DEFAULT_AUDIO_INBOX_CONFIG.read_text(encoding="utf-8"))
            block = data.get("runtime") or {}
            if isinstance(block, dict):
                runtime = {k: v for k, v in block.items() if not k.startswith("_")}
    except Exception:
        runtime = {}

    if not runtime.get("device"):
        try:
            import torch  # type: ignore
            if torch.cuda.is_available():
                runtime["device"] = "cuda"
                runtime.setdefault("compute_type", "float16")
                runtime.setdefault("diarization_device", "cuda")
            else:
                runtime["device"] = "cpu"
                runtime.setdefault("compute_type", "int8")
        except Exception:
            runtime.setdefault("device", "cpu")
            runtime.setdefault("compute_type", "int8")
    elif runtime.get("device") == "cuda":
        # Config requested CUDA but host may not have it (e.g. Mac M-series).
        # Fall back to CPU+int8 instead of hanging in CTranslate2 CUDA init.
        try:
            import torch  # type: ignore
            cuda_ok = torch.cuda.is_available()
        except Exception:
            cuda_ok = False
        if not cuda_ok:
            runtime["device"] = "cpu"
            runtime["compute_type"] = "int8"
            runtime.pop("diarization_device", None)
    return runtime


def _build_payload(args: argparse.Namespace) -> dict:
    payload: dict = {}
    if args.config_path:
        config_path = pathlib.Path(args.config_path).expanduser().resolve()
        payload = json.loads(config_path.read_text(encoding="utf-8"))

    payload["input_path"] = str(pathlib.Path(args.input_path).expanduser().resolve())
    payload["output_dir"] = str(pathlib.Path(args.output_dir).expanduser().resolve())

    runtime = dict(payload.get("runtime") or {})
    defaults = _resolve_default_runtime()
    for key, value in defaults.items():
        runtime.setdefault(key, value)
    if args.device:
        runtime["device"] = args.device
    if args.compute_type:
        runtime["compute_type"] = args.compute_type
    if args.diarization_device:
        runtime["diarization_device"] = args.diarization_device
    # Final guard: if config requested CUDA but host can't run it (e.g. Mac M-series
    # with cpu-only ctranslate2), fall back to CPU+int8 instead of hanging/crashing
    # in CTranslate2 init. Explicit --device flag (set above) is always respected.
    if not args.device and runtime.get("device") == "cuda":
        try:
            import torch  # type: ignore
            cuda_ok = torch.cuda.is_available()
        except Exception:
            cuda_ok = False
        if not cuda_ok:
            runtime["device"] = "cpu"
            runtime["compute_type"] = "int8"
            runtime.pop("diarization_device", None)
    if runtime:
        payload["runtime"] = runtime

    if args.project_id:
        payload["project_id"] = args.project_id
    if args.course_code:
        payload["course_code"] = args.course_code
    if args.inbox_path:
        payload["inbox_path"] = str(pathlib.Path(args.inbox_path).expanduser().resolve())

    if args.model:
        payload["model"] = args.model
    if args.quality_preset:
        payload["quality_preset"] = args.quality_preset
    if args.execution_mode:
        payload["execution_mode"] = args.execution_mode
    if args.speaker_mode:
        payload["speaker_mode"] = args.speaker_mode
    if args.speaker_map_path:
        payload["speaker_map_path"] = str(
            pathlib.Path(args.speaker_map_path).expanduser().resolve()
        )
    if args.zoom_vtt_path:
        payload["zoom_vtt_path"] = str(
            pathlib.Path(args.zoom_vtt_path).expanduser().resolve()
        )
    if args.ktalk_txt_path:
        payload["ktalk_txt_path"] = str(
            pathlib.Path(args.ktalk_txt_path).expanduser().resolve()
        )
    if args.enroll_name:
        # The enroll path (media_transcribe voiceprint phase) names the dominant speaker
        # from payload["voiceprint_enroll_name"] — wire the CLI flag to that field.
        payload["voiceprint_enroll_name"] = args.enroll_name
    if args.work_root:
        payload["work_root"] = args.work_root
    if args.output_base_name:
        payload["output_base_name"] = args.output_base_name
    if args.timestamps:
        payload["timestamps"] = args.timestamps
    if args.project_speaker_registry_path:
        payload["project_speaker_registry_path"] = str(
            pathlib.Path(args.project_speaker_registry_path).expanduser().resolve()
        )
    if args.machine_local_voiceprint_store_path:
        payload["machine_local_voiceprint_store_path"] = str(
            pathlib.Path(args.machine_local_voiceprint_store_path).expanduser().resolve()
        )
    if args.voiceprint_mode:
        payload["voiceprint_mode"] = args.voiceprint_mode

    identification = dict(payload.get("identification") or {})
    if args.project_id:
        identification["project_id"] = args.project_id
    if args.course_code:
        identification["course_code"] = args.course_code
    if args.inbox_path:
        identification["inbox_path"] = payload["inbox_path"]
    if identification:
        payload["identification"] = identification
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run media_transcribe.py with CLI args (builds JSON payload).",
    )
    parser.add_argument(
        "--input", "--input-path", dest="input_path", required=True,
        help="Path to input audio/video file.",
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory for ASR outputs.",
    )
    parser.add_argument(
        "--project-id", dest="project_id", default=None,
        help="ProjectId (e.g. 478). Recorded in run_meta.identification.",
    )
    parser.add_argument(
        "--course-code", dest="course_code", default=None,
        help="Course code (mdpg/stai/e101/...) used for ProductCode mapping.",
    )
    parser.add_argument(
        "--inbox", dest="inbox_path", default=None,
        help="Inbox folder where the source file was discovered. CLI parameter, not a vault canon.",
    )
    parser.add_argument(
        "--config", dest="config_path", default=None,
        help="Optional JSON file with payload defaults (merged before CLI overrides).",
    )
    parser.add_argument("--model", default=None, help="faster-whisper model.")
    parser.add_argument("--quality-preset", dest="quality_preset", default=None)
    parser.add_argument("--execution-mode", dest="execution_mode", default=None)
    parser.add_argument(
        "--device", dest="device", default=None,
        choices=("cuda", "cpu"),
        help=(
            "Runtime device for faster-whisper. Default: read `runtime.device` "
            "from audio-inbox-config.json; fallback to cuda if torch detects GPU, "
            "else cpu."
        ),
    )
    parser.add_argument(
        "--compute-type", dest="compute_type", default=None,
        help="ctranslate2 compute_type (e.g. float16 for cuda, int8 for cpu).",
    )
    parser.add_argument(
        "--diarization-device", dest="diarization_device", default=None,
        choices=("cuda", "cpu"),
        help="Device for pyannote diarization. Default mirrors --device.",
    )
    parser.add_argument("--speaker-mode", dest="speaker_mode", default=None)
    parser.add_argument("--speaker-map", dest="speaker_map_path", default=None)
    parser.add_argument("--zoom-vtt", dest="zoom_vtt_path", default=None)
    parser.add_argument(
        "--ktalk-txt", dest="ktalk_txt_path", default=None,
        help=(
            "Ktalk transcript export (tab-separated 'HH:MM:SS<TAB>speaker<TAB>text'). "
            "Speakers are taken from it by name and diarization is skipped."
        ),
    )
    parser.add_argument("--work-root", dest="work_root", default=None)
    parser.add_argument("--output-base-name", dest="output_base_name", default=None)
    parser.add_argument("--timestamps", default=None, choices=("hms", "vtt", "both", "none"))
    parser.add_argument(
        "--project-speaker-registry", dest="project_speaker_registry_path", default=None,
        help="Directory for the per-project speaker registry (index.json + profiles/). "
             "Set to the project root to keep voiceprints with the project.",
    )
    parser.add_argument(
        "--voiceprint-store", dest="machine_local_voiceprint_store_path", default=None,
        help="Node-local voiceprint store file (embeddings) used for match/enroll. Keep off the shared hub.",
    )
    parser.add_argument("--voiceprint-mode", dest="voiceprint_mode", default=None,
                        help="off | match | enroll")
    parser.add_argument(
        "--enroll-name", dest="enroll_name", default=None,
        help="Enroll the voice(s) in this file under this canonical name. For a curated "
             "single-speaker sample (bootstrap a project registry). Use with "
             "--voiceprint-mode enroll.",
    )
    parser.add_argument(
        "--worker", default=None,
        help="Path to media_transcribe.py (defaults to the file alongside this script).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the assembled payload JSON to stdout instead of running the worker.",
    )
    args = parser.parse_args()

    payload = _build_payload(args)
    payload_json = json.dumps(payload, ensure_ascii=False)

    if args.dry_run:
        print(payload_json)
        return 0

    worker_path = (
        pathlib.Path(args.worker).expanduser().resolve()
        if args.worker
        else (pathlib.Path(__file__).resolve().parent / "media_transcribe.py")
    )
    if not worker_path.is_file():
        sys.stderr.write(f"worker not found: {worker_path}\n")
        return 2

    proc = subprocess.run(
        [sys.executable, str(worker_path)],
        input=payload_json,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
