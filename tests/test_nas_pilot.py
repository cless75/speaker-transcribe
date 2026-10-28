import copy
import hashlib
import json

import pytest

import audio_inbox_watch as watcher
from nas_pilot import (Admission, NasPipeline, POLICY, make_area, pilot_profile,
                       prepare_area, preflight)
from pipeline_runtime import HoldJob, StageRunner
from pipeline_storage import EvidenceError, ReceiptStore, atomic_json, digest
from test_hub_worker import MemoryDrive


def package(tmp_path):
    corpus = tmp_path / "mount/corpus"
    source = corpus / "sources/recording.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic source")
    p = corpus / "preparation/0.0.1"
    p.mkdir(parents=True)
    entry = {"project": 501, "source_id": "501-synthetic-pilot", "source_path": "sources/recording.mp4",
             "source_unc": "//test/share/corpus/sources/recording.mp4", "source_sha256": digest(source),
             "bytes": source.stat().st_size, "duration_sec": 60,
             "admission": "admitted", "admission_scope": "single-initial-pilot-only",
             "speaker_policy": POLICY, "timeline": {"basis": "native-source-media", "trim": False,
             "speed_change": False, "offset_applied_sec": 0, "external_transcript_bindings": []}}
    atomic_json(p / "whitelist.json", {"project": 501, "recursive_scan_allowed": False,
                "include": [entry], "exclude": [{"path": "sources/not-admitted.mp4"}],
                "speaker_policy": POLICY, "transcripts_admitted": [], "offsets_applied": []})
    atomic_json(p / "speaker-policy.json", {"policy": POLICY, "enrollment_admitted": False})
    atomic_json(p / "source-validation.json", {"source": entry["source_path"],
                "source_sha256": entry["source_sha256"], "source_matches_archive_hash": True,
                "full_audio_video_decode_exit_code": 0, "status": "passed-technical-source-validation"})
    manifest = {"version": "0.0.1", "project": 501, "files": [
        {"path": f.name, "sha256": digest(f), "bytes": f.stat().st_size}
        for f in sorted(p.glob("*.json"))]}
    atomic_json(p / "manifest.json", manifest)
    check = tmp_path / "completion-check.json"
    atomic_json(check, {"source_id": entry["source_id"], "source_sha256": entry["source_sha256"],
                        "completed_asr_found": False})
    cfg = {"project": 501, "corpus_root": str(corpus), "mountpoint": str(tmp_path / "mount"),
           "mount_source": "//test/share", "package_relative": "preparation/0.0.1",
           "manifest_sha256": digest(p / "manifest.json"), "maximum_job": 20 * 1024**3,
           "state_root": str(tmp_path / "state"), "local_minimum_free": 0, "nas_minimum_free": 0,
           "completion_check_path": str(check), "completion_check_sha256": digest(check)}
    return cfg, source, p


def fixture(tmp_path):
    cfg, source, path = package(tmp_path)
    admission = Admission(cfg, mount_check=lambda *a: None)
    area = make_area(admission, 0)
    prepare_area(area)
    store = ReceiptStore(tmp_path / "state")
    drive = MemoryDrive({})
    calls = []

    def engine(audio, output, node, config_path, pid):
        calls.append(str(audio))
        assert node["voiceprint_mode"] == "off" and node["glossary_override"] == "off"
        assert node["zoom_vtt_autodetect"] is False and node["ktalk_sidecar_patterns"] == []
        output.mkdir(parents=True, exist_ok=True)
        text = output / "recording-transcript.md"
        text.write_text("[00:00] SPEAKER_00: synthetic")
        meta_path = output / "recording-run-meta.json"
        meta = {"status": "ok", "voiceprint": {"mode": "off", "enabled": False},
                "profile_updates": [], "diarization": {"source": "pyannote", "status": "ok"},
                "outputs": {"md": str(text), "run_meta": str(meta_path)}}
        atomic_json(meta_path, meta)
        return text, meta

    pipeline = NasPipeline(admission, store, area, drive,
                node_config={"hub_root": "/mnt/hub", "voiceprint_mode": "match_enroll"},
                config_path=tmp_path / "node.json", profile_digest="a" * 64,
                engine_digest="b" * 64, executor=engine, probe=lambda p: 60, gpu_check=lambda: None)
    runner = StageRunner(tmp_path / "queue.db", store, gpu_lock=tmp_path / "gpu.lock",
                owner="test", handlers={"discovered": pipeline.stage, "ready": pipeline.asr,
                "publish_pending": pipeline.delivery}, retry_seconds=.001)
    return admission, pipeline, store, drive, runner, calls


