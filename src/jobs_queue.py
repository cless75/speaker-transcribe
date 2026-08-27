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


JOB_TYPES = {"asr-revariant", "word-timestamps", "slide-frames", "rediarize"}
EXECUTABLE_PARAMS = {
    "quality_preset", "speaker_mode", "asr_variant_id", "timestamps",
    "slide_frames", "slide_ocr", "slide_ocr_engine", "slide_interval_sec",
    "scene_threshold", "slide_min_ocr_chars", "slide_dedupe",
    "slide_dedupe_visual",
}
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
    unsupported = sorted(set(params) - EXECUTABLE_PARAMS)
    if unsupported:
        return f"unsupported params for this node CLI: {unsupported}"
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


def _payload_config(job: dict, cfg: dict) -> dict:
    payload = dict(cfg)
    params = job.get("params") or {}
    for key in ("quality_preset", "speaker_mode", "asr_variant_id", "timestamps"):
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
    if job.get("type") == "rediarize":
        command += ["--execution-mode", "speaker_pass"]
    if job.get("type") == "slide-frames":
        command += ["--slide-frames", str(params.get("slide_frames", "interval"))]
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
    preflight: Callable[[str, str, float], dict] = run_preflight,
    cli_executor: Callable[[list[str], pathlib.Path, int], tuple[int, str]] = execute_cli,
    vram_probe: Callable[[], float] = probe_free_vram_gb,
) -> dict | None:
    """Process at most one eligible job, then return control to ordinary inboxes."""
    rotate_run_logs(cfg)
    jobs_dir = _hub(cfg) / "_jobs"
    if not jobs_dir.is_dir():
        return None
    candidates: list[tuple[pathlib.Path, dict]] = []
    for path in sorted(jobs_dir.glob("*.json")):
        if path.name.endswith(".claim.json"):
            continue
        try:
            job = _read_job(path)
        except Exception as exc:
            _journal_skip(cfg, path, {"job_id": path.stem}, f"invalid job JSON: {exc}")
            continue
        status = job.get("status")
        if status == "blocked" or status in TERMINAL_STATUSES:
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
            _journal_skip(cfg, path, job, reason)
            continue
        candidates.append((path, job))
    if not candidates:
        return None

    job_path, job = sorted(candidates, key=_sort_key)[0]
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
        input_path = _resolve_input(job, cfg)
        output_dir = _output_dir(job, cfg)
        output_dir.mkdir(parents=True, exist_ok=True)
        params = job.get("params") or {}
        preset = str(params.get("quality_preset") or cfg.get("quality_preset", "medium"))
        min_vram = float((job.get("requires") or {}).get("min_vram_gb") or 0)
        python = str(_node_value(cfg, "transcribe_python", None) or sys.executable).strip(' "\'')

        job.update({"status": "running", "started_at": _utc_iso(), "run_id": run_id,
                    "run_log": str(log_path), "run_summary": str(summary_path)})
        _write_json(job_path, job)
        with heartbeat_factory(job_path, claim_cfg):
            environment = preflight(python, preset, min_vram)
            _append_log(log_path, "preflight", "environment=" + json.dumps(environment, ensure_ascii=False))
            command, temp_config = _build_command(job, cfg, input_path, output_dir)
            _append_log(log_path, "cli", f"start type={job.get('type')} input={input_path}")
            return_code, tail = cli_executor(command, log_path, int(cfg.get("job_output_tail_lines", DEFAULT_TAIL_LINES)))

        duration = round(time.monotonic() - started, 3)
        output_paths = [str(path) for path in output_dir.rglob("*") if path.is_file()]
        status = "done" if return_code == 0 and output_paths else "failed"
        if return_code == 0 and not output_paths:
            tail = (tail + "\n" if tail else "") + "CLI returned 0 but produced no output files"
        summary = {
            "run_id": run_id, "status": status, "node": _host(cfg), "job_id": job_id,
            "job_type": job.get("type"), "job_file": str(job_path), "input_file": str(input_path),
            "engine_version": _engine_version(), "preset": preset, "environment": environment,
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
