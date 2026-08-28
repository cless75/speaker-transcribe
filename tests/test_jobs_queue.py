from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import pathlib
import time

import audio_inbox_watch as watcher
import jobs_queue


FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "801o11-real-job.json"


def write_json(path: pathlib.Path, value: dict) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def base_job(**overrides) -> dict:
    job = {
        "job_id": "2026-08-21-700o27-revariant-platforma",
        "type": "asr-revariant",
        "created_at": "2026-08-21T13:40:00Z",
        "created_by": "test",
        "reason": "acceptance",
        "requires": {"capabilities": ["gpu-cuda"], "min_vram_gb": 6},
        "priority": 1,
        "target": {
            "project_id": "700",
            "session_id": "S20260810T2110-platforma",
            "input": "700/_700_inbox/input.m4a",
        },
        "params": {
            "quality_preset": "medium", "speaker_mode": "diarize",
            "asr_variant_id": "medium-700o27",
        },
        "status": "pending",
    }
    job.update(overrides)
    return job


def cfg_for(hub: pathlib.Path, capabilities=("gpu-cuda",)) -> dict:
    return {
        "hub_root": str(hub),
        "node": {"host_label": "TEST-NODE", "capabilities": list(capabilities), "free_vram_gb": 8},
        "enable_multi_machine": True,
        "claim_sync_wait_seconds": 0,
        "claim_lease_minutes": 30,
        "quality_preset": "medium",
        "speaker_mode": "diarize",
        "timestamps": "both",
    }


def prepare(hub: pathlib.Path, job: dict) -> pathlib.Path:
    input_path = hub / job["target"]["input"]
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"fixture media")
    return write_json(hub / "_jobs" / f"{job['job_id']}.json", job)


class FakeHeartbeat:
    def __init__(self, path, cfg):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def environment(*args) -> dict:
    return {
        "python": args[0], "faster_whisper": "1.2.1", "ctranslate2": "4.6.0",
        "model": "cached/medium", "preset": args[1], "free_vram_gb": 8.0,
    }


def run(hub: pathlib.Path, job: dict, *, capabilities=("gpu-cuda",), executor=None,
        hints_confirmed=False):
    path = prepare(hub, job)
    calls = []

    def fake_executor(command, log_path, tail_lines):
        calls.append(command)
        jobs_queue._append_log(log_path, "worker", "FIRST_CHUNK")
        if hints_confirmed:
            # Exactly what the engine prints when project hints resolve.
            jobs_queue._append_log(
                log_path, "worker",
                "glossary prompts on: source=project terms=23 fp=a1b2c3d4e5f60718")
        out = pathlib.Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "result-transcript.md").write_text("ok", encoding="utf-8")
        return (executor or (lambda: (0, "worker tail")))()

    summary = jobs_queue.process_next_job(
        cfg_for(hub, capabilities), pathlib.Path("node.json"),
        claim_job=watcher.try_claim_file, retire_job=watcher.retire_claim,
        heartbeat_factory=FakeHeartbeat, preflight=environment, cli_executor=fake_executor,
    )
    return path, calls, summary


def all_run_files(hub: pathlib.Path) -> list[pathlib.Path]:
    return [path for path in (hub / "_runs").rglob("*") if path.is_file()]


def test_gpu_node_executes_once_and_job_remains_traceable(tmp_path):
    path, calls, summary = run(tmp_path, base_job())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert summary["status"] == "done" and summary["return_code"] == 0
    assert saved["status"] == "done"
    assert saved["result"]["run_log"] and saved["result"]["run_summary"]
    assert saved["result"]["output_paths"]
    assert path.exists()
    claim = json.loads(path.with_suffix(".json.claim.json").read_text(encoding="utf-8"))
    assert claim["claim"]["claimed_by"] == "TEST-NODE"
    assert claim["claim"]["claim_phase"] == "done"

    # A terminal job cannot create a second claim/run.
    again = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"), claim_job=lambda *_: (_ for _ in ()).throw(AssertionError()),
        retire_job=lambda *_: None, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=lambda *_: (_ for _ in ()).throw(AssertionError()),
    )
    assert again is None


