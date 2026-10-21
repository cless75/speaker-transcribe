import datetime as dt
import hashlib
import json
import pathlib

import pytest

import hub_archive as archive
import hub_worker
from test_hub_worker import MemoryDrive, fake_asr, media


def config(tmp_path, *, age=0):
    return archive.config_checked({
        "schema_version": 1, "remote": "gdrive:Hub", "worker_root": str(tmp_path),
        "nas": {"mount": "/mnt/wd-book", "source": "//192.168.1.28/Archive",
                "root": "/mnt/wd-book/speaker-transcribe",
                "identity_file": ".speaker-transcribe-archive", "min_free_gib": 50},
        "defaults": {"enabled": False, "hub_action": "keep",
                     "min_days_after_publication": 30},
        "projects": {"415": {"enabled": False, "hub_action": "trash_after_nas",
                             "min_days_after_publication": age}}})


def finished(tmp_path):
    drive = MemoryDrive({"415/voice.m4a": b"fake audio"})
    item = media(drive, project="415")
    hub_worker.process_one(drive, item, tmp_path, {"hub_root": "/mnt/hub"},
                           tmp_path / "config.json", transcribe=fake_asr([]))
    return drive, item


def test_unknown_projects_disabled_and_no_unsafe_policy(tmp_path):
    cfg = config(tmp_path)
    assert archive.policy(cfg, "416")["enabled"] is False
    assert archive.policy(cfg, "415")["hub_action"] == "trash_after_nas"
    cfg["defaults"]["enabled"] = True
    with pytest.raises(ValueError, match="disabled"):
        archive.config_checked(cfg)


def test_inventory_requires_full_verified_publication(tmp_path):
    cfg = config(tmp_path)
    drive, item = finished(tmp_path)
    rows = archive.inventory(drive, cfg, "415")
    assert len(rows) == 1 and rows[0]["status"] == "eligible"
    assert rows[0]["id"] == item.file_id and rows[0]["revision"] == item.revision
    drive.files["415/voice.m4a.state.json"] = json.dumps({"status": "asr-done"}).encode()
    assert "publication-not-verified-for-revision" in archive.inventory(drive, cfg, "415")[0]["reasons"]


def test_inventory_holds_legacy_missing_journal_and_e2e(tmp_path):
    cfg = config(tmp_path)
    drive = MemoryDrive({"415/voice.m4a": b"audio",
                         "415/voice.m4a.state.json": b'{"status":"asr-done"}'})
    assert archive.inventory(drive, cfg, "415")[0]["status"] == "held"
    drive, item = finished(tmp_path)
    hub_worker.atomic_json(tmp_path / "media-e2e.json",
                           {"source_id": item.file_id, "source": "415/voice.m4a"})
    assert "release-e2e-source" in archive.inventory(drive, cfg, "415")[0]["reasons"]


def test_age_and_duplicate_markers_fail_closed(tmp_path):
    cfg = config(tmp_path, age=30)
    drive, _ = finished(tmp_path)
    assert "publication-too-recent" in archive.inventory(drive, cfg, "415")[0]["reasons"]
    cfg["projects"]["415"]["min_days_after_publication"] = 0
    original = drive.list
    def duplicated(*parts, recursive=False):
        entries = original(*parts, recursive=recursive)
        if recursive:
            marker = next(e for e in entries if e.get("Path") == "voice.m4a.state.json")
            entries.append({**marker, "ID": "different-id"})
        return entries
    drive.list = duplicated
    assert "missing-or-ambiguous-marker" in archive.inventory(drive, cfg, "415")[0]["reasons"]


def test_copy_never_mutates_hub_and_rejects_changed_source(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    drive, _ = finished(tmp_path)
    row = archive.inventory(drive, cfg, "415")[0]
    with pytest.raises(ValueError, match="not eligible"):
        archive.copy_verified(drive, cfg, row)
    cfg["projects"]["415"]["enabled"] = True
    nas = tmp_path / "nas"
    nas.mkdir()
    monkeypatch.setattr(archive, "nas_ready", lambda config, need_bytes=0: nas)
    before = drive.files.copy()
    target = archive.copy_verified(drive, cfg, row)
    assert drive.files == before
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["hub_action"] == "none"
    assert (target / "source").read_bytes() == b"fake audio"
    assert manifest["files"]["source"]["sha256"] == hashlib.sha256(b"fake audio").hexdigest()
    with pytest.raises(RuntimeError, match="already exists"):
        archive.copy_verified(drive, cfg, row)
    drive.files["415/voice.m4a"] = b"modified"
    with pytest.raises(RuntimeError, match="eligibility changed"):
        archive.copy_verified(drive, cfg, {**row, "id": "other"})
