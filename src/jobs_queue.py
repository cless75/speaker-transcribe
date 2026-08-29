"""Declarative Hub jobs queue and shared run journal.

Claims are supplied by ``audio_inbox_watch`` so jobs reuse the exact claim,
lease, verification, and heartbeat implementation used for inbox media.
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable


JOB_TYPES = {"asr-revariant", "word-timestamps", "slide-frames", "rediarize",
             "fetch-model"}

# fetch-model возит на узел ВЕСА, а не код: задание называет модель, загрузку выполняет
# этот модуль, приехавший релизным каналом (org/node-autorun-via-jobs-queue-not-scripts).
# Имя репозитория берётся из белого списка, а не из задания как есть: иначе файл в общей
# папке Хаба становится каналом «скачай на боевой узел что угодно» — ровно то, что
# запрещало решение о канале автозапуска.
MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}
# Параметры, исполнимые для перегонки. У fetch-model свой набор — см. _eligibility.
EXECUTABLE_PARAMS = {
    "model", "quality_preset", "speaker_mode", "asr_variant_id", "timestamps", "glossary",
    "slide_frames", "slide_ocr", "slide_ocr_engine", "slide_interval_sec",
    "scene_threshold", "slide_min_ocr_chars", "slide_dedupe",
    "slide_dedupe_visual",
}
# params.glossary names a LEVEL, not a file. "project" (the default) means "pass no
# flag at all — the engine resolves {hub_root}/{pid}/_PROJECT-glossary.json itself".
# Passing "project" through as --glossary would be read as a path, fail to load, and
# switch hints OFF: the run level outranks the project level. That inversion is the
# whole reason these jobs were held back.
# params.model names the faster-whisper WEIGHTS; params.quality_preset is only a LABEL.
# They are separate on purpose: the CLI picks weights from selected_model/requested_model/
# model and ignores quality_preset entirely, so a job that sets the preset alone runs on
# whatever the node defaults to. That is exactly what happened on 22.08: a run labelled
# large-v3 executed on medium and the measurement compared medium with medium
# (org/preset-measure-2026-08-22-invalid). A job that asks for weights must say so here.
MODEL_LABEL_MISMATCH = "requested model {requested!r} but run-meta reports {actual!r}"
# Names faster-whisper resolves to weights. A quality_preset holding one of these is a
# job asking for those weights - and, without params.model, asking in a way the CLI does
# not honour. Before params.model existed such a job at least died in preflight (no
# faster-whisper-large-v3 cached); keeping that barrier explicit is the point of the
# check in _eligibility (review F5, 28.08).
KNOWN_MODEL_NAMES = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
    "large", "large-v1", "large-v2", "large-v3", "large-v3-turbo", "turbo",
    "distil-small.en", "distil-medium.en", "distil-large-v2", "distil-large-v3",
}

GLOSSARY_PROJECT_LEVEL = {"project", "", "auto", "default"}
GLOSSARY_OFF = {"off", "none", "false", "0"}
HINTS_CONFIRMATION = "glossary prompts on"

READY_STATUSES = {"pending", "ready"}
ORPHANABLE_STATUSES = {"claimed", "running"}
TERMINAL_STATUSES = {"done", "failed"}
DEFAULT_RUN_LOG_RETENTION_DAYS = 90
DEFAULT_TAIL_LINES = 80


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_iso() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_token(value: object, fallback: str = "job") -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-._")
    return token[:120] or fallback


def _write_json(path: pathlib.Path, value: dict) -> None:
    """Atomic UTF-8-without-BOM JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_log(path: pathlib.Path, stage: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _utcnow().isoformat(timespec="seconds").replace("+00:00", "Z")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for line in (str(message).splitlines() or [""]):
            stream.write(f"{timestamp} stage={stage} {line}\n")


def _node_value(cfg: dict, key: str, default=None):
    node = cfg.get("node") or {}
    return node[key] if key in node else cfg.get(key, default)


def _host(cfg: dict) -> str:
    return str(_node_value(cfg, "host_label", None) or os.environ.get("COMPUTERNAME")
               or os.environ.get("HOSTNAME") or "unknown-host")


def _hub(cfg: dict) -> pathlib.Path:
    return pathlib.Path(str(cfg["hub_root"])).expanduser()


def _run_paths(cfg: dict, job_id: str, now: dt.datetime | None = None) -> tuple[str, pathlib.Path, pathlib.Path]:
    now = now or _utcnow()
    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{_safe_token(job_id)}"
    root = _hub(cfg) / "_runs" / _safe_token(_host(cfg), "unknown-host") / now.strftime("%Y-%m-%d")
    return run_id, root / f"{run_id}.log", root / f"{run_id}.json"


def rotate_run_logs(cfg: dict, now: dt.datetime | None = None) -> list[pathlib.Path]:
    """Delete expired .log files only; JSON summaries remain permanently."""
    runs = _hub(cfg) / "_runs"
    if not runs.is_dir():
        return []
    days = int(cfg.get("run_log_retention_days", DEFAULT_RUN_LOG_RETENTION_DAYS))
    cutoff = (now or _utcnow()).timestamp() - max(0, days) * 86400
    removed: list[pathlib.Path] = []
    for path in runs.rglob("*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed


def _claim_cfg(cfg: dict) -> dict:
    """Jobs are shared across nodes, so claiming is always multi-machine."""
    claim_cfg = dict(cfg)
    claim_cfg["enable_multi_machine"] = True
    return claim_cfg


def _read_job(path: pathlib.Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("job JSON must be an object")
    return value


def _sort_key(item: tuple[pathlib.Path, dict]) -> tuple[int, str, str]:
    path, job = item
    try:
        priority = int(job.get("priority", 999))
    except (TypeError, ValueError):
        priority = 999
    return priority, str(job.get("created_at") or "9999"), path.name


def probe_free_vram_gb() -> float:
    query = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        check=True, timeout=10,
    )
    return max(float(line.strip()) for line in query.stdout.splitlines() if line.strip()) / 1024


def _eligibility(job: dict, cfg: dict,
                 vram_probe: Callable[[], float] = probe_free_vram_gb) -> str | None:
    job_type = job.get("type")
    if job_type not in JOB_TYPES:
        return f"unsupported job type: {job_type!r}; allowed={sorted(JOB_TYPES)}"
    params = job.get("params") or {}
    if not isinstance(params, dict):
        return "params must be an object"
    if job_type == "fetch-model":
        name = str(params.get("model") or "").strip()
        if not name:
            return "fetch-model requires params.model"
        if name not in MODEL_REPOS:
            return (f"unknown model {name!r}; allowed={sorted(MODEL_REPOS)} "
                    "(the list lives in the code, not in the job)")
        return None
    unsupported = sorted(set(params) - EXECUTABLE_PARAMS)
    if unsupported:
        return f"unsupported params for this node CLI: {unsupported}"
    model = str(params.get("model") or "").strip()
    if model:
        # Weights are a NAME, not a path: a path lands in the output folder name and can
        # never equal what run-meta reports, so such a job is unrunnable by construction
        # (review F9, 28.08).
        if any(sep in model for sep in ("/", "\\", ":")):
            return (f"params.model must be a model name, not a path: {model!r}; "
                    "point the node config at a model_root instead")
    preset = str(params.get("quality_preset") or "").strip()
    if preset in KNOWN_MODEL_NAMES and preset != _requested_model(job, cfg):
        # The preset is a label the CLI ignores when picking weights. Asking for large-v3
        # this way is how 22.08 ran on medium and measured nothing
        # (org/preset-measure-2026-08-22-invalid).
        return (f"quality_preset {preset!r} names weights but params.model resolves to "
                f"{_requested_model(job, cfg)!r}: set params.model explicitly - the preset "
                "alone does not select weights")
    requires = job.get("requires") or {}
    if not isinstance(requires, dict):
        return "requires must be an object"
    required = set(requires.get("capabilities") or [])
    available = set(_node_value(cfg, "capabilities", []) or [])
    missing = sorted(required - available)
    if missing:
        return f"requires mismatch: missing capabilities {missing}; node has {sorted(available)}"
    try:
        min_vram = float(requires.get("min_vram_gb") or 0)
    except (TypeError, ValueError):
        return "requires.min_vram_gb must be a number"
    if min_vram > 0:
        configured = _node_value(cfg, "free_vram_gb", None)
        try:
            free_vram = float(configured) if configured is not None else float(vram_probe())
        except Exception as exc:
            return f"requires mismatch: cannot determine free VRAM ({type(exc).__name__}: {exc})"
        if free_vram < min_vram:
            return f"requires mismatch: free VRAM {free_vram:.2f} GB is below {min_vram:.2f} GB"
    return None


def _skip_paths(cfg: dict, job_id: str, reason: str) -> tuple[str, pathlib.Path, pathlib.Path]:
    """Deterministic per (node, job, reason) — the dedupe key of the skip journal."""
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:8]
    run_id = f"skip-{_safe_token(job_id)}-{digest}"
    root = _hub(cfg) / "_runs" / _safe_token(_host(cfg), "unknown-host") / "skips"
    return run_id, root / f"{run_id}.log", root / f"{run_id}.json"


def _hints_expected(job: dict) -> bool:
    """True when the job explicitly asked for glossary hints (any level but off)."""
    params = job.get("params") or {}
    if "glossary" not in params:
        return False
    return str(params.get("glossary")).strip().lower() not in GLOSSARY_OFF


def _model_in_outputs(output_dir: pathlib.Path, since: float) -> str | None:
    """Weights of THIS run, from the run-meta this run wrote.

    Never "the first run-meta in the folder": a variant folder accumulates outputs of
    earlier runs, and the boldest example lives in the combat hub - a 22.08 file under
    transcripts-large-v3/asr/ saying model=medium sorts ahead of everything and would
    fail an honest large-v3 run after 110 GPU minutes (review F1, 28.08). Only files
    this run touched count, newest first; nothing older is evidence about it.
    """
    fresh = []
    for meta_path in output_dir.rglob("*run-meta.json"):
        try:
            if meta_path.stat().st_mtime + 1 < since:
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        model = meta.get("model") or (meta.get("asr_variant") or {}).get("model")
        if model:
            fresh.append((meta_path.stat().st_mtime, str(model)))
    if not fresh:
        return None
    return max(fresh)[1]


def _log_mentions(log_path: pathlib.Path, needle: str) -> bool:
    try:
        return needle in log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False



def _journal_skip(cfg: dict, job_path: pathlib.Path, job: dict, reason: str) -> dict | None:
    """Record why this node passed a job over — ONCE per (node, job, reason).

    A skip repeats on every sweep by nature: an unsupported param or a capability
    mismatch does not change between ticks. Journalling it each time buried the real
    runs under hundreds of identical summaries a day and, since summaries are kept
    forever by design, grew without bound on the cloud-synced hub. The reason is still
    on record — it is simply written once, and again only if the reason changes.
    """
    job_id = str(job.get("job_id") or job_path.stem)
    run_id, log_path, summary_path = _skip_paths(cfg, job_id, reason)
    if summary_path.exists():
        return None
    _append_log(log_path, "selection", f"SKIPPED job={job_id}: {reason}")
    summary = {
        "run_id": run_id, "status": "skipped", "node": _host(cfg),
        "job_id": job_id, "job_file": str(job_path),
        "input_file": (job.get("target") or {}).get("input"), "return_code": None,
        "reason": reason, "tail_output": reason, "log_path": str(log_path),
        "started_at": _utc_iso(), "finished_at": _utc_iso(),
    }
    _write_json(summary_path, summary)
    return summary


def _resolve_input(job: dict, cfg: dict) -> pathlib.Path:
    raw = str((job.get("target") or {}).get("input") or "")
    if not raw:
        raise ValueError("target.input is required")
    root = _hub(cfg).resolve()
    candidate = pathlib.Path(raw)
    if candidate.is_absolute():
        raise ValueError("target.input must be relative to hub_root")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("target.input escapes hub_root") from exc
    if not resolved.is_file():
        raise ValueError(f"target.input not found: {raw}")
    return resolved


# Where a job's product lands, by job type. A re-run belongs BESIDE the session's
# own transcripts, in the same folder the stop-gap runner used
# (org/jobs-queue-writes-beside-session-transcripts) — otherwise the acceptance of
# measurement 2, written against transcripts-hints, stops matching the moment the
# queue replaces the runner. Provenance moves to the journal, not the path: which
# job produced a file is read from _runs/** and the job's own result.output_paths.
OUTPUT_SUBDIR = {
    "asr-revariant": "transcripts-hints",
    "word-timestamps": "transcripts-words",
    "rediarize": "transcripts-rediarized",
    "slide-frames": "slides",
}


def _output_dir(job: dict, cfg: dict) -> pathlib.Path:
    target = job.get("target") or {}
    pid = _safe_token(target.get("project_id"), "_unknown")
    sid = _safe_token(target.get("session_id"), "no-session")
    month_match = re.match(r"S(\d{4})(\d{2})", sid)
    if month_match:
        root = _hub(cfg) / pid / "sessions" / f"{month_match.group(1)}-{month_match.group(2)}" / sid
    else:
        root = _hub(cfg) / pid / "_job-results" / sid
    # Like beside like, the F6 rule of the previous review, extended rather than
    # overwritten: hints keep transcripts-hints, other weights get their own folder, and
    # a run that changes BOTH says so in the name instead of silently claiming one of the
    # two homes (review F10, 28.08).
    model = ((job.get("params") or {}).get("model") or "").strip()
    if model and str(job.get("type")) == "asr-revariant":
        name = f"transcripts-{_safe_token(model, 'model')}"
        if _hints_expected(job):
            name += "-hints"
        return root / name
    subdir = OUTPUT_SUBDIR.get(str(job.get("type")))
    if subdir:
        return root / subdir
    return root / "jobs" / _safe_token(job.get("job_id"))


_PREFLIGHT_CODE = r'''
import importlib.metadata, json, pathlib, subprocess, sys
preset, min_vram = sys.argv[1], float(sys.argv[2])
result = {"python": sys.executable, "preset": preset}
for package, key in (("faster-whisper", "faster_whisper"), ("ctranslate2", "ctranslate2")):
    result[key] = importlib.metadata.version(package)
model = pathlib.Path(preset).expanduser()
if model.is_dir():
    result["model"] = str(model.resolve())
else:
    from huggingface_hub import snapshot_download
    result["model"] = snapshot_download("Systran/faster-whisper-" + preset, local_files_only=True)
result["free_vram_gb"] = None
if min_vram > 0:
    query = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                           text=True, encoding="utf-8", capture_output=True, check=True)
    free = max(float(line.strip()) for line in query.stdout.splitlines() if line.strip()) / 1024
    result["free_vram_gb"] = round(free, 2)
    if free < min_vram:
        raise RuntimeError(f"free VRAM {free:.2f} GB is below required {min_vram:.2f} GB")
print(json.dumps(result, ensure_ascii=False))
'''


def run_preflight(python: str, preset: str, min_vram_gb: float) -> dict:
    proc = subprocess.run(
        [python, "-c", _PREFLIGHT_CODE, preset, str(min_vram_gb)],
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        check=False, timeout=60,
    )
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "preflight failed")[-4000:])
    try:
        return json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"invalid preflight output: {proc.stdout[-1000:]}") from exc


