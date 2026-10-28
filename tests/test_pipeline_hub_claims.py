import json

import pytest

import hub_worker as hub
from pipeline_hub_claims import HubClaims
from pipeline_runtime import HoldJob, RetryJob
from pipeline_storage import atomic_json
from test_hub_worker import MemoryDrive, media


def fixture(tmp_path):
    drive = MemoryDrive({"_test505/voice.m4a": b"synthetic"})
    item = media(drive)
    job = {"id": "a" * 64}
    now = [100.0]
    claims = HubClaims(drive, tmp_path / "claims", host="test-PIPELINE",
                       convergence_seconds=0, clock=lambda: now[0])
    return drive, item, job, now, claims


def test_claim_survives_stage_gaps_and_restarts_without_retaking(tmp_path):
    drive, item, job, now, claims = fixture(tmp_path)
    claims.ensure(job, item)
    claims.check(job)
    value = json.loads(drive.files[item.project + "/" + item.path + ".claim.json"])
    token = value["claim"]["pipeline_token"]
    claims.refresh()
    resumed = HubClaims(drive, tmp_path / "claims", host="test-PIPELINE", convergence_seconds=0)
    resumed.ensure(job, item)
    assert resumed.active[job["id"]].token == token
    resumed.retire(job)
    assert json.loads(drive.files[item.project + "/" + item.path + ".claim.json"])["claim"]["claim_phase"] == "done"
    resumed.refresh()
    assert not resumed.active


def test_expired_foreign_claim_is_never_preempted(tmp_path):
    drive, item, job, now, claims = fixture(tmp_path)
    old = {"claim": {"claimed_by": "other", "lease_until": "2000-01-01T00:00:00Z", "claim_phase": "asr"}}
    path = item.project + "/" + item.path + ".claim.json"
    drive.files[path] = json.dumps(old).encode()
    with pytest.raises(HoldJob, match="requires_reconciliation"):
        claims.ensure(job, item)
    assert json.loads(drive.files[path]) == old


def test_ownership_staleness_never_refreshes_itself_without_remote_proof(tmp_path):
    drive, item, job, now, claims = fixture(tmp_path)
    claims.ensure(job, item)
    now[0] += 121
    with pytest.raises(RetryJob, match="stale"):
        claims.check(job)
    claims.refresh()
    claims.check(job)


def test_remote_token_change_fences_local_owner(tmp_path):
    drive, item, job, now, claims = fixture(tmp_path)
    claims.ensure(job, item)
    path = item.project + "/" + item.path + ".claim.json"
    value = json.loads(drive.files[path]);value["claim"]["pipeline_token"] = "foreign"
    drive.files[path] = json.dumps(value).encode()
    claims.refresh()
    with pytest.raises(HoldJob):
        claims.check(job)
    assert json.loads(drive.files[path])["claim"]["pipeline_token"] == "foreign"


def test_ambiguous_first_write_can_resume_only_its_own_persisted_token(tmp_path):
    drive, item, job, now, claims = fixture(tmp_path)
    token = "persisted-before-remote-write"
    atomic_json(tmp_path / "claims" / (job["id"] + ".json"),
                {"host": claims.host, "token": token, "source_id": item.file_id,
                 "revision": item.revision, "remote_id": None})
    drive.files[item.project + "/" + item.path + ".claim.json"] = json.dumps(
        {"claim": {"claimed_by": claims.host, "pipeline_token": token,
                   "source_id": item.file_id, "source_revision": item.revision}}).encode()
    claims.ensure(job, item)
    assert claims.active[job["id"]].token == token
