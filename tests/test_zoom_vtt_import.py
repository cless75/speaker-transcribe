"""A Zoom transcript can be consumed without sending the heavy video to Hub."""

import json
import pathlib

import pytest

import audio_inbox_watch as watcher
from zoom_vtt_import import import_zoom_vtt, parse_zoom_vtt


VTT = ("WEBVTT\n\n"
       "1\n00:00:01.100 --> 00:00:03.500\nAlice: Hello there.\n\n"
       "2\n00:00:03.500 --> 00:00:05.000\n<v Bob>Hi!</v>\n\n"
       "3\n00:00:05.000 --> 00:00:06.000\nNo known speaker.\n")


def test_import_preserves_named_and_unnamed_cues(tmp_path):
    source = tmp_path / "20261017_083000.transcript.vtt"
    source.write_text(VTT, encoding="utf-8")
    transcript, meta = import_zoom_vtt(source, tmp_path / "out")
    body = transcript.read_text(encoding="utf-8")
    assert "[00:00:01] Alice [zoom_vtt]: Hello there." in body
    assert "[00:00:03] Bob [zoom_vtt]: Hi!" in body
    assert "[00:00:05] No known speaker." in body
    assert meta["diarization"]["speakers_detected"] == 2
    assert json.loads((tmp_path / "out" / f"{source.stem}-run-meta.json")
                      .read_text(encoding="utf-8"))["source_file"] == str(source)


def test_malformed_vtt_never_publishes_success(tmp_path):
    source = tmp_path / "bad.vtt"
    source.write_text("WEBVTT\n\n00:00:05.000 --> 00:00:01.000\nbad\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_zoom_vtt(source)
    ok, error, transcript, meta = watcher.run_vtt_import(source, tmp_path / "out")
    assert not ok and "failed" in error.lower()
    assert transcript is None and meta is None
    assert not (tmp_path / "out" / "bad-transcript.md").exists()


def test_opt_in_scans_vtt_but_not_a_media_sidecar(tmp_path):
    inbox = tmp_path / "Hub" / "PE45" / "_PE45_inbox" / "w1"
    inbox.mkdir(parents=True)
    standalone = inbox / "20261017_083000.transcript.vtt"
    standalone.write_text(VTT, encoding="utf-8")
    media = inbox / "20261017_093000_Recording.mp4"
    media.write_bytes(b"media")
    attached = inbox / "20261017_093000_Recording.transcript.vtt"
    attached.write_text(VTT, encoding="utf-8")
    historical = tmp_path / "Hub" / "PE45" / "old-recording.vtt"
    historical.write_text(VTT, encoding="utf-8")
    cfg = {"hub_root": str(tmp_path / "Hub"),
           "sources": [{"root": "{hub_root}", "discover": "project-inboxes", "claim": False}],
           "zoom_vtt_only_input": True}
    found = [path for path, _, _ in watcher.find_audio_files(cfg)]
    assert standalone in found
    assert media in found
    assert attached not in found
    assert historical not in found
    assert standalone not in [path for path, _, _ in watcher.find_audio_files(
        {**cfg, "zoom_vtt_only_input": False})]


def test_standalone_vtt_uses_watcher_state_and_index(tmp_path, monkeypatch):
    hub = tmp_path / "Hub"
    inbox = hub / "PE45" / "_PE45_inbox" / "w1"
    inbox.mkdir(parents=True)
    source = inbox / "20261017_083000.transcript.vtt"
    source.write_text(VTT, encoding="utf-8")
    cfg = {"hub_root": str(hub), "node": {"host_label": "TEST"},
           "zoom_vtt_only_input": True, "voiceprint_mode": "match_enroll",
           "outputs": {"transcript_dir": "{hub_root}/{pid}/sessions/{YYYY-MM}/{sid}/transcripts",
                       "state_dir": "{hub_root}/{pid}/sessions/{YYYY-MM}/{sid}/pipeline"}}
    monkeypatch.setattr(watcher, "run_asr", lambda *args, **kwargs: pytest.fail("ASR must not run"))
    monkeypatch.setattr(watcher, "wait_for_cpu_available",
                        lambda *args: pytest.fail("VTT import must not wait for CPU"))
    monkeypatch.setattr(watcher, "status_event", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "publish_status", lambda *a, **k: None)
    route = {"route": "literal", "project": "PE45", "claim": False}
    for _ in range(2):
        watcher.process_one_file(source, inbox, route, cfg, {}, tmp_path / "node.json")
    state = json.loads(watcher.state_path(source).read_text(encoding="utf-8"))
    assert state["status"] == "asr-done"
    assert state["worker_completed"] is True
    assert state["session_id"]
    assert watcher.marker_processed_asr(source).exists()
    transcript = state["transcript_path"]
    assert "Alice" in pathlib.Path(transcript).read_text(encoding="utf-8")
    watcher._write_project_index(hub, "PE45", cfg)
    index = (hub / "PE45" / "_sessions-index.md").read_text(encoding="utf-8")
    assert "20261017_083000.transcript.vtt" in index
    assert "Обработанные (asr-done) (1)" in index
