import hashlib
import json
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from pipeline_budget import DiskBudget, DiskPressure
from pipeline_delivery import DualDelivery, NasArea, verify_mount
from pipeline_queue import LeaseLost, PipelineQueue
from pipeline_runtime import RetryJob, StageRunner
from pipeline_storage import EvidenceError, ReceiptStore, ResourceBusy, atomic_json, file_lock, digest


def enqueue(queue, name="one"):
    return queue.enqueue(f"drive:{name}", "revision", "profile-digest", "engine-digest",
                         {"source": name})


def handlers():
    def stage(job, directory, check):
        (directory / "input").mkdir(parents=True, exist_ok=True)
        (directory / "input/audio.wav").write_bytes(b"synthetic-audio")
        return {"source_verified": True}

    def asr(job, directory, check):
        (directory / "result").mkdir(parents=True, exist_ok=True)
        (directory / "result/transcript.txt").write_text("synthetic text")
        return {"quality": "degraded", "review": "needs_review"}

    def publish(job, directory, check):
        return {"deliveries": {"hub": {"verified": True}, "nas": {"verified": True}}}

    return dict(zip(("discovered", "ready", "publish_pending"), (stage, asr, publish)))


def setup(tmp_path, **kwargs):
    db = tmp_path / "queue.db"
    store = ReceiptStore(tmp_path / "state")
    runner = StageRunner(db, store, gpu_lock=tmp_path / "shared-gpu.lock",
                         handlers=kwargs.pop("handlers", handlers()), owner="test", **kwargs)
    return runner, store, enqueue(runner.queue)


def test_full_local_path_and_receipt_identity(tmp_path):
    runner, store, key = setup(tmp_path)
    try:
        for stage in ("discovered", "ready", "publish_pending"):
            assert runner.tick(stage) == "finished"
        assert runner.queue.get(key)["state"] == "done"
        assert store.read(runner.queue.get(key), "ready")["metadata"]["review"] == "needs_review"
        saved = json.loads(store.path(key, "ready").read_bytes())
        saved["binding"]["identity"] = "forged"
        atomic_json(store.path(key, "ready"), saved)
        with pytest.raises(EvidenceError, match="identity"):
            store.read(runner.queue.get(key), "ready")
    finally:
        runner.close()


def test_existing_receipt_skips_repeated_work_after_exception(tmp_path):
    runner, store, key = setup(tmp_path)
    original = runner.queue.finish
    def crash(*args):
        raise OSError("simulated crash after receipt")
    runner.queue.finish = crash
    runner.retry_seconds = 0.001
    assert runner.tick("discovered") == "retry_wait"
    runner.queue.finish = original
    runner.handlers["discovered"] = lambda *a: pytest.fail("must not restage")
    time.sleep(.01)
    assert runner.tick("discovered") == "finished"
    assert runner.queue.get(key)["state"] == "ready"
    runner.close()


def test_corrupt_or_extra_artifact_holds_without_asr(tmp_path):
    runner, store, key = setup(tmp_path)
    assert runner.tick("discovered") == "finished"
    (store.directory(key) / "input/extra.txt").write_text("not inventoried")
    runner.handlers["ready"] = lambda *args: pytest.fail("bad upstream must be checked BEFORE ASR")
    # The upstream receipt is verified again before accepting ASR results.
    assert runner.tick("ready") == "held"
    assert runner.queue.get(key)["state"] == "held"
    runner.close()


def test_shared_gpu_lock_blocks_claim_without_new_lease(tmp_path):
    runner, store, key = setup(tmp_path)
    assert runner.tick("discovered") == "finished"
    with file_lock(tmp_path / "shared-gpu.lock"):
        assert runner.tick("ready") == "resource_busy"
        assert runner.queue.get(key)["state"] == "ready"
    assert runner.tick("ready") == "finished"
    runner.close()


def test_reconcile_expired_asr_only_after_proving_gpu_free(tmp_path):
    now = [100.0]
    runner, store, key = setup(tmp_path, clock=lambda: now[0], gpu_recovery_check=lambda job: True)
    assert runner.tick("discovered") == "finished"
    job = runner.queue.claim("ready", "dead-worker", lease_seconds=1)
    runner.handlers["ready"](job, store.directory(key), lambda: None)
    store.write(job, "ready", {"quality": "ok"})
    now[0] = 102
    with file_lock(tmp_path / "shared-gpu.lock"):
        with pytest.raises(ResourceBusy):
            runner.reconcile(key)
    assert runner.reconcile(key) == "reconciled"
    assert runner.queue.get(key)["state"] == "publish_pending"
    with pytest.raises(LeaseLost):
        runner.queue.finish(key, job["token"])
    runner.close()


