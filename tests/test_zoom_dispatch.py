"""Offline checks against zoomrs' actual SQLite table layout."""

import json
import pathlib
import sqlite3

import pytest

import audio_inbox_watch as watcher
import zoom_dispatch as dispatch
from zoom_vtt_import import parse_zoom_vtt


def fixture(tmp_path):
    db = tmp_path / "zoomrs.db"
    repo = tmp_path / "recordings"
    repo.mkdir()
    hub = tmp_path / "Hub"
    hub.mkdir()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE meetings (uuid TEXT PRIMARY KEY, id INTEGER, topic TEXT, startTime TEXT)")
        connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY, meetingId TEXT, type TEXT, "
                           "startTime TEXT, fileExtension TEXT, fileSize INTEGER, downUrl TEXT, "
                           "playUrl TEXT, status TEXT, path TEXT)")
        for i, topic in [(1, "Example Course lesson"), (2, "Unknown meeting")]:
            uuid = f"uuid-{i}"
            connection.execute("INSERT INTO meetings VALUES (?, ?, ?, ?)",
                               (uuid, i, topic, f"2026-10-17 08:{i:02d}:00"))
            for kind, ext, content in [("audio_transcript", "VTT", b"WEBVTT\n\n"
                                        b"00:00:01.000 --> 00:00:03.000\nAlice: Hello\n"),
                                       ("chat_file", "TXT", b"chat")]:
                path = repo / f"{i}-{kind}.{ext.lower()}"
                path.write_bytes(content)
                connection.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?)",
                                   (f"{i}-{kind}", uuid, kind, f"2026-10-17 08:{i:02d}:00",
                                    ext, len(content), "downloaded", str(path)))
    cfg = {"database": str(db), "repository": str(repo), "hub_root": str(hub),
           "require_hub_mount": False, "state_file": str(tmp_path / "state.json"),
           "routes": [{"contains": "example course", "pid": "PE45"}],
            "fallback_pid": "unrouted", "worker_folder": "w1",
            "approved_meetings": ["uuid-1", "uuid-2"],
           "max_meetings_per_run": 1, "min_stable_age_seconds": 0}
    return cfg, hub, repo


def test_delivers_chat_first_then_vtt_and_resumes_without_duplicates(tmp_path, monkeypatch):
    cfg, hub, _ = fixture(tmp_path)
    calls = []
    copy = dispatch._copy_once

    def record_order(src, target):
        calls.append(target.suffix)
        copy(src, target)

    monkeypatch.setattr(dispatch, "_copy_once", record_order)
    first = dispatch.dispatch_once(cfg)
    assert first["delivered"] == 1 and calls == [".txt", ".vtt"]
    fallback = hub / "unrouted" / "_unrouted_inbox" / "w1"
    vtt = next(fallback.glob("*.vtt"))
    assert parse_zoom_vtt(vtt)[0]["speaker"] == "Alice"
    assert dispatch.dispatch_once(cfg)["delivered"] == 1  # next meeting, per-run throttle
    assert dispatch.dispatch_once(cfg)["delivered"] == 0
    assert len(list((hub / "PE45" / "_PE45_inbox" / "w1").glob("*.vtt"))) == 1
    assert len(json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))) == 2


