"""Зеркало тяжёлого в Hub не должно возвращаться во вход.

Канон Hub 258-a кладёт исходники и выдачу в ``{hub}/{pid}/process`` и
``{hub}/{pid}/delivery``. Расширения там те же, что у дропов, поэтому без явного
пропуска рекурсивный обход папки проекта ставит зеркало в очередь на распознавание —
и на облачном диске упирается в dataless-поддерево (наблюдалось 19.09.2026: прогон
час вместо 16 с, дважды OSError 22, дроп в ``_PE45_inbox`` не взят).
"""
from __future__ import annotations

import os
import pathlib

import audio_inbox_watch as watcher


def build_hub(tmp_path: pathlib.Path) -> pathlib.Path:
    hub = tmp_path / "Hub"
    (hub / "PE45" / "_PE45_inbox").mkdir(parents=True)
    (hub / "PE45" / "process" / "2026" / "45").mkdir(parents=True)
    (hub / "PE45" / "delivery").mkdir(parents=True)
    (hub / "PE45" / "_PE45_inbox" / "drop.wav").write_bytes(b"0")
    (hub / "PE45" / "process" / "2026" / "45" / "heavy-record.mp4").write_bytes(b"0")
    (hub / "PE45" / "delivery" / "final-cut.mp4").write_bytes(b"0")
    return hub


def cfg_for(hub: pathlib.Path) -> dict:
    return {
        "hub_root": str(hub),
        "sources": [{"root": "{hub_root}", "discover": "project-inboxes"}],
    }


def test_drop_in_project_inbox_is_found(tmp_path):
    hub = build_hub(tmp_path)
    found = {p.name for p, _root, _src in watcher.find_audio_files(cfg_for(hub))}
    assert "drop.wav" in found


def test_process_and_delivery_are_not_scanned(tmp_path):
    hub = build_hub(tmp_path)
    found = {p.name for p, _root, _src in watcher.find_audio_files(cfg_for(hub))}
    assert "heavy-record.mp4" not in found
    assert "final-cut.mp4" not in found


def test_skip_list_names_the_mirror():
    assert "process" in watcher.DEFAULT_SKIP_FOLDERS
    assert "delivery" in watcher.DEFAULT_SKIP_FOLDERS


def test_recursive_scan_never_enters_excluded_trees(tmp_path, monkeypatch):
    hub = build_hub(tmp_path)
    project = hub / "PE45"
    (project / "sessions" / "2026-09").mkdir(parents=True)
    (project / "custom" / "2026-09").mkdir(parents=True)
    (project / "custom" / "2026-09" / "drop.mp3").write_bytes(b"x")
    original_scandir = os.scandir
    excluded = {"process", "delivery", "sessions", "_PE45_inbox"}

    def checked_scandir(path):
        if pathlib.Path(path).name in excluded:
            raise AssertionError(f"listed excluded subtree: {path}")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", checked_scandir)
    found = watcher._scan_one_source(project, {}, recursive=True)
    assert [p.name for p in found] == ["drop.mp3"]


def test_project_index_does_not_descend_into_session_outputs(tmp_path, monkeypatch):
    project = tmp_path / "Hub" / "PE45"
    (project / "sessions" / "2026-09").mkdir(parents=True)
    (project / "sessions" / "2026-09" / "ignored.mp3.state.json").write_text(
        '{"status":"queued"}', encoding="utf-8")
    (project / "other").mkdir()
    (project / "other" / "kept.mp3.state.json").write_text(
        '{"status":"queued"}', encoding="utf-8")
    original_scandir = os.scandir

    def checked_scandir(path):
        if pathlib.Path(path).name == "sessions":
            raise AssertionError("index listed sessions outputs")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", checked_scandir)
    watcher._write_project_index(tmp_path / "Hub", "PE45", {})
    index = (project / "_sessions-index.md").read_text(encoding="utf-8")
    assert "kept.mp3" in index
    assert "ignored.mp3" not in index


def test_project_index_preserves_previous_version_when_cloud_listing_fails(tmp_path,
                                                                          monkeypatch):
    project = tmp_path / "Hub" / "PE45"
    (project / "other").mkdir(parents=True)
    index = project / "_sessions-index.md"
    index.write_text("previous complete index", encoding="utf-8")
    original_scandir = os.scandir

    def failing_scandir(path):
        if pathlib.Path(path).name == "other":
            raise OSError("cloud listing failed")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", failing_scandir)
    watcher._write_project_index(tmp_path / "Hub", "PE45", {})
    assert index.read_text(encoding="utf-8") == "previous complete index"


def test_pickup_abort_skips_global_cleanup_and_releases_lock(tmp_path, monkeypatch):
    root = tmp_path / "inbox"
    root.mkdir()
    audio = root / "drop.mp3"
    audio.write_bytes(b"x")
    cfg = {"hub_root": str(tmp_path), "sources": [{"root": str(root)}],
           "write_session_index": True, "status_page": False}
    calls = []
    monkeypatch.setattr(watcher, "acquire_watcher_lock", lambda: tmp_path / "lock")
    monkeypatch.setattr(watcher, "release_watcher_lock", lambda lock: calls.append("unlock"))
    monkeypatch.setattr(watcher, "load_mapper", lambda cfg: {})
    monkeypatch.setattr(watcher, "status_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(watcher, "publish_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(watcher.jobs_queue, "process_next_job", lambda *args, **kwargs: {})
    monkeypatch.setattr(watcher, "maybe_diagnose_after_boot", lambda *args: None)
    monkeypatch.setattr(watcher, "find_audio_files", lambda cfg: [(audio, root, {})])
    monkeypatch.setattr(watcher, "apply_bundle_metadata", lambda files, cfg, mapper: files)
    monkeypatch.setattr(watcher, "process_one_file",
                        lambda *args: (_ for _ in ()).throw(watcher._CloudOpTimeout("test")))
    monkeypatch.setattr(watcher, "_sweep_processed_moves",
                        lambda *args: calls.append("tidy"))
    monkeypatch.setattr(watcher, "_write_project_index",
                        lambda *args: calls.append("index"))
    watcher.run_once(cfg, tmp_path / "node.json", max_files=1)
    assert calls == ["unlock"]