def test_missing_evidence_never_reclaims_expired_job(tmp_path):
    now = [100.0]
    runner, store, key = setup(tmp_path, clock=lambda: now[0])
    runner.queue.claim("discovered", "dead-worker", lease_seconds=1)
    now[0] = 102
    with pytest.raises(ValueError, match="evidence"):
        runner.reconcile(key)
    assert runner.tick("discovered") == "idle"
    assert runner.queue.get(key)["state"] == "staging"
    runner.close()


def test_heartbeat_survives_slow_handler(tmp_path):
    hs = handlers()
    original = hs["discovered"]
    def slow(*args):
        time.sleep(.8)
        return original(*args)
    hs["discovered"] = slow
    runner, store, key = setup(tmp_path, handlers=hs, lease_seconds=.3)
    assert runner.tick("discovered") == "finished"
    runner.close()


def test_asr_runs_while_scanner_and_publisher_blocked(tmp_path):
    db = tmp_path / "queue.db"
    store = ReceiptStore(tmp_path / "state")
    q = PipelineQueue(db)
    first, second = enqueue(q, "first"), enqueue(q, "second")
    release, scan_started, publish_started, second_asr = [threading.Event() for _ in range(4)]
    hs = handlers()
    original_stage, original_asr, original_pub = hs.values()
    def staging(job, directory, check):
        if job["id"] == second:
            scan_started.set()
            assert release.wait(5)
        return original_stage(job, directory, check)
    def transcribing(job, directory, check):
        result = original_asr(job, directory, check)
        if job["id"] == second:
            second_asr.set()
        return result
    def publishing(*args):
        publish_started.set()
        assert release.wait(5)
        return original_pub(*args)
    hs = dict(zip(hs, (staging, transcribing, publishing)))

    def tick(stage):
        runner = StageRunner(db, store, gpu_lock=tmp_path / "gpu.lock", handlers=hs, owner=stage)
        try:
            return runner.tick(stage)
        finally:
            runner.close()

    assert tick("discovered") == "finished"
    assert tick("ready") == "finished"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            scanning = pool.submit(tick, "discovered")
            publishing_future = pool.submit(tick, "publish_pending")
            assert scan_started.wait(2) and publish_started.wait(2)
            # Scanner is copying job 2; a third already-ready job must still run.
            third = enqueue(q, "third")
            assert tick("discovered") == "finished"
            assert tick("ready") == "finished"
            assert q.get(third)["state"] == "publish_pending"
            assert not publishing_future.done() and not scanning.done()
            release.set()
            assert scanning.result(3) == publishing_future.result(3) == "finished"
    finally:
        release.set()
        q.close()


def test_idle_consumer_sees_new_work_without_scan_completion(tmp_path):
    db = tmp_path / "queue.db"
    store = ReceiptStore(tmp_path / "state")
    q = PipelineQueue(db)
    stop, started = threading.Event(), threading.Event()
    def consume():
        r = StageRunner(db, store, gpu_lock=tmp_path / "gpu.lock", handlers=handlers(), owner="idle")
        started.set()
        try:
            r.serve("discovered", stop, poll_seconds=.05)
        finally:
            r.close()
    thread = threading.Thread(target=consume)
    thread.start()
    try:
        assert started.wait(2)
        key = enqueue(q)
        limit = time.monotonic() + 3
        while q.get(key)["state"] != "ready" and time.monotonic() < limit:
            time.sleep(.02)
        assert q.get(key)["state"] == "ready"
    finally:
        stop.set()
        thread.join(3)
        q.close()


def test_disk_budget_two_connections_cannot_reserve_same_free_bytes(tmp_path):
    path = tmp_path / "budget.db"
    a = DiskBudget(path, capacity=100, minimum_free=10, volume=tmp_path, free_bytes=lambda: 100)
    b = DiskBudget(path, capacity=100, minimum_free=10, volume=tmp_path, free_bytes=lambda: 100)
    try:
        a.reserve("one", 60)
        with pytest.raises(DiskPressure):
            b.reserve("two", 60)
        b.reserve("one", 60)
        with pytest.raises(ValueError):
            b.reserve("one", 50)
        with pytest.raises(DiskPressure):
            a.check("one", 61)
        with pytest.raises(ValueError, match="config_changed"):
            DiskBudget(path, capacity=1000, minimum_free=10, volume=tmp_path)
    finally:
        a.close()
        b.close()