def test_admission_is_read_only_and_only_one_source(tmp_path):
    cfg, source, p = package(tmp_path)
    before = {f.relative_to(source.parent.parent).as_posix(): digest(f)
              for f in source.parent.parent.rglob("*") if f.is_file()}
    admission = Admission(cfg, mount_check=lambda *a: None)
    assert admission.report()["include_count"] == 1
    assert admission.report()["input_ack"] == "acknowledged"
    after = {f.relative_to(source.parent.parent).as_posix(): digest(f)
             for f in source.parent.parent.rglob("*") if f.is_file()}
    assert before == after
    assert not source.with_suffix(".mp4.asr").exists()


@pytest.mark.parametrize("part", ["manifest.json", "whitelist.json", "speaker-policy.json", "source-validation.json"])
def test_package_mutation_rejected_before_admission(tmp_path, part):
    cfg, source, p = package(tmp_path)
    (p / part).write_text("{}")
    with pytest.raises(EvidenceError, match="changed"):
        Admission(cfg, mount_check=lambda *a: None)


def test_source_changed_after_package_is_held(tmp_path):
    cfg, source, p = package(tmp_path)
    admission = Admission(cfg, mount_check=lambda *a: None)
    source.write_bytes(b"changed")
    with pytest.raises(EvidenceError, match="admitted_source_changed"):
        admission.verify()


def test_real_stages_deliver_both_and_rediscovery_never_repeats(tmp_path):
    a, pipeline, store, drive, runner, calls = fixture(tmp_path)
    try:
        report = pipeline.discover(runner.queue)
        key = report["job_id"]
        pipeline.discover(runner.queue)
        assert runner.queue.db.execute("SELECT count(*) FROM pipeline_jobs").fetchone()[0] == 1
        assert runner.tick("discovered") == "finished"
        assert runner.tick("ready") == "finished"
        assert not (pipeline.area.root / "source-state.json").exists()
        assert runner.tick("publish_pending") == "finished"
        state = json.loads((pipeline.area.root / "source-state.json").read_bytes())
        assert state["deliveries_verified"] == ["hub", "nas"]
        assert state["speaker_policy"] == POLICY
        assert state["review"] == "needs_review"
        assert pipeline.discover(runner.queue) == {"candidates": 0, "reused_completed": 1, "job_id": key}
        assert runner.tick("ready") == "idle" and len(calls) == 1
        assert pipeline.delivery.verify_completed(runner.queue.get(key), store.read(runner.queue.get(key), "publish_pending"), lambda: None)
        assert a.source.read_bytes() == b"synthetic source"
    finally:
        runner.close()


def test_nas_outage_never_marks_source_done(tmp_path):
    a, pipeline, store, drive, runner, calls = fixture(tmp_path)
    try:
        key = pipeline.discover(runner.queue)["job_id"]
        assert runner.tick("discovered") == runner.tick("ready") == "finished"
        pipeline.area.mount_check = lambda *a: (_ for _ in ()).throw(OSError("offline"))
        assert runner.tick("publish_pending") == "retry_wait"
        assert not (pipeline.area.root / "source-state.json").exists()
        pipeline.area.mount_check = lambda *a: None
        import time
        time.sleep(.01)
        assert runner.tick("publish_pending") == "finished"
        assert len(calls) == 1
    finally:
        runner.close()


def test_foreign_persistent_claim_is_never_stolen(tmp_path):
    a, pipeline, store, drive, runner, calls = fixture(tmp_path)
    atomic_json(pipeline.area.root / "source-claim.json", {"token": "foreign"})
    try:
        pipeline.discover(runner.queue)
        assert runner.tick("discovered") == "held"
        assert not calls
        assert json.loads((pipeline.area.root / "source-claim.json").read_bytes()) == {"token": "foreign"}
    finally:
        runner.close()