def test_cpu_skips_gpu_job_without_claim_or_execution_and_logs_reason(tmp_path):
    path, calls, summary = run(tmp_path, base_job(), capabilities=("cpu",))
    assert calls == [] and summary is None
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "pending"
    assert not path.with_suffix(".json.claim.json").exists()
    logs = [p.read_text(encoding="utf-8") for p in all_run_files(tmp_path) if p.suffix == ".log"]
    assert any("missing capabilities" in text and "gpu-cuda" in text for text in logs)


def test_gpu_with_insufficient_vram_skips_before_claim(tmp_path):
    job = base_job()
    path = prepare(tmp_path, job)
    cfg = cfg_for(tmp_path)
    cfg["node"]["free_vram_gb"] = 4
    claims = []
    result = jobs_queue.process_next_job(
        cfg, pathlib.Path("node.json"), claim_job=lambda *_: claims.append("claim") or True,
        retire_job=lambda *_: None, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=lambda *_: (0, ""),
    )
    assert result is None and claims == []
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "pending"
    logs = [p.read_text(encoding="utf-8") for p in all_run_files(tmp_path) if p.suffix == ".log"]
    assert any("free VRAM 4.00 GB is below 6.00 GB" in text for text in logs)


def test_fixture_matches_combat_layout_and_glossary_is_a_level(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Exact field contract observed in all five Hub/_jobs files on 2026-08-27.
    assert set(fixture) == {
        "job_id", "type", "created_at", "created_by", "reason", "requires", "priority",
        "target", "params", "status", "blocked_by", "notes", "runner", "updated_at", "updated_note",
    }
    assert fixture["params"]["glossary"] == "project"
    # Combat job now runs: "project" is a level, and the level's meaning is to pass
    # no flag at all - the engine resolves the project glossary itself.
    path, calls, summary = run(tmp_path, fixture, hints_confirmed=True)
    assert summary["status"] == "done"
    assert "--glossary" not in calls[0]
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "done"


def test_unknown_top_level_combat_fields_are_ignored(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["params"].pop("glossary")
    path, calls, summary = run(tmp_path, fixture)
    assert calls and summary["status"] == "done"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["runner"].endswith(".ps1") and saved["updated_note"]


def test_unknown_type_is_skipped_and_blocked_is_never_claimed(tmp_path):
    unknown = base_job(job_id="unknown", type="arbitrary-script")
    blocked = base_job(job_id="blocked", status="blocked")
    prepare(tmp_path, unknown)
    blocked_path = prepare(tmp_path, blocked)
    claims = []
    result = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"),
        claim_job=lambda *_: claims.append("claim") or True,
        retire_job=lambda *_: None, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=lambda *_: (0, ""),
    )
    assert result is None and claims == []
    assert json.loads(blocked_path.read_text(encoding="utf-8"))["status"] == "blocked"
    logs = [p.read_text(encoding="utf-8") for p in all_run_files(tmp_path) if p.suffix == ".log"]
    assert any("unsupported job type" in text for text in logs)


def test_variant_is_passed_via_utf8_payload_without_adding_cli_flag(tmp_path):
    job = base_job()
    path = prepare(tmp_path, job)
    observed = {}

    def execute(command, log_path, tail_lines):
        assert "--asr-variant-id" not in command
        config = pathlib.Path(command[command.index("--config") + 1])
        observed.update(json.loads(config.read_text(encoding="utf-8")))
        assert not config.read_bytes().startswith(b"\xef\xbb\xbf")
        out = pathlib.Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "variant-transcript.md").write_text("ok", encoding="utf-8")
        return 0, ""

    jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"), claim_job=watcher.try_claim_file,
        retire_job=watcher.retire_claim, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=execute,
    )
    assert observed["asr_variant_id"] == "medium-700o27"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "done"