def test_budget_pressure_never_calls_stager(tmp_path):
    budget = DiskBudget(tmp_path / "budget.db", capacity=100, minimum_free=10,
                        volume=tmp_path, free_bytes=lambda: 20)
    runner, store, key = setup(tmp_path, budget=budget, reserve_bytes=lambda _: 50)
    runner.handlers["discovered"] = lambda *args: pytest.fail("no space")
    assert runner.tick("discovered") == "retry_wait"
    assert not (store.directory(key) / "input").exists()
    runner.close()
    budget.close()


def test_per_job_budget_is_enforced_before_reservation(tmp_path):
    budget = DiskBudget(tmp_path / "budget.db", capacity=100, minimum_free=10,
                        maximum_job=20, volume=tmp_path, free_bytes=lambda: 1000)
    try:
        with pytest.raises(ValueError, match="per_job_budget"):
            budget.reserve("too-large", 21)
        assert budget.db.execute("SELECT count(*) FROM disk_reservations").fetchone()[0] == 0
        budget.reserve("allowed", 20)
    finally:
        budget.close()


class Sink:
    def __init__(self):
        self.calls, self.verifications, self.fail = 0, 0, False
    def publish(self, job, result_dir, check):
        self.calls += 1
        if self.fail:
            raise RetryJob("offline")
        return {"destination": "synthetic", "revision": job["id"]}
    def verify(self, job, evidence, check):
        self.verifications += 1
        if self.fail:
            raise RetryJob("offline")
        return evidence["revision"] == job["id"]


def test_dual_delivery_failure_never_repeats_asr_or_finalizes_early(tmp_path):
    runner, store, key = setup(tmp_path, retry_seconds=.001)
    hub, nas, finalized = Sink(), Sink(), []
    nas.fail = True
    runner.handlers["publish_pending"] = DualDelivery(
        store, hub=hub, nas=nas, finalize=lambda *args: finalized.append(True))
    assert runner.tick("discovered") == runner.tick("ready") == "finished"
    assert runner.tick("publish_pending") == "retry_wait"
    assert not finalized
    assert hub.calls == 1
    nas.fail = False
    time.sleep(.01)
    runner.handlers["ready"] = lambda *a: pytest.fail("no repeat ASR")
    assert runner.tick("publish_pending") == "finished"
    assert hub.calls == 1 and hub.verifications == 2
    assert nas.calls == 2 and finalized == [True]
    assert runner.queue.get(key)["state"] == "done"
    runner.close()


def nas_area(tmp_path, mount_check=None):
    mount = tmp_path / "mount"
    root = mount / "wd-store"
    root.mkdir(parents=True)
    atomic_json(root / ".identity", {"schema": 1, "collection_id": "test", "mount_source": "//test/share"})
    area = NasArea(root, mountpoint=mount, source="//test/share", collection_id="test",
                   marker=".identity", minimum_free=0,
                   mount_check=mount_check or (lambda *args: None))
    return area


def test_nas_version_readback_and_conflict_without_overwrite(tmp_path):
    area = nas_area(tmp_path)
    local = tmp_path / "result"
    local.mkdir()
    (local / "transcript.txt").write_bytes(b"text")
    files = [{"path": "transcript.txt", "size": 4, "sha256": hashlib.sha256(b"text").hexdigest()}]
    args = ("source.m4a.asr/version", local, files, {"revision": "one"}, lambda: None)
    first = area.publish(*args)
    assert area.publish(*args) == first
    target = area.root / "source.m4a.asr/version/transcript.txt"
    target.write_bytes(b"evil")
    with pytest.raises(EvidenceError, match="readback"):
        area.publish(*args)
    assert target.read_bytes() == b"evil"


def test_missing_mount_never_creates_destination(tmp_path):
    area = nas_area(tmp_path)
    area.mount_check = lambda *args: (_ for _ in ()).throw(OSError("unmounted"))
    with pytest.raises(OSError):
        area.publish("new/version", tmp_path, [{"path": "x", "size": 1, "sha256": "a" * 64}], {}, lambda: None)
    assert not (area.root / "new").exists()


def test_unlisted_nas_file_prevents_completion_receipt(tmp_path):
    area = nas_area(tmp_path)
    local = tmp_path / "result"
    local.mkdir()
    (local / "transcript.txt").write_bytes(b"text")
    target = area.root / "version"
    target.mkdir()
    (target / "unknown.txt").write_text("user content")
    files = [{"path": "transcript.txt", "size": 4, "sha256": hashlib.sha256(b"text").hexdigest()}]
    with pytest.raises(EvidenceError, match="unlisted"):
        area.publish("version", local, files, {}, lambda: None)
    assert not (target / "receipt.json").exists()


