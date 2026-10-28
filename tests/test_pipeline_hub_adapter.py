import json

from hub_nas_intake import HubNasIntake
from pipeline_hub_adapter import HubPipeline
from pipeline_hub_claims import HubClaims
from pipeline_runtime import StageRunner
from pipeline_storage import ReceiptStore
from test_hub_worker import MemoryDrive, fake_asr
from test_pipeline_runtime import nas_area


def fixture(tmp_path):
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic",
                         "Example/voice.m4a.state.json": json.dumps({"status": "queued"}).encode(),
                         "Example/not-authorized.m4a": b"other"})
    area = nas_area(tmp_path)
    store = ReceiptStore(tmp_path / "state")
    intake = HubNasIntake(drive, area, projects=["Example"], node_config={},
                          legacy_worker_root=tmp_path / "legacy", scan_cache=tmp_path / "scan",
                          profile_digest="a" * 64, engine_digest="b" * 64,
                          source_paths={"Example": ["voice.m4a"]})
    claims = HubClaims(drive, tmp_path / "claims", host="test-PIPELINE", convergence_seconds=0)
    calls = []
    fake = fake_asr(calls)
    def executor(audio, output, cfg, config_path, pid):
        path, meta = fake(audio, output, cfg, config_path, pid)
        meta["outputs"] = {"md": str(path), "run_meta": str(next(output.glob("*-run-meta.json")))}
        return path, meta
    pipeline = HubPipeline(intake, store, claims, node_config={"hub_root": "/mnt/hub"},
                           config_path=tmp_path / "node.json", maximum_job=20 * 1024**3,
                           executor=executor, probe=lambda path: 60, gpu_check=lambda: None)
    runner = StageRunner(tmp_path / "queue.db", store, gpu_lock=tmp_path / "gpu.lock", owner="test",
                         handlers={"discovered": pipeline.stage, "ready": pipeline.asr,
                                   "publish_pending": pipeline.delivery}, retry_seconds=.001)
    return drive, store, intake, claims, pipeline, runner, calls


def test_concrete_hub_nas_stages_and_source_marker_last(tmp_path):
    drive, store, intake, claims, pipeline, runner, calls = fixture(tmp_path)
    try:
        assert intake.discover(runner.queue)["candidates"] == 1
        key = runner.queue.db.execute("SELECT id FROM pipeline_jobs").fetchone()[0]
        assert runner.tick("discovered") == "finished"
        assert drive.state("Example", "voice.m4a")["status"] == "in-progress"
        assert runner.tick("ready") == "finished"
        # ASR must not publish or mutate the receipted input directory.
        assert drive.state("Example", "voice.m4a")["status"] == "in-progress"
        assert store.read(runner.queue.get(key), "discovered")
        assert runner.tick("publish_pending") == "finished"
        state = drive.state("Example", "voice.m4a")
        assert state["status"] == "asr-done"
        assert state["deliveries_verified"] == ["hub", "nas"]
        assert state["pipeline_job_id"] == key
        assert len(calls) == 1 and not claims.active
        assert runner.queue.get(key)["state"] == "done"
        assert "Example/not-authorized.m4a.state.json" not in drive.files
        assert list(intake.nas.root.rglob("quality.json"))
    finally:
        runner.close()
        claims.close()


def test_nas_outage_after_asr_never_marks_hub_source_done(tmp_path):
    import time
    drive, store, intake, claims, pipeline, runner, calls = fixture(tmp_path)
    try:
        intake.discover(runner.queue)
        key = runner.queue.db.execute("SELECT id FROM pipeline_jobs").fetchone()[0]
        assert runner.tick("discovered") == runner.tick("ready") == "finished"
        mount = intake.nas.mount_check
        intake.nas.mount_check = lambda *args: (_ for _ in ()).throw(OSError("NAS unavailable"))
        assert runner.tick("publish_pending") == "retry_wait"
        assert drive.state("Example", "voice.m4a")["status"] == "awaiting-publication"
        intake.nas.mount_check = mount
        time.sleep(.01)
        assert runner.tick("publish_pending") == "finished"
        assert len(calls) == 1
        assert runner.queue.get(key)["state"] == "done"
    finally:
        runner.close()
        claims.close()


def test_durable_engine_completion_resumes_snapshot_without_repeating_model(tmp_path):
    import time
    drive, store, intake, claims, pipeline, runner, calls = fixture(tmp_path)
    try:
        intake.discover(runner.queue)
        key = runner.queue.db.execute("SELECT id FROM pipeline_jobs").fetchone()[0]
        assert runner.tick("discovered") == "finished"
        snapshot = pipeline.snapshot_result
        pipeline.snapshot_result = lambda *a: (_ for _ in ()).throw(OSError("crash after engine receipt"))
        assert runner.tick("ready") == "retry_wait"
        assert len(calls) == 1
        assert (store.directory(key) / "engine-completion.json").exists()
        pipeline.snapshot_result = snapshot
        pipeline.executor = lambda *a: (_ for _ in ()).throw(AssertionError("must not repeat ASR"))
        time.sleep(.01)
        assert runner.tick("ready") == "finished"
        assert len(calls) == 1
    finally:
        runner.close()
        claims.close()


def test_hub_transient_failure_still_delivers_nas_without_rerunning_asr(tmp_path):
    import time
    from hub_intake import DriveTransient
    drive, store, intake, claims, pipeline, runner, calls = fixture(tmp_path)
    original = drive.call
    try:
        intake.discover(runner.queue)
        key = runner.queue.db.execute("SELECT id FROM pipeline_jobs").fetchone()[0]
        assert runner.tick("discovered") == runner.tick("ready") == "finished"
        def outage(*args):
            if args[0] == "copy":
                raise DriveTransient("temporary")
            return original(*args)
        drive.call = outage
        assert runner.tick("publish_pending") == "retry_wait"
        assert (store.directory(key) / "delivery-nas.json").exists()
        assert drive.state("Example", "voice.m4a")["status"] != "asr-done"
        drive.call = original
        time.sleep(.01)
        assert runner.tick("publish_pending") == "finished"
        assert len(calls) == 1
    finally:
        runner.close()
        claims.close()
