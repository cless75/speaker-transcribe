import json

import pytest

from hub_nas_intake import HubNasIntake
from pipeline_queue import PipelineQueue
from pipeline_runtime import HoldJob, StageRunner
from pipeline_storage import ReceiptStore
from test_hub_worker import MemoryDrive
from test_pipeline_runtime import handlers, nas_area


def adapter(tmp_path, drive, area):
    return HubNasIntake(drive, area, projects=["Example"], node_config={},
                        legacy_worker_root=tmp_path / "legacy",
                        scan_cache=tmp_path / "scan-cache", profile_digest="a" * 64,
                        engine_digest="b" * 64)


def test_real_adapter_copies_to_nas_before_ready_without_drive_writes(tmp_path):
    queued = json.dumps({"status": "queued"}).encode()
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic", "Example/voice.m4a.state.json": queued,
                         "Other/ignored.m4a": b"not admitted"})
    original = dict(drive.files)
    nas = nas_area(tmp_path)
    intake = adapter(tmp_path, drive, nas)
    store = ReceiptStore(tmp_path / "state")
    hs = handlers()
    hs["discovered"] = intake.stage
    r = StageRunner(tmp_path / "queue.db", store, gpu_lock=tmp_path / "gpu.lock", handlers=hs, owner="test")
    try:
        assert intake.discover(r.queue) == {"candidates": 1, "held": 0}
        key = r.queue.db.execute("SELECT id FROM pipeline_jobs").fetchone()[0]
        assert r.tick("discovered") == "finished"
        receipt = store.read(r.queue.get(key), "discovered")
        evidence = receipt["metadata"]["nas_source"]
        manifest = nas.verify_source(evidence, lambda: None)
        assert manifest["binding"]["drive_id"].startswith("id-")
        source_dir = nas.root / evidence["relative"]
        assert (source_dir / "voice.m4a").read_bytes() == b"synthetic"
        assert (source_dir / "READY").stat().st_size == 0
        assert drive.files == original  # No claims, terminal state or status-page writes.
        assert intake.discover(r.queue)["candidates"] == 1
        assert r.queue.get(key)["state"] == "ready"
        generated = source_dir / "voice.m4a.asr/version"
        generated.mkdir(parents=True)
        (generated / "transcript.txt").write_text("synthetic")
        assert nas.verify_source(evidence, lambda: None) == manifest
    finally:
        r.close()


def test_terminal_history_is_not_queued_for_asr(tmp_path):
    done = json.dumps({"status": "asr-done", "publication_verified": True}).encode()
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic", "Example/voice.m4a.state.json": done})
    intake = adapter(tmp_path, drive, nas_area(tmp_path))
    q = PipelineQueue(tmp_path / "queue.db")
    try:
        assert intake.discover(q) == {"candidates": 0, "held": 0}
        assert q.claim("discovered", "test") is None
    finally:
        q.close()


def test_source_state_changed_after_discovery_holds(tmp_path):
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic",
                         "Example/voice.m4a.state.json": json.dumps({"status": "queued"}).encode()})
    intake = adapter(tmp_path, drive, nas_area(tmp_path))
    q = PipelineQueue(tmp_path / "queue.db")
    try:
        intake.discover(q)
        job = q.claim("discovered", "test")
        drive.files["Example/voice.m4a.state.json"] = json.dumps({"status": "in-progress"}).encode()
        with pytest.raises(HoldJob, match="state_changed"):
            intake.stage(job, tmp_path / "job", lambda: None)
        assert not (tmp_path / "job/input").exists()
    finally:
        q.close()


def test_source_checksum_mismatch_never_publishes_ready(tmp_path):
    class CorruptDrive(MemoryDrive):
        def call(self, *args):
            out = super().call(*args)
            if args[0] == "copyto" and args[1].startswith("gdrive:"):
                from pathlib import Path
                Path(args[2]).write_bytes(b"corrupted")
            return out
    drive = CorruptDrive({"Example/voice.m4a": b"synthetic",
                          "Example/voice.m4a.state.json": json.dumps({"status": "queued"}).encode()})
    area = nas_area(tmp_path)
    intake = adapter(tmp_path, drive, area)
    q = PipelineQueue(tmp_path / "queue.db")
    try:
        intake.discover(q)
        job = q.claim("discovered", "test")
        with pytest.raises(HoldJob, match="checksum"):
            intake.stage(job, tmp_path / "job", lambda: None)
        assert not list(area.root.rglob("READY"))
    finally:
        q.close()


def test_shared_inbox_cannot_implicitly_inherit_project_routing(tmp_path):
    with pytest.raises(ValueError, match="explicit_project"):
        HubNasIntake(None, None, projects=["_inbox"], node_config={},
                     legacy_worker_root=tmp_path, scan_cache=tmp_path,
                      profile_digest="a" * 64, engine_digest="b" * 64)


@pytest.mark.parametrize("policy", [{"Unknown": "hub-only"}, {"Example": "no"},
                                    {"Example": None}, [], False])
def test_invalid_project_delivery_rejected(tmp_path, policy):
    with pytest.raises(ValueError, match="invalid_project_delivery"):
        HubNasIntake(None, None, projects=["Example"], node_config={},
                     legacy_worker_root=tmp_path, scan_cache=tmp_path,
                     profile_digest="a" * 64, engine_digest="b" * 64,
                     project_delivery=policy)


@pytest.mark.parametrize("policy", [None, [], {}, False, "unknown"])
def test_invalid_frozen_policy_rejected_before_io(tmp_path, policy):
    from pipeline_storage import EvidenceError
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic"})
    intake = adapter(tmp_path, drive, nas_area(tmp_path))
    q = PipelineQueue(tmp_path / "queue.db")
    try:
        key = intake.admit_exact(q, "Example", "voice.m4a")
        job = q.get(key)
        payload = json.loads(job["payload"])
        payload["delivery_policy"] = policy
        job["payload"] = json.dumps(payload)
        with pytest.raises(EvidenceError, match="invalid_delivery_policy"):
            intake.stage(job, tmp_path / "job", lambda: None)
    finally:
        q.close()


def test_policy_changes_processing_identity_and_cannot_relabel_legacy(tmp_path):
    from pipeline_storage import EvidenceError
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic"})
    intake = adapter(tmp_path, drive, nas_area(tmp_path))
    q = PipelineQueue(tmp_path / "queue.db")
    try:
        dual = intake.admit_exact(q, "Example", "voice.m4a")
        intake.project_delivery["Example"] = "hub-only"
        only = intake.admit_exact(q, "Example", "voice.m4a")
        assert dual != only
        intake._source(q.get(dual))  # old frozen jobs still validate
        intake._source(q.get(only))
        job = q.get(dual)
        payload = json.loads(job["payload"])
        payload["delivery_policy"] = "hub-only"
        job["payload"] = json.dumps(payload)
        with pytest.raises(EvidenceError, match="processing_identity"):
            intake._source(job)
    finally:
        q.close()