def test_failed_run_has_tail_and_shared_summary(tmp_path):
    path, calls, summary = run(tmp_path, base_job(), executor=lambda: (17, "fatal line\nlast detail"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed" and summary["return_code"] == 17
    assert "last detail" in summary["tail_output"]
    assert "last detail" in pathlib.Path(saved["result"]["run_log"]).read_text(encoding="utf-8")


def test_preflight_is_logged_before_first_chunk_and_files_have_no_bom(tmp_path):
    path, calls, summary = run(tmp_path, base_job())
    log_path = pathlib.Path(summary["log_path"])
    text = log_path.read_text(encoding="utf-8")
    assert text.index("faster_whisper") < text.index("FIRST_CHUNK")
    assert "ctranslate2" in text and "cached/medium" in text and "free_vram_gb" in text
    for artifact in [path, *all_run_files(tmp_path)]:
        assert not artifact.read_bytes().startswith(b"\xef\xbb\xbf"), artifact


def test_priority_then_created_at_and_ready_alias(tmp_path):
    newer = base_job(job_id="newer", created_at="2026-08-22T00:00:00Z", status="ready")
    older = base_job(job_id="older", created_at="2026-08-20T00:00:00Z", status="ready")
    prepare(tmp_path, newer)
    prepare(tmp_path, older)
    called = []

    def execute(command, log_path, tail_lines):
        called.append(command)
        out = pathlib.Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "priority-transcript.md").write_text("ok", encoding="utf-8")
        return 0, ""

    jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"), claim_job=watcher.try_claim_file,
        retire_job=watcher.retire_claim, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=execute,
    )
    assert json.loads((tmp_path / "_jobs" / "older.json").read_text(encoding="utf-8"))["status"] == "done"
    assert json.loads((tmp_path / "_jobs" / "newer.json").read_text(encoding="utf-8"))["status"] == "ready"


def test_zero_exit_without_outputs_is_not_false_done(tmp_path):
    job = base_job(job_id="zero-without-output")
    path = prepare(tmp_path, job)
    summary = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"), claim_job=watcher.try_claim_file,
        retire_job=watcher.retire_claim, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=lambda *_: (0, "worker said success"),
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed" and saved["status"] == "failed"
    assert "produced no output" in summary["tail_output"]


def test_empty_queue_does_nothing_and_watcher_continues_to_inbox(tmp_path, monkeypatch):
    (tmp_path / "_jobs").mkdir()
    source = tmp_path / "inbox"
    source.mkdir()
    cfg = {"hub_root": str(tmp_path), "sources": [{"root": str(source)}],
           "write_session_index": False, "status_page": False}
    calls = []
    monkeypatch.setattr(watcher, "acquire_watcher_lock", lambda: tmp_path / "lock")
    monkeypatch.setattr(watcher, "release_watcher_lock", lambda lock: None)
    monkeypatch.setattr(watcher, "find_audio_files", lambda cfg: calls.append("inbox-scan") or [])
    monkeypatch.setattr(watcher, "load_mapper", lambda cfg: {})
    monkeypatch.setattr(watcher, "apply_bundle_metadata", lambda files, cfg, mapper: files)
    watcher.run_once(cfg, tmp_path / "node.json")
    assert calls == ["inbox-scan"]


def test_rotation_deletes_old_logs_but_keeps_json(tmp_path):
    root = tmp_path / "_runs" / "node" / "2026-01-01"
    old_log = root / "old.log"
    old_json = root / "old.json"
    fresh_log = root / "fresh.log"
    for path in (old_log, old_json, fresh_log):
        write_json(path, {}) if path.suffix == ".json" else (path.parent.mkdir(parents=True, exist_ok=True), path.write_text("x", encoding="utf-8"))
    old = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    os.utime(old_log, (old, old))
    os.utime(old_json, (old, old))
    now = dt.datetime(2026, 8, 27, tzinfo=dt.timezone.utc)
    os.utime(fresh_log, (now.timestamp(), now.timestamp()))
    removed = jobs_queue.rotate_run_logs({"hub_root": str(tmp_path), "run_log_retention_days": 90}, now)
    assert old_log in removed and not old_log.exists()
    assert old_json.exists() and fresh_log.exists()


