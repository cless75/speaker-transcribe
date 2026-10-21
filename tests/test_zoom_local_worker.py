"""Local Zoom intake may publish text, never the source video or ASR WAV chunks."""

import contextlib
import hashlib
import json
import pathlib
import sqlite3

import pytest

import audio_inbox_watch as watcher
import zoom_local_worker as local
from hub_intake import Drive

DISPATCH_GUARD = local.require_dispatch_inactive


@pytest.fixture(autouse=True)
def no_systemd_in_offline_tests(monkeypatch):
    monkeypatch.setattr(local, "require_dispatch_inactive", lambda _: None)


class MemoryDrive(Drive):
    def __init__(self):
        super().__init__("gdrive:Hub")
        self.files = {}
        self.fail_once = False

    def list(self, *parts, recursive=False):
        prefix = "/".join(parts).rstrip("/") + "/"
        return [{"Path": path[len(prefix):], "Name": pathlib.PurePosixPath(path).name,
                 "ID": hashlib.sha256(path.encode()).hexdigest()[:12], "Size": len(data),
                 "Hashes": {"md5": hashlib.md5(data).hexdigest()}, "IsDir": False}
                for path, data in self.files.items() if path.startswith(prefix)]

    def call(self, verb, *args):
        if verb == "copy":
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("publication interrupted")
            src, target = pathlib.Path(args[0]), args[1].removeprefix("gdrive:Hub/")
            for path in src.rglob("*"):
                if path.is_file():
                    self.files[f"{target}/{path.relative_to(src).as_posix()}"] = path.read_bytes()
            return b""
        if verb == "copyto":
            self.files[args[1].removeprefix("gdrive:Hub/")] = pathlib.Path(args[0]).read_bytes()
            return b""
        if verb == "cat":
            return self.files[args[0].removeprefix("gdrive:Hub/")]
        raise AssertionError(verb)


def fixture(tmp_path):
    repo = tmp_path / "zoom-recordings"
    repo.mkdir()
    video = repo / "private.mp4"
    vtt = repo / "private.vtt"
    video.write_bytes(b"synthetic VIDEO")
    vtt.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nAlice: hi\n", encoding="utf-8")
    db = tmp_path / "zoomrs.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE meetings (uuid TEXT PRIMARY KEY, topic TEXT, startTime TEXT)")
        connection.execute("CREATE TABLE records (id TEXT PRIMARY KEY, meetingId TEXT, type TEXT, "
                           "fileExtension TEXT, fileSize INTEGER, status TEXT, path TEXT)")
        connection.execute("INSERT INTO meetings VALUES (?, ?, ?)",
                           ("uuid-1", "Project 415", "2026-10-20 07:02:30"))
        for kind, source in (("active_speaker", video), ("audio_transcript", vtt)):
            connection.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (kind, "uuid-1", kind, source.suffix[1:].upper(),
                                source.stat().st_size, "downloaded", str(source)))
    dispatch = tmp_path / "dispatch.json"
    state = tmp_path / "dispatch-state.json"
    dispatch.write_text(json.dumps({"approved_meetings": [], "state_file": str(state)}))
    cfg = {"database": str(db), "repository": str(repo), "hub_root": str(tmp_path / "Hub"),
           "drive_remote": "gdrive:Hub", "dispatch_config": str(dispatch),
           "container_repository": None, "min_stable_age_seconds": 0,
           "routes": [{"contains": "415", "pid": "415"}], "fallback_pid": "unrouted",
           "local_root": str(tmp_path / "out"), "hub_worker_root": str(tmp_path / "worker")}
    return cfg, dispatch, state