def test_unverified_single_delivery_cannot_make_done_receipt(tmp_path):
    runner, store, key = setup(tmp_path)
    assert runner.tick("discovered") == runner.tick("ready") == "finished"
    job = runner.queue.get(key)
    with pytest.raises(EvidenceError, match="both_verified"):
        store.write(job, "publish_pending", {"deliveries": {"hub": {"verified": True}}})
    assert not store.path(key, "publish_pending").exists()
    runner.close()


def test_completed_delivery_receipt_rechecks_destinations_after_crash(tmp_path):
    runner, store, key = setup(tmp_path, retry_seconds=.001)
    hub, nas = Sink(), Sink()
    runner.handlers["publish_pending"] = DualDelivery(store, hub=hub, nas=nas, finalize=lambda *args: None)
    assert runner.tick("discovered") == runner.tick("ready") == "finished"
    finish = runner.queue.finish
    runner.queue.finish = lambda *a: (_ for _ in ()).throw(OSError("crash after delivery receipt"))
    assert runner.tick("publish_pending") == "retry_wait"
    runner.queue.finish = finish
    nas.fail = True
    time.sleep(.01)
    assert runner.tick("publish_pending") == "retry_wait"
    assert runner.queue.get(key)["state"] != "done"
    nas.fail = False
    time.sleep(.01)
    assert runner.tick("publish_pending") == "finished"
    assert hub.calls == nas.calls == 1
    runner.close()


def test_degraded_without_review_cannot_write_asr_receipt(tmp_path):
    runner, store, key = setup(tmp_path)
    assert runner.tick("discovered") == "finished"
    job = runner.queue.claim("ready", "test")
    runner.handlers["ready"](job, store.directory(key), lambda: None)
    with pytest.raises(EvidenceError, match="requires_review"):
        store.write(job, "ready", {"quality": "degraded"})
    assert not store.path(key, "ready").exists()
    runner.close()


def test_orphaned_asr_blocks_other_ready_jobs_even_if_lock_is_free(tmp_path):
    now = [100.0]
    runner, store, first = setup(tmp_path, clock=lambda: now[0])
    assert runner.tick("discovered") == "finished"
    now[0] += .1
    second = enqueue(runner.queue, "second")
    assert runner.tick("discovered") == "finished"
    abandoned = runner.queue.claim("ready", "lost-parent", lease_seconds=1)
    assert runner.tick("ready") == "recovery_required"
    now[0] = 102
    assert runner.tick("ready") == "recovery_required"
    assert runner.queue.get(second)["state"] == "ready"
    runner.handlers["ready"](abandoned, store.directory(first), lambda: None)
    store.write(abandoned, "ready", {"quality": "ok"})
    with pytest.raises(EvidenceError, match="process_release_proof"):
        runner.reconcile(first)
    runner.gpu_recovery_check = lambda job: True  # synthetic deployment proof
    assert runner.reconcile(first) == "reconciled"
    assert runner.tick("ready") == "finished"
    runner.close()


def test_mount_guard_filters_autofs_but_requires_exact_cifs_identity(tmp_path, monkeypatch):
    import pipeline_delivery as delivery
    from types import SimpleNamespace
    monkeypatch.setattr(delivery, "os", SimpleNamespace(
        name="posix", path=SimpleNamespace(ismount=lambda _: True)))
    class Result:
        stdout = json.dumps({"filesystems": [{"target": str(tmp_path),
                                               "source": "//test/share", "fstype": "cifs"}]}).encode()
    def findmnt(args, **kwargs):
        assert args[args.index("--types") + 1] == "cifs"
        return Result()
    monkeypatch.setattr(delivery.subprocess, "run", findmnt)
    verify_mount(tmp_path, "//test/share")
    with pytest.raises(OSError, match="identity_mismatch"):
        verify_mount(tmp_path, "//different/share")


def test_owned_nas_hash_does_not_depend_on_cifs_timestamp_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace
    path = tmp_path / "nas-copy"
    path.write_bytes(b"synthetic")
    original = pathlib.Path.stat
    calls = [0]
    def stat_for_test(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if self != path:
            return value
        calls[0] += 1
        return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                               st_ino=value.st_ino, st_mtime_ns=calls[0], st_ctime_ns=calls[0])
    monkeypatch.setattr(pathlib.Path, "stat", stat_for_test)
    with pytest.raises(EvidenceError, match="artifact_changed"):
        digest(path)  # local input snapshot checks remain strict
    assert digest(path, strict_snapshot=False) == hashlib.sha256(b"synthetic").hexdigest()