# --- находки ревью 2026-08-27 --------------------------------------------------

def test_repeated_skip_is_journalled_once_per_reason(tmp_path):
    """F1: неподходящее задание не плодит сводку на каждом свипе."""
    job = base_job(params={"quality_preset": "medium", "no_such_param": 1})
    prepare(tmp_path, job)
    for _ in range(3):
        jobs_queue.process_next_job(
            cfg_for(tmp_path), pathlib.Path("node.json"),
            claim_job=watcher.try_claim_file, retire_job=watcher.retire_claim,
            heartbeat_factory=FakeHeartbeat, preflight=environment,
            cli_executor=lambda *a: (0, ""),
        )
    summaries = [p for p in all_run_files(tmp_path) if p.suffix == ".json"]
    assert len(summaries) == 1, summaries
    recorded = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert recorded["status"] == "skipped"
    assert "no_such_param" in recorded["reason"]

    # причина сменилась — появляется вторая запись, старая остаётся
    write_json(tmp_path / "_jobs" / f"{job['job_id']}.json",
               base_job(type="no-such-type", params={"quality_preset": "medium"}))
    jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"),
        claim_job=watcher.try_claim_file, retire_job=watcher.retire_claim,
        heartbeat_factory=FakeHeartbeat, preflight=environment,
        cli_executor=lambda *a: (0, ""),
    )
    assert len([p for p in all_run_files(tmp_path) if p.suffix == ".json"]) == 2


def test_queue_failure_does_not_stop_the_inbox_sweep(tmp_path, monkeypatch):
    """F2: падение очереди стоит задания, а не свипа."""
    (tmp_path / "_jobs").mkdir()
    source = tmp_path / "inbox"
    source.mkdir()
    cfg = {"hub_root": str(tmp_path), "sources": [{"root": str(source)}],
           "write_session_index": False, "status_page": False}
    calls = []
    monkeypatch.setattr(watcher, "acquire_watcher_lock", lambda: tmp_path / "lock")
    monkeypatch.setattr(watcher, "release_watcher_lock", lambda lock: None)
    monkeypatch.setattr(watcher, "load_mapper", lambda cfg: {})
    monkeypatch.setattr(watcher, "apply_bundle_metadata", lambda files, cfg, mapper: files)
    monkeypatch.setattr(watcher, "find_audio_files",
                        lambda cfg: calls.append("inbox-scan") or [])

    def explode(*args, **kwargs):
        raise OSError("hub went away mid-sweep")

    monkeypatch.setattr(jobs_queue, "process_next_job", explode)
    watcher.run_once(cfg, tmp_path / "node.json")
    assert calls == ["inbox-scan"]


def test_orphaned_running_job_is_reclaimed_after_lease_expires(tmp_path):
    """F3: задание умершего узла возвращается в работу, а не виснет навсегда."""
    job = base_job(status="running", claimed_by="DEAD-NODE",
                   claimed_at="2026-08-21T13:40:00Z")
    path = prepare(tmp_path, job)
    calls = []

    def fake_executor(command, log_path, tail_lines):
        calls.append(command)
        out = pathlib.Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "result-transcript.md").write_text("ok", encoding="utf-8")
        return 0, "tail"

    common = dict(claim_job=watcher.try_claim_file, retire_job=watcher.retire_claim,
                  heartbeat_factory=FakeHeartbeat, preflight=environment,
                  cli_executor=fake_executor)

    # живой владелец — не трогаем
    summary = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"),
        claim_is_stale=lambda p, cfg: False, **common)
    assert summary is None and calls == []
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "running"

    # лизинг истёк — задание подбирается и доводится
    summary = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"),
        claim_is_stale=lambda p, cfg: True, **common)
    assert summary and summary["status"] == "done"
    assert len(calls) == 1
    finished = json.loads(path.read_text(encoding="utf-8"))
    assert finished["status"] == "done" and "reclaimed_from" not in finished
    log_text = chr(10).join(p.read_text(encoding="utf-8") for p in all_run_files(tmp_path)
                            if p.suffix == ".log")
    assert "reclaimed orphaned job" in log_text


