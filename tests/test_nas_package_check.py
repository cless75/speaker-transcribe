import copy
import json

import pytest

import nas_pilot as app
from pipeline_storage import EvidenceError, atomic_json, digest
from test_nas_pilot import package


def resign(cfg, p):
    manifest = json.loads((p / "manifest.json").read_bytes())
    manifest["files"] = [{"path": f.name, "bytes": f.stat().st_size, "sha256": digest(f)}
                         for f in sorted(p.iterdir()) if f.name not in {"manifest.json", "manifest.sha256"}]
    atomic_json(p / "manifest.json", manifest)
    cfg["manifest_sha256"] = digest(p / "manifest.json")


def multi_package(tmp_path):
    cfg, first, old = package(tmp_path)
    p = old.rename(old.parent / "0.0.2")
    cfg["package_relative"] = "preparation/0.0.2"
    w = json.loads((p / "whitelist.json").read_bytes())
    w["version"] = "0.0.2"
    w["include"][0]["execution_admission"] = "held-awaiting-801-ack-and-runtime-gates"
    second = first.with_name("second.mp4")
    second.write_bytes(b"second synthetic source")
    e = copy.deepcopy(w["include"][0])
    e.update(source_id="501-second", source_path="sources/second.mp4", source_sha256=digest(second),
             bytes=second.stat().st_size, admission_scope="501-source-prepared-no-vtt-unknown-no-enrollment",
             execution_admission="held-awaiting-shared-pilot-acceptance-updated-801-ack-and-runtime-gates")
    w["include"].append(e)
    atomic_json(p / "whitelist.json", w)
    validation = json.loads((p / "source-validation.json").read_bytes())
    (p / "source-validation.json").rename(p / "source-validation-initial.json")
    validation.update(source=e["source_path"], source_sha256=e["source_sha256"])
    atomic_json(p / "source-validation-philipp.json", validation)
    atomic_json(p / "mentoring-full-coverage.json", {"method": "synthetic fixture"})
    (p / "README.md").write_text("immutable fixture")
    m = json.loads((p / "manifest.json").read_bytes())
    m["version"] = "0.0.2"
    atomic_json(p / "manifest.json", m)
    resign(cfg, p)
    return cfg, first, p


def completed(first, entry, manifest):
    area = first.with_name(first.name + ".asr")
    key = "d" * 64
    d = area / key
    d.mkdir(parents=True)
    output = d / "transcript.md"
    output.write_text("original output")
    state = {"status": "asr-done", "project": 501, "source_id": entry["source_id"],
             "source_sha256": entry["source_sha256"], "pipeline_job_id": key,
             "manifest_sha256": manifest, "deliveries_verified": ["hub", "nas"],
             "speaker_policy": app.POLICY}
    atomic_json(area / "source-state.json", state)
    atomic_json(d / "receipt.json", {"schema": 1, "binding": {
        "identity": json.dumps(["nas:501:" + entry["source_id"], entry["source_sha256"], "a" * 64, "b" * 64]),
        "job_id": key, "source_sha256": entry["source_sha256"], "manifest_sha256": manifest},
        "files": [{"path": output.name, "size": output.stat().st_size, "sha256": digest(output)}]})
    return area, key


def tree(root):
    return {str(p.relative_to(root)): digest(p) for p in root.rglob("*") if p.is_file()}


def test_legacy_compatibility_stays_read_only(tmp_path):
    cfg, source, p = package(tmp_path)
    before = tree(tmp_path)
    report = app.check_package(cfg, mount_check=lambda *a: None)
    assert report["compatibility"] == "accepted"
    assert report["sources"][0]["disposition"] == "execution_held"
    assert not report["runtime_ready"] and not report["execution_started"]
    assert tree(tmp_path) == before


def test_multi_package_completed_reuse_precedes_new_profile_or_engine(tmp_path, monkeypatch):
    cfg, source, p = multi_package(tmp_path)
    entry = json.loads((p / "whitelist.json").read_bytes())["include"][0]
    area, key = completed(source, entry, "e" * 64)
    cfg.update(engine="new-engine", profile="new-profile", enabled=True)
    def forbidden(*a, **k):
        pytest.fail("compatibility must not construct a queue, claim, worker, or runtime area")
    for name in ["PipelineQueue", "SourceClaim", "NasPipeline", "prepare_area", "run_pilot", "preflight"]:
        monkeypatch.setattr(app, name, forbidden)
    before = tree(tmp_path)
    for _ in range(2):
        report = app.check_package(cfg, mount_check=lambda *a: None)
        assert report["members_verified"] == 6
        first, second = report["sources"]
        assert first["disposition"] == "reuse_completed"
        assert first["completed_run_ref"]["job_id"] == key
        assert first["completed_run_ref"]["manifest_sha256"] == "e" * 64
        assert second["disposition"] == "execution_held" and second["completed_run_ref"] is None
        assert all(not s["execution_authorized"] for s in report["sources"])
    assert tree(tmp_path) == before


