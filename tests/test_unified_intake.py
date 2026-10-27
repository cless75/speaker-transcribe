"""Single scheduler: migrate journals, alternate sources, retain release gate."""

import contextlib
import datetime as dt
import json
import pathlib

import pytest

import unified_intake as unified
from test_hub_worker import MemoryDrive, fake_asr, media
from test_zoom_local_worker import fixture as zoom_fixture, fake_asr as zoom_fake_asr


def config(tmp_path):
    zc, _, _ = zoom_fixture(tmp_path)
    zpath = tmp_path / "zoom-config.json"
    zpath.write_text(json.dumps(zc))
    hc = {"hub_root": str(tmp_path / "Hub"), "enable_multi_machine": True,
          "zoom_vtt_only_input": True}
    hpath = tmp_path / "hub-config.json"
    hpath.write_text(json.dumps(hc))
    root = tmp_path / "worker"
    return {"hub_config": str(hpath), "zoom_config": str(zpath),
            "hub_worker_root": str(root), "queue_path": str(root / "unified.sqlite3"),
            "remote": "gdrive:Hub", "deployed_at": "2026-10-21T10:00:00+00:00"}


def test_queue_is_idempotent_and_alternates_attempts(tmp_path):
    q = unified.Queue(tmp_path / "state" / "queue.sqlite3")
    try:
        q.upsert("drive:a", "drive", "415", "audio.m4a", "a")
        q.upsert("zoom:b", "zoom", "415", "uuid-hash", "b")
        q.upsert("drive:c", "drive", "700", "next.m4a", "c")
        candidates = {"drive:a": ("drive",), "zoom:b": ("zoom",),
                      "drive:c": ("drive",)}
        assert unified.schedule(q, candidates, 3) == ["drive:a", "zoom:b", "drive:c"]
        q.update("drive:a", "retry", attempt=True,
                 retry_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat())
        assert "drive:a" not in unified.schedule(q, candidates, 3)
        q.upsert("drive:a", "drive", "415", "audio.m4a", "a")
        assert q.get("drive:a")["attempts"] == 1  # rediscovery does not erase backoff
        q.upsert("drive:a", "drive", "415", "audio.m4a", "a", done=True)
        assert q.get("drive:a")["status"] == "done"
        q.upsert("drive:a", "drive", "415", "audio.m4a", "a")
        assert q.get("drive:a")["status"] == "done"
    finally:
        q.close()