def test_output_base_name_is_normalised(tmp_path):
    """F4: описательный asr_variant_id не уходит в имя файла как есть."""
    job = base_job(params={"quality_preset": "medium", "speaker_mode": "diarize",
                           "asr_variant_id": "auto ({base}-gloss-{sha8} from feat/project-glossary)"})
    _, calls, summary = run(tmp_path, job)
    assert summary["status"] == "done"
    name = calls[0][calls[0].index("--output-base-name") + 1]
    assert name == "auto-base--gloss--sha8-from-feat-project-glossary"
    assert " " not in name and "(" not in name and "/" not in name


def test_journal_timestamps_are_utc(tmp_path):
    """F5: время в .log и в сводке — одно и то же, в UTC."""
    run(tmp_path, base_job())
    logs = [p for p in all_run_files(tmp_path) if p.suffix == ".log"]
    stamp = logs[0].read_text(encoding="utf-8").splitlines()[0].split(" ")[0]
    assert stamp.endswith("Z"), stamp
    parsed = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert abs((now - parsed).total_seconds()) < 300


def test_revariant_lands_beside_session_transcripts(tmp_path):
    """F6: перегонка кладёт продукт в transcripts-hints, как временный раннер."""
    _, calls, summary = run(tmp_path, base_job())
    out = pathlib.Path(calls[0][calls[0].index("--output-dir") + 1])
    expected = (tmp_path / "700" / "sessions" / "2026-08"
                / "S20260810T2110-platforma" / "transcripts-hints")
    assert out == expected, out
    assert summary["output_paths"] and all(
        "transcripts-hints" in path for path in summary["output_paths"])


def test_unknown_shaped_job_type_still_has_a_home(tmp_path):
    """Тип из словаря без своей папки не теряет выход."""
    out = jobs_queue._output_dir({"type": "rediarize", "target": {
        "project_id": "700", "session_id": "S20260810T2110-platforma"}},
        {"hub_root": str(tmp_path)})
    assert out.name == "transcripts-rediarized"


# --- уровни глоссария и гейт подтверждения (восстановление skipped) ------------

def test_glossary_project_level_passes_no_flag(tmp_path):
    job = base_job(params={"quality_preset": "medium", "glossary": "project"})
    _, calls, summary = run(tmp_path, job, hints_confirmed=True)
    assert summary["status"] == "done"
    assert "--glossary" not in calls[0]


def test_glossary_off_is_passed_through(tmp_path):
    job = base_job(params={"quality_preset": "medium", "glossary": "off"})
    _, calls, summary = run(tmp_path, job)
    assert summary["status"] == "done"
    assert calls[0][calls[0].index("--glossary") + 1] == "off"


def test_glossary_path_is_passed_as_path(tmp_path):
    custom = str(tmp_path / "custom-glossary.json")
    job = base_job(params={"quality_preset": "medium", "glossary": custom})
    _, calls, summary = run(tmp_path, job, hints_confirmed=True)
    assert calls[0][calls[0].index("--glossary") + 1] == custom


def test_requested_hints_without_confirmation_is_not_scored(tmp_path):
    """Прогон без подтверждения подсказок — обычное перераспознавание, не вариант."""
    job = base_job(params={"quality_preset": "medium", "glossary": "project"})
    path, calls, summary = run(tmp_path, job, hints_confirmed=False)
    assert summary["status"] == "failed"
    assert "never reported" in summary["tail_output"]
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"
    assert summary["output_paths"], "продукт остаётся на месте — его просто не зачли"


def test_job_without_glossary_is_not_gated(tmp_path):
    """Задание, которое подсказок не просило, гейт не трогает."""
    job = base_job(params={"quality_preset": "medium", "speaker_mode": "diarize"})
    _, _, summary = run(tmp_path, job, hints_confirmed=False)
    assert summary["status"] == "done"