def _requested_model(job: dict, cfg: dict) -> str:
    """Weights the job asks for: run level, then node, then the engine default."""
    params = job.get("params") or {}
    return str(params.get("model") or _node_value(cfg, "model", None) or "medium")


def _payload_config(job: dict, cfg: dict) -> dict:
    payload = dict(cfg)
    params = job.get("params") or {}
    for key in ("model", "quality_preset", "speaker_mode", "asr_variant_id", "timestamps"):
        if key in params:
            payload[key] = params[key]
    if job.get("type") == "rediarize":
        payload["execution_mode"] = "speaker_pass"
        payload["speaker_mode"] = "diarize"
    if job.get("type") == "word-timestamps":
        payload["timestamps"] = "both"
    if job.get("type") == "slide-frames":
        frames = dict(payload.get("video_frames") or {})
        frames["mode"] = params.get("slide_frames", "interval")
        if "slide_ocr" in params:
            frames["ocr"] = str(params["slide_ocr"]).lower() not in {"off", "false", "0"}
        payload["video_frames"] = frames
    return payload


def _build_command(job: dict, cfg: dict, input_path: pathlib.Path,
                   output_dir: pathlib.Path) -> tuple[list[str], pathlib.Path]:
    python = str(_node_value(cfg, "transcribe_python", None) or sys.executable).strip(' "\'')
    cli = str(cfg.get("transcribe_cli") or (pathlib.Path(__file__).parent / "media_transcribe_cli.py"))
    fd, temp_name = tempfile.mkstemp(prefix="jobs-queue-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(_payload_config(job, cfg), stream, ensure_ascii=False, indent=2)
    temp_path = pathlib.Path(temp_name)
    params = job.get("params") or {}
    target = job.get("target") or {}
    timestamps = params.get("timestamps") or ("both" if job.get("type") == "word-timestamps" else cfg.get("timestamps", "both"))
    command = [
        python, cli, "--config", str(temp_path), "--input", str(input_path),
        "--output-dir", str(output_dir), "--project-id", str(target.get("project_id") or ""),
        "--output-base-name", _safe_token(params.get("asr_variant_id") or input_path.stem,
                                          _safe_token(input_path.stem, "job-output")),
        "--quality-preset", str(params.get("quality_preset") or cfg.get("quality_preset", "medium")),
        "--speaker-mode", str(params.get("speaker_mode") or cfg.get("speaker_mode", "diarize")),
        "--timestamps", str(timestamps),
    ]
    # Weights before anything optional: without this flag the CLI falls back to "medium"
    # no matter what the preset label says (org/preset-measure-2026-08-22-invalid).
    if params.get("model"):
        command += ["--model", str(params["model"])]
    if job.get("type") == "rediarize":
        command += ["--execution-mode", "speaker_pass"]
    if job.get("type") == "slide-frames":
        command += ["--slide-frames", str(params.get("slide_frames", "interval"))]
    glossary = params.get("glossary")
    if glossary is not None:
        level = str(glossary).strip().lower()
        if level in GLOSSARY_OFF:
            command += ["--glossary", "off"]
        elif level not in GLOSSARY_PROJECT_LEVEL:
            command += ["--glossary", str(glossary)]
    option_map = {
        "slide_ocr": "--slide-ocr", "slide_ocr_engine": "--slide-ocr-engine",
        "slide_interval_sec": "--slide-interval-sec", "scene_threshold": "--scene-threshold",
        "slide_min_ocr_chars": "--slide-min-ocr-chars", "slide_dedupe": "--slide-dedupe",
        "slide_dedupe_visual": "--slide-dedupe-visual",
    }
    for key, option in option_map.items():
        if key in params:
            value = params[key]
            if key == "slide_ocr" and isinstance(value, bool):
                value = "on" if value else "off"
            command += [option, str(value)]
    return command, temp_path


def execute_cli(command: list[str], log_path: pathlib.Path, tail_lines: int) -> tuple[int, str]:
    """Stream both outputs to Hub and retain only a bounded failure tail."""
    tail: collections.deque[str] = collections.deque(maxlen=max(1, tail_lines))
    lock = threading.Lock()
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)

        def consume(pipe, label: str) -> None:
            for line in iter(pipe.readline, ""):
                rendered = f"{label}: {line.rstrip()}"
                with lock:
                    stream.write(rendered + "\n")
                    stream.flush()
                    tail.append(rendered)
            pipe.close()

        threads = [threading.Thread(target=consume, args=(proc.stdout, "stdout"), daemon=True),
                   threading.Thread(target=consume, args=(proc.stderr, "stderr"), daemon=True)]
        for thread in threads:
            thread.start()
        return_code = proc.wait()
        for thread in threads:
            thread.join()
    return return_code, "\n".join(tail)