@pytest.mark.parametrize("mutation,error", [
    ("version", "unsupported_admission_manifest"),
    ("policy", "unknown_off_package_policy_required"),
    ("duplicate", "duplicate_or_excluded_source"),
    ("exclude", "duplicate_or_excluded_source"),
    ("excluded_root", "duplicate_or_excluded_source"),
    ("timeline", "native_timeline_required"),
    ("names", "unknown_off_package_policy_required"),
    ("speaker_project", "unknown_off_package_policy_required"),
    ("execution", "unsupported_source_admission"),
    ("validation_hash", "source_validation_required"),
    ("validation_source", "ambiguous_source_validation"),
    ("unknown_validation", "unsupported_validation_members"),
    ("unsafe_path", "unsafe_relative_path"),
])
def test_semantic_incompatibilities_fail_closed(tmp_path, mutation, error):
    cfg, source, p = multi_package(tmp_path)
    w = json.loads((p / "whitelist.json").read_bytes())
    if mutation == "version":
        m = json.loads((p / "manifest.json").read_bytes()); m["version"] = "0.0.3"
        atomic_json(p / "manifest.json", m)
    elif mutation == "policy": w["speaker_policy"] = {**app.POLICY, "enrollment": True}
    elif mutation == "duplicate": w["include"][1] = w["include"][0]
    elif mutation == "exclude": w["exclude"].append({"path": w["include"][0]["source_path"]})
    elif mutation == "excluded_root": w["excluded_roots"] = ["sources/"]
    elif mutation == "timeline": w["include"][1]["timeline"]["external_transcript_bindings"] = ["old.vtt"]
    elif mutation == "names":
        speaker = json.loads((p / "speaker-policy.json").read_bytes()); speaker["confirmed_persons"] = ["Person"]
        atomic_json(p / "speaker-policy.json", speaker)
    elif mutation == "speaker_project":
        speaker = json.loads((p / "speaker-policy.json").read_bytes()); speaker["project"] = 700
        atomic_json(p / "speaker-policy.json", speaker)
    elif mutation == "execution": w["include"][1]["execution_admission"] = "approved"
    elif mutation in {"validation_hash", "validation_source"}:
        v = json.loads((p / "source-validation-philipp.json").read_bytes())
        v["source_sha256" if mutation == "validation_hash" else "source"] = "f" * 64 if mutation == "validation_hash" else w["include"][0]["source_path"]
        atomic_json(p / "source-validation-philipp.json", v)
    elif mutation == "unknown_validation": atomic_json(p / "source-validation-other.json", {})
    elif mutation == "unsafe_path": w["include"][0]["source_path"] = "../outside.mp4"
    atomic_json(p / "whitelist.json", w)
    resign(cfg, p)
    with pytest.raises(EvidenceError, match=error):
        app.check_package(cfg, mount_check=lambda *a: None)


@pytest.mark.parametrize("target", ["manifest", "member", "source", "missing_source"])
def test_bytes_or_missing_input_never_fall_back(tmp_path, target):
    cfg, source, p = multi_package(tmp_path)
    if target == "manifest": (p / "manifest.json").write_text("{}")
    elif target == "member": (p / "README.md").write_text("changed")
    elif target == "source": source.write_bytes(b"changed")
    else: source.unlink()
    with pytest.raises((EvidenceError, FileNotFoundError)):
        app.check_package(cfg, mount_check=lambda *a: None)


@pytest.mark.parametrize("target", ["source_hash", "job_id", "output", "receipt_identity", "partial"])
def test_bad_completed_evidence_blocks_instead_of_new_asr(tmp_path, target):
    cfg, source, p = multi_package(tmp_path)
    entry = json.loads((p / "whitelist.json").read_bytes())["include"][0]
    area, key = completed(source, entry, "e" * 64)
    if target == "output": (area / key / "transcript.md").write_text("changed")
    elif target == "receipt_identity":
        f = area / key / "receipt.json"; r = json.loads(f.read_bytes())
        r["binding"]["identity"] = json.dumps(["nas:501:other", entry["source_sha256"], "a" * 64, "b" * 64])
        atomic_json(f, r)
    else:
        f = area / "source-state.json"; state = json.loads(f.read_bytes())
        state[{"source_hash": "source_sha256", "job_id": "pipeline_job_id", "partial": "status"}[target]] = {
            "source_hash": "c" * 64, "job_id": "../../escape", "partial": "in-progress"}[target]
        atomic_json(f, state)
    with pytest.raises(EvidenceError):
        app.check_package(cfg, mount_check=lambda *a: None)


def test_execution_path_remains_single_v1_only(tmp_path):
    cfg, source, p = multi_package(tmp_path)
    with pytest.raises(EvidenceError, match="unsupported_admission_manifest"):
        app.Admission(cfg, mount_check=lambda *a: None)


def test_cli_check_never_calls_execution_or_preflight(tmp_path, monkeypatch, capsys):
    import sys
    cfg, source, p = multi_package(tmp_path)
    path = tmp_path / "check.json"
    atomic_json(path, cfg)
    original = app.check_package
    monkeypatch.setattr(app, "check_package", lambda c: original(c, mount_check=lambda *a: None))
    def forbidden(*a, **k):
        pytest.fail("check CLI entered an execution path")
    for name in ["PipelineQueue", "SourceClaim", "run_pilot", "preflight", "prepare_area"]:
        monkeypatch.setattr(app, name, forbidden)
    monkeypatch.setattr(sys, "argv", ["nas_pilot.py", "--config", str(path), "--check-package"])
    before = tree(tmp_path)
    app.main()
    report = json.loads(capsys.readouterr().out)
    assert report["compatibility"] == "accepted" and not report["execution_authorized"]
    assert tree(tmp_path) == before
