import datetime as dt
import hashlib
import json
import pathlib
import subprocess
from types import SimpleNamespace

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


@pytest.mark.parametrize("failure", ["mount", "identity", "space"])
def test_nas_gate_fails_closed(tmp_path, monkeypatch, failure):
    cfg = config(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    cfg["nas"]["mount"] = str(mount)
    cfg["nas"]["root"] = str(mount / "archive")
    identity = mount / cfg["nas"]["identity_file"]
    identity.write_text("wrong" if failure == "identity" else "speaker-transcribe-archive-v1")
    source = "//wrong/share" if failure == "mount" else cfg["nas"]["source"]
    monkeypatch.setattr(archive, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=f"{mount} systemd-1 autofs\n{mount} {source} cifs\n"))
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda p: SimpleNamespace(
        free=0 if failure == "space" else 100 * 1024 ** 3))
    with pytest.raises(RuntimeError):
        archive.nas_ready(cfg)
    assert not (mount / "archive").exists()


@pytest.mark.parametrize("failure", ["source-corrupt", "marker-corrupt", "source-replaced"])
def test_interrupted_or_changed_copy_never_commits_archive(tmp_path, monkeypatch, failure):
    cfg = config(tmp_path)
    cfg["projects"]["415"]["enabled"] = True
    drive, _ = finished(tmp_path)
    row = archive.inventory(drive, cfg, "415")[0]
    nas = tmp_path / "nas"
    nas.mkdir()
    monkeypatch.setattr(archive, "nas_ready", lambda c, need_bytes=0: nas)
    before = drive.files.copy()
    original = drive.call
    def damaged(*args):
        result = original(*args)
        if args[0] == "copyto" and args[1].startswith("gdrive:"):
            marker = args[1].endswith(".state.json")
            if (failure == "source-corrupt" and not marker or
                    failure == "marker-corrupt" and marker):
                pathlib.Path(args[2]).write_bytes(b"damaged")
            if failure == "source-replaced" and marker:
                drive.files["415/voice.m4a"] = b"replacement by another actor"
        return result
    monkeypatch.setattr(drive, "call", damaged)
    with pytest.raises(RuntimeError):
        archive.copy_verified(drive, cfg, row)
    assert not (nas / "415" / row["id"] / row["revision"]).exists()
    assert not list(nas.rglob(".archive-*"))
    if failure != "source-replaced":
        assert drive.files == before


def test_nas_automount_and_reserve_includes_copy_budget(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / cfg["nas"]["identity_file"]).write_text("speaker-transcribe-archive-v1")
    cfg["nas"].update(mount=str(mount), root=str(mount / "archive"))
    monkeypatch.setattr(archive, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=f"{mount} systemd-1 autofs\n{mount} {cfg['nas']['source']} cifs\n"))
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda p: SimpleNamespace(free=51 * 1024 ** 3))
    assert archive.nas_ready(cfg) == mount / "archive"
    with pytest.raises(RuntimeError, match="reserve"):
        archive.nas_ready(cfg, need_bytes=2 * 1024 ** 3)