_FETCH_CODE = r'''
import json, sys
from huggingface_hub import snapshot_download
repo = sys.argv[1]
path = snapshot_download(repo, local_files_only=False)
print(json.dumps({"repo": repo, "path": path}, ensure_ascii=False))
'''


def fetch_model(python: str, repo: str, timeout_sec: int = 3600) -> dict:
    """Скачать веса в локальный кеш узла. Возвращает путь снапшота.

    Отдельный процесс тем же интерпретатором, что гоняет ASR: кеш HuggingFace
    привязан к окружению, и скачанное «куда-то ещё» движку не поможет.
    """
    proc = subprocess.run([python, "-c", _FETCH_CODE, repo], text=True, encoding="utf-8",
                          errors="replace", capture_output=True, check=False,
                          timeout=timeout_sec)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "fetch failed")[-4000:])
    try:
        return json.loads(proc.stdout.splitlines()[-1])
    except Exception as exc:
        raise RuntimeError(f"invalid fetch output: {proc.stdout[-1000:]}") from exc


def _engine_version() -> str:
    try:
        return (pathlib.Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def process_next_job(
    cfg: dict,
    config_path: pathlib.Path,
    *,
    claim_job: Callable[[pathlib.Path, dict], bool],
    retire_job: Callable[[pathlib.Path, dict], None],
    heartbeat_factory: Callable[[pathlib.Path, dict], object],
    claim_is_stale: Callable[[pathlib.Path, dict], bool] | None = None,
    log: Callable[[str], None] | None = None,
    preflight: Callable[[str, str, float], dict] = run_preflight,
    fetcher: Callable[[str, str, int], dict] = fetch_model,
    cli_executor: Callable[[list[str], pathlib.Path, int], tuple[int, str]] = execute_cli,
    vram_probe: Callable[[], float] = probe_free_vram_gb,
) -> dict | None:
    """Process at most one eligible job, then return control to ordinary inboxes.

    Every sweep says what it saw, even when it saw nothing. Silence here cost a whole
    diagnosis round: the node log says "queue empty" about INBOX files, the jobs queue
    said nothing at all, and from the outside an unrun queue step looked exactly like
    a queue that ran and found no work.
    """
    say = log or (lambda _message: None)
    rotate_run_logs(cfg)
    jobs_dir = _hub(cfg) / "_jobs"
    if not jobs_dir.is_dir():
        say(f"jobs queue: {jobs_dir} not present — nothing to read this sweep")
        return None
    candidates: list[tuple[pathlib.Path, dict]] = []
    total = skipped = finished = blocked = 0
    for path in sorted(jobs_dir.glob("*.json")):
        if path.name.endswith(".claim.json"):
            continue
        total += 1
        try:
            job = _read_job(path)
        except Exception as exc:
            _journal_skip(cfg, path, {"job_id": path.stem}, f"invalid job JSON: {exc}")
            continue
        status = job.get("status")
        if status == "blocked":
            blocked += 1
            continue
        if status in TERMINAL_STATUSES:
            finished += 1
            continue
        if status not in READY_STATUSES:
            # A job stamped claimed/running by a node that then died stays stamped: the
            # claim lease frees the file, but the status alone would keep every node off
            # it forever. Liveness is decided by the lease, not by the stamp — the same
            # rule the inbox flow uses for an in-progress file.
            if status not in ORPHANABLE_STATUSES or claim_is_stale is None:
                continue
            if not claim_is_stale(path, _claim_cfg(cfg)):
                continue
            job = dict(job, status="pending", reclaimed_from=status)
        reason = _eligibility(job, cfg, vram_probe)
        if reason:
            skipped += 1
            say(f"jobs queue: skip {path.name}: {reason}")
            _journal_skip(cfg, path, job, reason)
            continue
        candidates.append((path, job))
    if not candidates:
        say(f"jobs queue: {total} file(s), 0 runnable here "
            f"({skipped} skipped, {finished} finished, {blocked} blocked)")
        return None
    say(f"jobs queue: {total} file(s), {len(candidates)} runnable "
        f"({skipped} skipped, {finished} finished, {blocked} blocked)")

    job_path, job = sorted(candidates, key=_sort_key)[0]
    say(f"jobs queue: taking {job_path.name}")
    reclaimed_from = job.get("reclaimed_from")
    claim_cfg = _claim_cfg(cfg)
    if not claim_job(job_path, claim_cfg):
        return None
    job = _read_job(job_path)
    if job.get("status") not in READY_STATUSES:
        stale = (job.get("status") in ORPHANABLE_STATUSES and claim_is_stale is not None
                 and claim_is_stale(job_path, claim_cfg))
        if not stale:
            retire_job(job_path, claim_cfg)
            return None
        reclaimed_from = job.get("status")

    job_id = str(job.get("job_id") or job_path.stem)
    run_id, log_path, summary_path = _run_paths(cfg, job_id)
    started = time.monotonic()
    run_started_at = time.time()
    started_at = _utc_iso()
    job.update({"status": "claimed", "claimed_by": _host(cfg), "claimed_at": started_at})
    job.pop("reclaimed_from", None)
    _write_json(job_path, job)
    _append_log(log_path, "claim", f"claimed job={job_id} node={_host(cfg)}")
    if reclaimed_from:
        _append_log(log_path, "claim",
                    f"reclaimed orphaned job={job_id} from status={reclaimed_from} "
                    f"(claim lease expired)")

    summary: dict = {}
    temp_config: pathlib.Path | None = None
    try:
        if job.get("type") == "fetch-model":
            params = job.get("params") or {}
            name = str(params.get("model")).strip()
            repo = MODEL_REPOS[name]
            python = str(_node_value(cfg, "transcribe_python", None) or sys.executable).strip(' "\'')
            job.update({"status": "running", "started_at": _utc_iso(), "run_id": run_id,
                        "run_log": str(log_path), "run_summary": str(summary_path)})
            _write_json(job_path, job)
            _append_log(log_path, "fetch", f"downloading {repo} into the node cache")
            with heartbeat_factory(job_path, claim_cfg):
                fetched = fetcher(python, repo,
                                  int(cfg.get("model_fetch_timeout_sec", 3600)))
            duration = round(time.monotonic() - started, 3)
            _append_log(log_path, "result", f"status=done path={fetched.get('path')}")
            summary = {
                "run_id": run_id, "status": "done", "node": _host(cfg), "job_id": job_id,
                "job_type": "fetch-model", "job_file": str(job_path),
                "input_file": None, "engine_version": _engine_version(),
                "preset": name, "environment": fetched, "duration_sec": duration,
                "return_code": 0, "output_paths": [str(fetched.get("path") or "")],
                "tail_output": "", "log_path": str(log_path),
                "started_at": started_at, "finished_at": _utc_iso(),
            }
            job["status"] = "done"
            job["finished_at"] = summary["finished_at"]
            job["result"] = {"run_id": run_id, "return_code": 0,
                             "output_paths": summary["output_paths"],
                             "run_log": str(log_path), "run_summary": str(summary_path),
                             "tail_output": ""}
            return summary
        input_path = _resolve_input(job, cfg)
        output_dir = _output_dir(job, cfg)
        output_dir.mkdir(parents=True, exist_ok=True)
        params = job.get("params") or {}
        # Preflight resolves weights by name (snapshot_download of faster-whisper-<name>),
        # so it must be handed the model, not the label — otherwise a job asking for
        # large-v3 is cleared by the presence of medium.
        preset = str(params.get("quality_preset") or cfg.get("quality_preset", "medium"))
        weights = _requested_model(job, cfg)
        min_vram = float((job.get("requires") or {}).get("min_vram_gb") or 0)
        python = str(_node_value(cfg, "transcribe_python", None) or sys.executable).strip(' "\'')

        job.update({"status": "running", "started_at": _utc_iso(), "run_id": run_id,
                    "run_log": str(log_path), "run_summary": str(summary_path)})
        _write_json(job_path, job)
        with heartbeat_factory(job_path, claim_cfg):
            environment = preflight(python, weights, min_vram)
            _append_log(log_path, "preflight", "environment=" + json.dumps(environment, ensure_ascii=False))
            command, temp_config = _build_command(job, cfg, input_path, output_dir)
            _append_log(log_path, "cli", f"start type={job.get('type')} input={input_path}")
            return_code, tail = cli_executor(command, log_path, int(cfg.get("job_output_tail_lines", DEFAULT_TAIL_LINES)))

        duration = round(time.monotonic() - started, 3)
        output_paths = [str(path) for path in output_dir.rglob("*") if path.is_file()]
        status = "done" if return_code == 0 and output_paths else "failed"
        if return_code == 0 and not output_paths:
            tail = (tail + "\n" if tail else "") + "CLI returned 0 but produced no output files"
        # A job that asked for hints and got none produced an ordinary re-run, not the
        # thing it was created for. Scoring such output as success is how 90 minutes of
        # GPU were spent measuring nothing on 27.08
        # (org/measurement-invalid-without-confirmed-hints). The engine announces hints
        # early in the run, so the journal - not the bounded tail - is where to look.
        if status == "done" and _hints_expected(job) and not _log_mentions(log_path, HINTS_CONFIRMATION):
            status = "failed"
            tail = ((tail + chr(10)) if tail else "") + (
                "hints were requested but the engine never reported "
                f"'{HINTS_CONFIRMATION}' - this output is a plain re-run, "
                "not a hinted variant; not scored")
        # Same stance as the hints gate above, for weights. A run that asked for large-v3
        # and quietly executed on medium is not the thing the job was created for: on
        # 22.08 exactly that produced a measurement comparing medium with medium, and it
        # stood as fact for five days (org/preset-measure-2026-08-22-invalid). The engine
        # writes the weights it used into run-meta, so the answer is on disk, not in a tail.
        requested_model = (job.get("params") or {}).get("model")
        actual_model = _model_in_outputs(output_dir, run_started_at) if status == "done" else None
        if status == "done" and requested_model:
            requested = str(requested_model)
            if actual_model is None:
                # Symmetry with the hints gate: absence of proof is not proof of the
                # right weights. An unverifiable run is exactly the 22.08 shape - it
                # looked finished and measured nothing (review F4, 28.08).
                status = "failed"
                tail = ((tail + chr(10)) if tail else "") + (
                    f"requested model {requested!r} but no run-meta from this run was "
                    "found in the output - weights unverifiable; not scored")
            elif actual_model != requested:
                status = "failed"
                tail = ((tail + chr(10)) if tail else "") + MODEL_LABEL_MISMATCH.format(
                    requested=requested, actual=actual_model
                ) + " - this output is not the variant the job asked for; not scored"
        summary = {
            "run_id": run_id, "status": status, "node": _host(cfg), "job_id": job_id,
            "model_requested": requested_model, "model_used": actual_model,
            "job_type": job.get("type"), "job_file": str(job_path), "input_file": str(input_path),
            "engine_version": _engine_version(), "preset": preset, "weights": weights,
            "environment": environment,
            "duration_sec": duration, "return_code": return_code, "output_paths": output_paths,
            "tail_output": tail, "log_path": str(log_path), "started_at": started_at,
            "finished_at": _utc_iso(),
        }
        _append_log(log_path, "result", f"status={status} return_code={return_code} duration_sec={duration}")
        if status == "failed":
            _append_log(log_path, "failure-tail", tail)
        job["status"] = status
        job["finished_at"] = summary["finished_at"]
        job["result"] = {"run_id": run_id, "return_code": return_code,
                         "output_paths": output_paths, "run_log": str(log_path),
                         "run_summary": str(summary_path), "tail_output": tail if status == "failed" else ""}
    except Exception as exc:
        duration = round(time.monotonic() - started, 3)
        reason = f"{type(exc).__name__}: {exc}"
        _append_log(log_path, "failure", reason)
        summary = {
            "run_id": run_id, "status": "failed", "node": _host(cfg), "job_id": job_id,
            "job_type": job.get("type"), "job_file": str(job_path),
            "input_file": (job.get("target") or {}).get("input"), "engine_version": _engine_version(),
            "preset": (job.get("params") or {}).get("quality_preset"), "duration_sec": duration,
            "return_code": None, "output_paths": [], "tail_output": reason,
            "log_path": str(log_path), "started_at": started_at, "finished_at": _utc_iso(),
        }
        job["status"] = "failed"
        job["finished_at"] = summary["finished_at"]
        job["result"] = {"run_id": run_id, "return_code": None, "output_paths": [],
                         "run_log": str(log_path), "run_summary": str(summary_path),
                         "tail_output": reason}
    finally:
        if temp_config is not None:
            try:
                temp_config.unlink()
            except OSError:
                pass
        _write_json(summary_path, summary)
        _write_json(job_path, job)
        retire_job(job_path, claim_cfg)
    return summary