def test_other_processing_identity_cannot_repeat_same_source(tmp_path):
    a, pipeline, store, drive, runner, calls = fixture(tmp_path)
    try:
        pipeline.discover(runner.queue)
        pipeline.engine_digest = "c" * 64
        with pytest.raises(HoldJob, match="other_processing_identity"):
            pipeline.discover(runner.queue)
        assert runner.queue.db.execute("SELECT count(*) FROM pipeline_jobs").fetchone()[0] == 1
    finally:
        runner.close()


def test_unknown_worker_attempt_requires_reconciliation_not_retry(tmp_path):
    a, pipeline, store, drive, runner, calls = fixture(tmp_path)
    try:
        key = pipeline.discover(runner.queue)["job_id"]
        assert runner.tick("discovered") == "finished"
        atomic_json(store.directory(key) / "asr-process.json", {"finished": True, "returncode": 1})
        assert runner.tick("ready") == "held"
        assert not calls
    finally:
        runner.close()


@pytest.mark.parametrize("mode,enabled,source", [("match", True, "pyannote"),
                                               ("off", False, "zoom_vtt"),
                                               ("off", False, "ktalk_txt")])
def test_unapproved_engine_policy_cannot_be_receipted(tmp_path, mode, enabled, source):
    a, pipeline, store, drive, runner, calls = fixture(tmp_path)
    original = pipeline.executor
    def bad(*args):
        text, metadata = original(*args)
        metadata["voiceprint"] = {"mode": mode, "enabled": enabled}
        metadata["diarization"]["source"] = source
        return text, metadata
    pipeline.executor = bad
    try:
        key = pipeline.discover(runner.queue)["job_id"]
        assert runner.tick("discovered") == "finished"
        assert runner.tick("ready") == "held"
        assert not store.path(key, "ready").exists()
        assert not (store.directory(key) / "engine-completion.json").exists()
        assert not (pipeline.area.root / "source-state.json").exists()
    finally:
        runner.close()


def test_preflight_refuses_completed_source_without_writing_or_asr(tmp_path):
    cfg, source, p = package(tmp_path)
    cfg["maximum_job"] = 100
    admission = Admission(cfg, mount_check=lambda *a: None)
    area = make_area(admission, 0)
    prepare_area(area)
    atomic_json(area.root / "source-state.json", {"status": "asr-done"})
    with pytest.raises(HoldJob, match="requires_reuse"):
        preflight(cfg, mount_check=lambda *a: None)
    assert not (tmp_path / "state").exists()


def test_worker_off_survives_cli_defaults_and_sidecar_discovery(tmp_path, monkeypatch):
    import media_transcribe_cli as cli
    import media_transcribe as worker
    cfg = pilot_profile({"voiceprint_mode": "match_enroll", "zoom_vtt_path": "old.vtt",
                         "speaker_map_path": "names.json", "glossary_path": "proposed.json"})
    config = tmp_path / "node.json"
    atomic_json(config, cfg)
    media = tmp_path / "recording.mp4"
    media.write_bytes(b"synthetic")
    (tmp_path / "recording.vtt").write_text("WEBVTT")
    (tmp_path / "Транскрипция recording.txt").write_text("named")
    monkeypatch.setattr(cli, "_resolve_default_runtime", lambda: {})
    import sys
    monkeypatch.setattr(sys, "argv", ["cli", "--config", str(config), "--input", str(media),
                                     "--output-dir", str(tmp_path / "output"), "--project-id", "501"])
    # The parser is defined inside main; intercept payload handoff without spawning a model.
    saved = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: saved.append(json.loads(kw["input"])) or
                        type("Result", (), {"returncode": 0})())
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    assert saved
    payload = worker.normalize_payload(saved[0])
    assert payload["voiceprint_mode"] == "off" and payload["generate_speaker_clips"] is False
    assert not payload.get("zoom_vtt_path") and not payload.get("speaker_map_path")
    assert payload["glossary_override"] == "off"
    assert watcher.zoom_vtt_for(media, cfg) is None
    assert watcher.ktalk_sidecar_for(media, cfg) is None