def test_migration_keeps_verified_old_journals_and_does_not_repeat_asr(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(unified.zoom, "require_dispatch_inactive", lambda _: None)
    hc, zc, _ = unified.configs(cfg)
    drive = MemoryDrive({"415/voice.m4a": b"audio"})
    item = media(drive, project="415", path="voice.m4a")
    calls = []
    unified.hub.process_one(drive, item, pathlib.Path(cfg["hub_worker_root"]), hc,
                            pathlib.Path(cfg["hub_config"]), transcribe=fake_asr(calls))
    zoom_item = unified.zoom.select(zc)[0]
    unified.zoom.process_one(zc, zoom_item, drive, transcribe=zoom_fake_asr(calls))
    q = unified.Queue(pathlib.Path(cfg["queue_path"]))
    try:
        assert unified.migrate(cfg, q, drive, zc)["done"] == 2
        assert unified.migrate(cfg, q, drive, zc)["done"] == 2
        assert q.counts() == {"done": 2}
        assert len(calls) == 2
    finally:
        q.close()


def test_release_identity_pins_code_and_node_config(tmp_path):
    cfg = config(tmp_path)
    baseline = unified.identity(cfg)
    path = pathlib.Path(cfg["hub_config"])
    path.write_text(json.dumps({"hub_root": "changed"}))
    assert unified.identity(cfg) != baseline


def test_pilot_exclusion_is_exact_and_pinned_in_release_identity(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    baseline = unified.identity(cfg)
    cfg["excluded_hub_projects"] = ["Example"]
    assert unified.identity(cfg) != baseline
    hc, zc, _ = unified.configs(cfg)
    monkeypatch.setattr(unified.zoom, "select", lambda c: [])
    scanned = []
    def pending(drive, project, *args, **kwargs):
        scanned.append(project)
        return [], []
    monkeypatch.setattr(unified.hub, "pending_inputs", pending)
    unified.discover(cfg, hc, zc, MemoryDrive({"Example/a.m4a": b"a", "Example-other/b.m4a": b"b"}))
    assert scanned == ["Example-other"]


@pytest.mark.parametrize("excluded", ["Example", ["*"], ["../Example"], [None]])
def test_pilot_exclusion_refuses_ambiguous_scope(tmp_path, excluded):
    cfg = config(tmp_path)
    cfg["excluded_hub_projects"] = excluded
    hc, zc, _ = unified.configs(cfg)
    with pytest.raises(ValueError, match="explicit list"):
        unified.discover(cfg, hc, zc, MemoryDrive({}))


def test_release_preflight_reports_drift_without_config_values(tmp_path):
    cfg = config(tmp_path)
    root = pathlib.Path(cfg["hub_worker_root"])
    root.mkdir()
    assert unified.release_status(cfg)["reason"] == "missing_or_invalid_attestation"
    (root / "release-attestation.json").write_text(json.dumps({"identity": unified.identity(cfg)}))
    assert unified.release_status(cfg)["status"] == "ok"
    pathlib.Path(cfg["hub_config"]).write_text(json.dumps({"hub_root": "private-new-path"}))
    status = unified.release_status(cfg)
    assert status["changed"] == ["config.hub_config"]
    assert "private-new-path" not in json.dumps(status)


def test_health_records_blocked_run_without_erasing_progress(tmp_path):
    cfg = config(tmp_path)
    unified.record_health(cfg, report={"processed": 1})
    path = pathlib.Path(cfg["hub_worker_root"]) / "health" / "intake.json"
    before = json.loads(path.read_text())
    unified.record_health(cfg)
    unified.record_health(cfg, error=RuntimeError("private source name"))
    after = json.loads(path.read_text())
    assert after["status"] == "blocked"
    assert after["last_processed_at"] == before["last_processed_at"]
    assert after["last_scan_finished_at"] == before["last_scan_finished_at"]
    assert "private source name" not in path.read_text()


@pytest.mark.parametrize("error,reason", [
    (unified.DriveRateLimited("quota"), "drive-quota"),
    (unified.DriveTransient("timeout"), "drive-transient"),
    (ValueError("bad marker"), "scan-error"),
])
def test_discovery_distinguishes_incomplete_from_quarantine(tmp_path, monkeypatch, error, reason):
    cfg = config(tmp_path)
    hc, zc, _ = unified.configs(cfg)
    monkeypatch.setattr(unified.zoom, "select", lambda c: [])
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(unified.hub, "pending_inputs", fail)
    candidates, report = unified.discover(cfg, hc, zc, MemoryDrive({"415/voice.m4a": b"a"}))
    assert candidates == {}
    assert report["hub_incomplete"] == 1
    assert report["hub_quarantined"] == 0
    assert report["hub_incomplete_reasons"][reason] == 1


def test_release_attestation_requires_fresh_zoom_proof_and_pins_identity(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    hc, zc, drive = unified.configs(cfg)
    root = pathlib.Path(cfg["hub_worker_root"])
    root.mkdir()
    (root / "media-e2e.json").write_text(json.dumps({
        "source_id": "source-1", "source_revision": "proof-1",
        "verified_at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    calls = []
    monkeypatch.setattr(unified.hub, "require_media_e2e", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(unified, "verify_zoom_proof", lambda d, r, after=None: [])
    with pytest.raises(RuntimeError, match="no verified Zoom"):
        unified.attest(cfg, drive, hc, zc)
    monkeypatch.setattr(unified, "verify_zoom_proof", lambda d, r, after=None: ["415/session"])
    receipt = unified.attest(cfg, drive, hc, zc)
    assert receipt["zoom_proof"] == "415/session"
    assert calls[0]["max_age"] == dt.timedelta(hours=24)
    unified.require_attestation(cfg, drive, hc, zc)
    assert calls[-1]["max_age"] is None  # verified release has no daily expiry
    pathlib.Path(cfg["zoom_config"]).write_text("{}")
    with pytest.raises(RuntimeError, match="identity changed"):
        unified.require_attestation(cfg, drive, hc, zc)


def test_scheduler_retries_failure_then_runs_other_source(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(unified, "old_consumers_inactive", lambda: None)
    monkeypatch.setattr(unified, "require_attestation", lambda *a: None)
    monkeypatch.setattr(unified.hub, "single_worker", lambda _: contextlib.nullcontext())
    monkeypatch.setattr(unified, "discover", lambda c, hc, zc, d, q: (
        {"drive:a": ("drive", "item", []), "zoom:b": ("zoom", {"uuid": "uuid-1"})},
        {"hub_incomplete": 0, "hub_quarantined": 0, "zoom_ready": 1}))
    monkeypatch.setattr(unified.hub, "run_claimed", lambda *a: (_ for _ in ()).throw(RuntimeError("asr fail")))
    monkeypatch.setattr(unified.hub, "defer_failed_job", lambda *a: None)
    monkeypatch.setattr(unified.zoom, "process_one", lambda *a: {"phase": "done"})
    # The fake discovery must also seed the persistent queue.
    original_discover = unified.discover
    def seeded(c, hc, zc, d, q):
        candidates, report = original_discover(c, hc, zc, d, q)
        q.upsert("drive:a", "drive", "415", "audio.m4a", "revision")
        q.upsert("zoom:b", "zoom", "415", "uuid-hash", "revision")
        return candidates, report
    monkeypatch.setattr(unified, "discover", seeded)
    report = unified.run(cfg, dry_run=False, max_files=2)
    assert report["attempted"] == 2 and report["processed"] == 1 and report["failed"] == 1
    q = unified.Queue(pathlib.Path(cfg["queue_path"]))
    try:
        assert q.get("drive:a")["status"] == "retry"
        assert q.get("zoom:b")["status"] == "done"
    finally:
        q.close()