def test_off_is_not_gated_either(tmp_path):
    job = base_job(params={"quality_preset": "medium", "glossary": "off"})
    _, _, summary = run(tmp_path, job, hints_confirmed=False)
    assert summary["status"] == "done"



# --- веса прогона: params.model (org/preset-measure-2026-08-22-invalid) ---------

def run_with_meta(hub: pathlib.Path, job: dict, meta_model: str, *, capabilities=("gpu-cuda",)):
    """Прогон, в котором движок записывает в run-meta заданную модель."""
    prepare(hub, job)
    calls = []
    seen = {}

    def execute(command, log_path, tail_lines):
        calls.append(command)
        jobs_queue._append_log(log_path, "worker", "FIRST_CHUNK")
        out = pathlib.Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "result-transcript.md").write_text("ok", encoding="utf-8")
        write_json(out / "result-run-meta.json", {"model": meta_model, "quality_preset": "large-v3"})
        return 0, ""

    def preflight(python, preset, min_vram):
        seen["preset"] = preset
        return environment(python, preset)

    summary = jobs_queue.process_next_job(
        cfg_for(hub, capabilities), pathlib.Path("node.json"),
        claim_job=watcher.try_claim_file, retire_job=watcher.retire_claim,
        heartbeat_factory=FakeHeartbeat, preflight=preflight, cli_executor=execute,
    )
    return calls, summary, seen


def test_model_param_reaches_the_cli_because_preset_never_picks_weights(tmp_path):
    """Пресет — только ярлык: без --model движок берёт дефолт medium."""
    job = base_job(params={"model": "large-v3", "quality_preset": "large-v3",
                           "speaker_mode": "diarize"})
    calls, summary, _ = run_with_meta(tmp_path, job, "large-v3")
    command = calls[0]
    assert "--model" in command
    assert command[command.index("--model") + 1] == "large-v3"
    assert summary["status"] == "done"
    assert summary["model_requested"] == "large-v3" and summary["model_used"] == "large-v3"


def test_job_without_model_passes_no_flag_and_keeps_the_hints_folder(tmp_path):
    """Прежние задания (пять боевых) ведут себя ровно как раньше."""
    _, calls, summary = run(tmp_path, base_job())
    assert "--model" not in calls[0]
    out = pathlib.Path(calls[0][calls[0].index("--output-dir") + 1])
    assert out.name == "transcripts-hints"
    assert summary["status"] == "done"


def test_model_run_lands_in_its_own_folder_beside_the_baseline(tmp_path):
    """Перегонка на других весах сравнивается с medium — значит лежит отдельно."""
    job = base_job(params={"model": "large-v3", "quality_preset": "large-v3"})
    calls, _, _ = run_with_meta(tmp_path, job, "large-v3")
    out = pathlib.Path(calls[0][calls[0].index("--output-dir") + 1])
    assert out == (tmp_path / "700" / "sessions" / "2026-08"
                   / "S20260810T2110-platforma" / "transcripts-large-v3"), out


def test_preflight_checks_the_requested_weights_not_the_label(tmp_path):
    """Иначе наличие medium зачитывает готовность к large-v3."""
    # Ярлык, который не называет модель, — барьер согласованности молчит, веса свои.
    job = base_job(params={"model": "large-v3", "quality_preset": "fast"})
    _, _, seen = run_with_meta(tmp_path, job, "large-v3")
    assert seen["preset"] == "large-v3"


def test_silent_fallback_to_other_weights_is_not_scored_as_done(tmp_path):
    """22.08: прогон назывался large-v3, run-meta показал medium — это не тот вариант."""
    job = base_job(params={"model": "large-v3", "quality_preset": "large-v3"})
    _, summary, _ = run_with_meta(tmp_path, job, "medium")
    assert summary["status"] == "failed"
    assert "large-v3" in summary["tail_output"] and "medium" in summary["tail_output"]
    assert summary["model_used"] == "medium"


# --- находки ревью 28.08 -------------------------------------------------------

