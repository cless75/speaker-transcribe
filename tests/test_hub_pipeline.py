import json
import sys
from types import SimpleNamespace

import pytest

import hub_pipeline as app
from pipeline_storage import EvidenceError, atomic_json


def config(tmp_path):
    node = tmp_path / "node.json"
    node.write_text(json.dumps({"transcribe_python": sys.executable, "hub_root": str(tmp_path / "Hub"),
                               "quality_preset": "medium", "speaker_mode": "diarize",
                               "voiceprint_mode": "match_enroll", "cleanup_intermediates": True,
                               "runtime": {"device": "cuda"},
                               "node": {"host_label": "test"}}))
    return {"schema": 1, "enabled": False, "node_config": str(node),
            "state_root": str(tmp_path / "state"), "legacy_worker_root": str(tmp_path / "legacy"),
            "remote": "gdrive:Hub", "gpu_lock": str(tmp_path / "gpu.lock"),
            "source_paths": {"Example": ["recording.webm"]},
            "e2e_source": {"project": "Example", "path": "recording.webm"},
            "maximum_job": 100, "buffer_capacity": 500, "local_minimum_free": 10, "nas_minimum_free": 10,
            "nas": {"root": str(tmp_path / "mount/nas"), "mountpoint": str(tmp_path / "mount"),
                    "source": "//test/share", "collection_id": "test", "marker": ".identity"}}


def test_prepare_preserves_profile_and_refuses_silent_drift(tmp_path):
    cfg = config(tmp_path)
    root = app.prepare(cfg)
    snapshot = json.loads((root / "node-snapshot.json").read_bytes())
    assert snapshot["voiceprint_mode"] == "match_enroll"
    assert snapshot["speaker_mode"] == "diarize"
    assert snapshot["cleanup_intermediates"] is False
    assert snapshot["on_asr_done"] == "leave"
    app.prepare(cfg)
    node = json.loads((tmp_path / "node.json").read_bytes())
    node["quality_preset"] = "changed"
    atomic_json(tmp_path / "node.json", node)
    with pytest.raises(EvidenceError, match="drift"):
        app.prepare(cfg)


def test_serving_gate_pins_identity_not_enable_switch(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    app.prepare(cfg)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=b"test-packages"))
    pilot = app.Pilot(cfg)
    identity = pilot.identity()
    cfg["enabled"] = True
    assert pilot.identity() == identity
    atomic_json(pilot.root / "release-attestation.json", {"identity": identity, "job_id": "test"})
    with pytest.raises(EvidenceError, match="missing_or_unfinished"):
        pilot.require_attestation()
    cfg["source_paths"]["Example"].append("not-tested.webm")
    with pytest.raises(EvidenceError, match="repeat_e2e"):
        pilot.require_attestation()


def test_live_serving_requires_old_timer_disabled_not_merely_stopped(monkeypatch):
    monkeypatch.setattr(app, "os", SimpleNamespace(name="posix"))
    def systemctl(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="enabled\n" if "is-enabled" in args else "inactive\n")
    monkeypatch.setattr(app.subprocess, "run", systemctl)
    app.old_intake_stopped(serving=False)
    with pytest.raises(RuntimeError, match="not_disabled"):
        app.old_intake_stopped(serving=True)


@pytest.mark.parametrize("target", [None, {}, {"project": "Example"},
                                    {"project": "Example", "path": None}])
def test_candidate_without_new_e2e_source_fails_closed(target):
    pilot = app.Pilot.__new__(app.Pilot)
    pilot.config = {"e2e_source": target}
    with pytest.raises(EvidenceError, match="fresh_e2e_source_required"):
        pilot.admit_e2e(None)


def test_e2e_refuses_completed_source_before_enqueue_or_asr(tmp_path):
    from test_hub_worker import MemoryDrive
    pilot = app.Pilot.__new__(app.Pilot)
    pilot.config = {"e2e_source": {"project": "Example", "path": "voice.m4a"}}
    drive = MemoryDrive({"Example/voice.m4a": b"synthetic",
                         "Example/voice.m4a.state.json": json.dumps({"status": "asr-done"}).encode()})
    pilot.intake = SimpleNamespace(drive=drive)
    # No admit_exact/pipeline/queue objects exist: no side effects are permitted.
    with pytest.raises(EvidenceError, match="completed_source_forbidden"):
        pilot.admit_e2e(None)