def test_dry_run_never_writes_hub_or_state(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    assert dispatch.dispatch_once(cfg, dry_run=True)["delivered"] == 1
    assert not list(hub.iterdir())
    assert not (tmp_path / "state.json").exists()


def test_retry_after_vtt_publication_recovers_same_file(tmp_path, monkeypatch):
    cfg, hub, _ = fixture(tmp_path)
    old = dispatch._save_state
    monkeypatch.setattr(dispatch, "_save_state", lambda *args: (_ for _ in ()).throw(OSError("power loss")))
    with pytest.raises(OSError, match="power loss"):
        dispatch.dispatch_once(cfg)
    monkeypatch.setattr(dispatch, "_save_state", old)
    assert dispatch.dispatch_once(cfg)["delivered"] == 1
    assert len(list((hub / "unrouted" / "_unrouted_inbox" / "w1").glob("*.vtt"))) == 1


def test_dispatch_then_watcher_completes_without_media(tmp_path, monkeypatch):
    cfg, hub, _ = fixture(tmp_path)
    dispatch.dispatch_once(cfg)
    wc = {"hub_root": str(hub), "node": {"host_label": "TEST"},
          "sources": [{"root": "{hub_root}", "discover": "project-inboxes", "claim": False}],
          "zoom_vtt_only_input": True, "voiceprint_mode": "off",
          "outputs": {"transcript_dir": "{hub_root}/{pid}/sessions/{YYYY-MM}/{sid}/transcripts",
                      "state_dir": "{hub_root}/{pid}/sessions/{YYYY-MM}/{sid}/pipeline"}}
    monkeypatch.setattr(watcher, "run_asr", lambda *a, **k: pytest.fail("ASR must not run"))
    monkeypatch.setattr(watcher, "status_event", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "publish_status", lambda *a, **k: None)
    files = watcher.find_audio_files(wc)
    assert len(files) == 1 and files[0][0].suffix == ".vtt"
    for source, root, route in files:
        watcher.process_one_file(source, root, route, wc, {}, tmp_path / "node.json")
    watcher._write_project_index(hub, "unrouted", wc)
    assert dispatch.dispatch_once({**cfg, "max_meetings_per_run": 2})["already"] == 1
    state = json.loads(watcher.state_path(files[0][0]).read_text(encoding="utf-8"))
    assert state["status"] == "asr-done"
    assert "Hello" in pathlib.Path(state["transcript_path"]).read_text(encoding="utf-8")


def test_existing_processed_index_prevents_republishing(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    meeting = dispatch._meetings(tmp_path / "zoomrs.db")[0]
    vtt = next(r for r in meeting["records"] if r["type"] == "audio_transcript")
    name = dispatch._filename(meeting, vtt) + ".transcript.vtt"
    project = hub / "unrouted"
    project.mkdir()
    (project / "_sessions-index.md").write_text(
        f"| `{name}` | asr-done | — | S20261017T0802 |\n", encoding="utf-8")
    result = dispatch.dispatch_once(cfg)
    assert result["delivered"] == 1  # second meeting, but not the already processed first
    assert result["already"] == 1
    assert not list((project / "_unrouted_inbox").glob("**/*.vtt"))


def test_legacy_processed_mapping_uses_explicit_identity_not_timestamp(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    project = hub / "unrouted"
    project.mkdir()
    old_name = "20261017_080200_Recording.mp4"
    (project / "_sessions-index.md").write_text(
        f"| `{old_name}` | asr-done | — | S20261017T0802 |\n", encoding="utf-8")
    cfg["legacy_processed"] = {"uuid-2": {"pid": "unrouted", "filename": old_name}}
    cfg["worker_folder"] = "iso_week"
    result = dispatch.dispatch_once(cfg)
    assert result["already"] == 1
    assert result["delivered"] == 1  # only the other, approved meeting
    assert not list((project / "_unrouted_inbox").glob("**/*.vtt"))
    assert list((hub / "PE45" / "_PE45_inbox" / "w42").glob("*.vtt"))


def test_unapproved_or_unverified_legacy_meeting_cannot_be_dispatched(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    cfg["approved_meetings"] = []
    report = dispatch.dispatch_once(cfg)
    assert report["unapproved"] == 2 and report["delivered"] == 0
    assert not list(hub.iterdir())
    cfg["approved_meetings"] = ["uuid-2"]
    cfg["legacy_processed"] = {"uuid-2": {"pid": "unrouted", "filename": "old.mp4"}}
    with pytest.raises(ValueError, match="legacy meeting approval"):
        dispatch.dispatch_once(cfg)
    assert not list(hub.iterdir())


def test_processed_sidecar_recovers_before_index_refresh(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    meeting = dispatch._meetings(tmp_path / "zoomrs.db")[0]
    vtt = next(r for r in meeting["records"] if r["type"] == "audio_transcript")
    name = dispatch._filename(meeting, vtt) + ".transcript.vtt"
    processed = hub / "unrouted" / "_unrouted_inbox" / "_processed"
    processed.mkdir(parents=True)
    (processed / (name + ".state.json")).write_text(
        '{"status":"asr-done"}', encoding="utf-8")
    result = dispatch.dispatch_once(cfg)
    assert result["already"] == 1
    assert not list((hub / "unrouted" / "_unrouted_inbox" / "w1").glob("*.vtt"))


def test_mounted_hub_required_and_external_paths_refused(tmp_path):
    cfg, hub, repo = fixture(tmp_path)
    cfg["require_hub_mount"] = True
    with pytest.raises(ValueError, match="Hub mount"):
        dispatch.dispatch_once(cfg)
    cfg["require_hub_mount"] = False
    with sqlite3.connect(cfg["database"]) as connection:
        connection.execute("UPDATE records SET path=? WHERE id='2-audio_transcript'",
                           (str(tmp_path / "zoomrs.db"),))
    with pytest.raises(ValueError, match="outside"):
        dispatch.dispatch_once(cfg)
    assert not list(hub.iterdir())


def test_container_record_paths_map_to_host_repository(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    cfg["container_repository"] = "/data/recordings"
    with sqlite3.connect(cfg["database"]) as connection:
        for record_id, original_path in connection.execute("SELECT id, path FROM records"):
            connection.execute("UPDATE records SET path=? WHERE id=?",
                               ("/data/recordings/" + pathlib.Path(original_path).name, record_id))
    assert dispatch.dispatch_once(cfg)["delivered"] == 1
    assert len(list((hub / "unrouted" / "_unrouted_inbox" / "w1").glob("*.vtt"))) == 1


def test_container_path_cannot_escape_repository(tmp_path):
    cfg, hub, _ = fixture(tmp_path)
    cfg["container_repository"] = "/data/recordings"
    with sqlite3.connect(cfg["database"]) as connection:
        connection.execute("UPDATE records SET path=? WHERE id='2-audio_transcript'",
                           ("/data/recordings/../../private.vtt",))
    with pytest.raises(ValueError, match="escapes"):
        dispatch.dispatch_once(cfg)
    assert not list(hub.iterdir())


def test_config_rejects_traversal_and_ambiguous_routes(tmp_path):
    path = tmp_path / "config.json"
    cfg, _, _ = fixture(tmp_path)
    cfg["fallback_pid"] = "../private"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe pid"):
        dispatch.load_config(path)
    cfg["fallback_pid"] = "unrouted"
    cfg["routes"] = [{"contains": "x", "regex": "x", "pid": "PE45"}]
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        dispatch.load_config(path)