def test_stale_run_meta_from_an_earlier_run_is_not_evidence(tmp_path):
    """F1: в боевой папке варианта лежит run-meta от 22.08 с model=medium."""
    job = base_job(params={"model": "large-v3", "quality_preset": "large-v3"})
    prepare(tmp_path, job)
    out = (tmp_path / "700" / "sessions" / "2026-08" / "S20260810T2110-platforma"
           / "transcripts-large-v3" / "asr")
    out.mkdir(parents=True)
    stale = write_json(out / "large-v3-700o27-run-meta.json", {"model": "medium"})
    old = time.time() - 6 * 24 * 3600
    os.utime(stale, (old, old))

    def execute(command, log_path, tail_lines):
        jobs_queue._append_log(log_path, "worker", "FIRST_CHUNK")
        target = pathlib.Path(command[command.index("--output-dir") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "fresh-transcript.md").write_text("ok", encoding="utf-8")
        write_json(target / "fresh-run-meta.json", {"model": "large-v3"})
        return 0, ""

    summary = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"), claim_job=watcher.try_claim_file,
        retire_job=watcher.retire_claim, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=execute,
    )
    assert summary["status"] == "done", summary["tail_output"]
    assert summary["model_used"] == "large-v3"


def test_run_without_any_run_meta_is_not_scored_when_weights_were_asked(tmp_path):
    """F4: нет свидетельства — нет зачёта, как у гейта подсказок."""
    job = base_job(params={"model": "large-v3", "quality_preset": "large-v3"})
    prepare(tmp_path, job)

    def execute(command, log_path, tail_lines):
        jobs_queue._append_log(log_path, "worker", "FIRST_CHUNK")
        out = pathlib.Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "only-transcript.md").write_text("ok", encoding="utf-8")
        return 0, ""

    summary = jobs_queue.process_next_job(
        cfg_for(tmp_path), pathlib.Path("node.json"), claim_job=watcher.try_claim_file,
        retire_job=watcher.retire_claim, heartbeat_factory=FakeHeartbeat,
        preflight=environment, cli_executor=execute,
    )
    assert summary["status"] == "failed"
    assert "unverifiable" in summary["tail_output"]


def test_preset_naming_weights_without_model_is_refused_not_run_quietly(tmp_path):
    """F5: барьер, который до params.model держался случайно — на preflight."""
    job = base_job(params={"quality_preset": "large-v3", "speaker_mode": "diarize"})
    reason = jobs_queue._eligibility(job, cfg_for(tmp_path))
    assert reason and "does not select weights" in reason


def test_preset_equal_to_the_node_default_stays_runnable(tmp_path):
    """Пять боевых заданий несут quality_preset: medium — они обязаны идти как шли."""
    assert jobs_queue._eligibility(base_job(), cfg_for(tmp_path)) is None


def test_model_as_a_path_is_refused_before_the_gpu_is_taken(tmp_path):
    """F9: путь в весах даёт папку transcripts-C-models-… и вечный mismatch."""
    job = base_job(params={"model": "C:/models/large-v3", "quality_preset": "large-v3"})
    reason = jobs_queue._eligibility(job, cfg_for(tmp_path))
    assert reason and "not a path" in reason


def test_weights_plus_hints_names_both_instead_of_claiming_one_home(tmp_path):
    """F10: договорённость F6 расширена, а не переписана молча."""
    out = jobs_queue._output_dir(
        {"type": "asr-revariant", "params": {"model": "large-v3", "glossary": "project"},
         "target": {"project_id": "700", "session_id": "S20260810T2110-platforma"}},
        {"hub_root": str(tmp_path)})
    assert out.name == "transcripts-large-v3-hints"


def test_summary_keeps_preset_a_label_and_reports_weights_separately(tmp_path):
    """F6: смысл существующего поля не меняется задним числом."""
    job = base_job(params={"model": "large-v3", "quality_preset": "fast"})
    _, summary, _ = run_with_meta(tmp_path, job, "large-v3")
    assert summary["preset"] == "fast"
    assert summary["weights"] == "large-v3"