def fake_asr(calls):
    def transcribe(video, vtt, output):
        calls.append((video, vtt))
        assert video.read_bytes() == b"synthetic VIDEO"
        assert "Alice" in vtt.read_text(encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        transcript = output / "recording-transcript.md"
        transcript.write_text("recognized MEDIA speech", encoding="utf-8")
        (output / "recording-asr-chunk-000.wav").write_bytes(b"private audio")
        return transcript, {"status": "degraded", "quality": {
            "status": "degraded", "gaps_sec": 152.17, "thresholds": {"gaps_sec_max": 30}},
            "zoom_vtt": {"status": "ok", "speakers_detected": 1}}
    return transcribe


def test_local_pair_publishes_only_verified_text_and_quality(tmp_path):
    cfg, _, _ = fixture(tmp_path)
    drive = MemoryDrive()
    items = local.select(cfg)
    assert len(items) == 1 and items[0]["pid"] == "415"
    calls = []
    drive.fail_once = True
    with pytest.raises(RuntimeError, match="publication interrupted"):
        local.process_one(cfg, items[0], drive, transcribe=fake_asr(calls))
    job = next((tmp_path / "out" / "jobs").glob("*/job.json"))
    assert json.loads(job.read_text())["phase"] == "publishing"
    assert not list((tmp_path / "out" / "jobs").glob("*/state.json"))
    result = local.process_one(cfg, items[0], drive, transcribe=fake_asr(calls))
    assert len(calls) == 1 and result["phase"] == "done"
    assert {pathlib.PurePosixPath(path).name for path in drive.files} == {
        "transcript.md", "quality.json", "state.json"}
    assert not any(path.endswith((".mp4", ".wav", ".vtt")) for path in drive.files)
    assert b"recognized MEDIA speech" in next(data for path, data in drive.files.items()
                                              if path.endswith("transcript.md"))
    state = json.loads(next(data for path, data in drive.files.items()
                            if path.endswith("pipeline/state.json")))
    assert state["status"] == "asr-done" and state["publication_verified"]
    assert state["speaker_source"] == "zoom_vtt"
    assert state["transcript_quality"]["gaps_sec"] == 152.17


def test_dispatch_ownership_or_matching_hub_vtt_prevents_duplicate(tmp_path):
    cfg, dispatch, state = fixture(tmp_path)
    item = local.select(cfg)[0]
    doc = json.loads(dispatch.read_text())
    doc["approved_meetings"] = ["uuid-1"]
    dispatch.write_text(json.dumps(doc))
    assert local.select(cfg) == []
    doc["approved_meetings"] = []
    dispatch.write_text(json.dumps(doc))
    state.write_text(json.dumps({"uuid-1": {"delivered": True}}))
    assert local.select(cfg) == []
    state.write_text("{}")
    drive = MemoryDrive()
    name = f"415/_415_inbox/{item['week']}/{item['stem']}.transcript.vtt"
    drive.files[name] = b"WEBVTT"
    with pytest.raises(RuntimeError, match="matching VTT already in Hub"):
        local.process_one(cfg, item, drive, transcribe=fake_asr([]))
    assert not list((tmp_path / "out" / "jobs").glob("*/state.json"))


def test_dry_run_and_runner_are_bounded(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    drive = MemoryDrive()
    assert local.run(cfg, dry_run=True, drive=drive) == {"eligible": 1, "processed": 0, "held": 0}
    assert not (tmp_path / "out").exists()
    monkeypatch.setattr(local, "single_worker", lambda _: contextlib.nullcontext())
    calls = []
    assert local.run(cfg, dry_run=False, drive=drive, transcribe=fake_asr(calls))["processed"] == 1
    assert local.run(cfg, dry_run=False, drive=drive, transcribe=fake_asr(calls))["processed"] == 0
    assert local.run(cfg, dry_run=True, drive=drive)["eligible"] == 0
    assert len(calls) == 1


def test_dispatch_guard_blocks_activating_service(monkeypatch):
    monkeypatch.setattr(local, "require_dispatch_inactive", DISPATCH_GUARD)
    def show(cmd, **kwargs):
        return type("Result", (), {"returncode": 0,
                                   "stdout": "inactive\n" if cmd[-1].endswith(".timer") else "activating\n"})()
    monkeypatch.setattr(local.subprocess, "run", show)
    with pytest.raises(RuntimeError, match="dispatcher is active"):
        local.require_dispatch_inactive({})


def test_real_watcher_bridge_uses_local_vtt_without_hub_voiceprint(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    node = tmp_path / "node.json"
    node.write_text(json.dumps({"voiceprint_mode": "match_enroll"}))
    cfg["node_config"] = str(node)
    observed = []

    def bridge(video, runtime, pid, inbox, output, config_path):
        assert runtime["voiceprint_mode"] == "off"
        assert watcher.zoom_vtt_for(video, runtime) == inbox / "recording.transcript.vtt"
        observed.append((pid, video.suffix, output))
        output.mkdir(parents=True, exist_ok=True)
        transcript = output / "recording-transcript.md"
        transcript.write_text("media words", encoding="utf-8")
        return True, "", transcript, {"status": "ok", "zoom_vtt": {
            "status": "ok", "speakers_detected": 1}}

    monkeypatch.setattr(local.watcher, "run_asr", bridge)
    result = local.process_one(cfg, local.select(cfg)[0], MemoryDrive())
    assert result["phase"] == "done" and observed[0][1] == ".mp4"
