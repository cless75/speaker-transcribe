import hashlib
import datetime as dt
import json
import pathlib

import pytest

import hub_worker
from hub_intake import Drive, MediaFile


class MemoryDrive(Drive):
    def __init__(self, files):
        super().__init__("gdrive:Hub")
        self.files = dict(files)
        self.fail_copy_once = False
        self.corrupt_output = False
        self.source_statuses = []

    def _key(self, path):
        assert path.startswith("gdrive:Hub/")
        return path[len("gdrive:Hub/"):]

    def list(self, *parts, recursive=False):
        if not parts:
            return [{"Name": name, "IsDir": True}
                    for name in sorted({path.split("/", 1)[0] for path in self.files})]
        prefix = "/".join(parts)
        output = []
        dirs = set()
        for path, contents in sorted(self.files.items()):
            if not path.startswith(prefix + "/"):
                continue
            relative = path[len(prefix) + 1:]
            if not recursive and "/" in relative:
                top = relative.split("/", 1)[0]
                if top not in dirs:
                    output.append({"Name": top, "Path": top, "IsDir": True})
                    dirs.add(top)
                continue
            output.append({"Path": relative, "Name": pathlib.PurePosixPath(path).name,
                           "Size": len(contents), "ModTime": "2026-10-17T09:00:00Z",
                           "IsDir": False, "ID": "id-" + hashlib.sha1(path.encode()).hexdigest()[:12],
                           "Hashes": {"md5": hashlib.md5(contents).hexdigest()}})
        return output

    def state(self, project, path):
        return json.loads(self.files[project + "/" + path + ".state.json"])

    def states_bulk(self, project, target):
        for path, data in self.files.items():
            if path.startswith(project + "/") and path.endswith(".state.json"):
                local = target.joinpath(*path[len(project) + 1:].split("/"))
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)

    def call(self, *args):
        verb = args[0]
        if verb == "cat":
            return self.files[self._key(args[1])]
        if verb == "copyto":
            src, dst = args[1:3]
            if src.startswith("gdrive:"):
                pathlib.Path(dst).write_bytes(self.files[self._key(src)])
            else:
                self.files[self._key(dst)] = pathlib.Path(src).read_bytes()
                if dst.endswith(".m4a.state.json") or dst.endswith(".vtt.state.json"):
                    self.source_statuses.append(json.loads(self.files[self._key(dst)])["status"])
            return b""
        if verb == "copy":
            if self.fail_copy_once:
                self.fail_copy_once = False
                raise RuntimeError("Drive publish interrupted")
            src, dst = pathlib.Path(args[1]), self._key(args[2])
            for path in src.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    self.files[dst + "/" + path.relative_to(src).as_posix()] = (
                        b"damaged" if self.corrupt_output else data)
            return b""
        raise AssertionError(args)


def media(drive, project="_test505", path="voice.m4a"):
    return hub_worker.choose_input(drive, project, path, allow_vtt=True)


def fake_asr(calls):
    def run(audio, output, cfg, config_path, pid):
        calls.append(audio)
        output.mkdir(parents=True, exist_ok=True)
        transcript = output / (audio.stem + "-transcript.md")
        transcript.write_text("[00:00] audible words", encoding="utf-8")
        (output / (audio.stem + "-run-meta.json")).write_text(
            json.dumps({"status": "ok", "outputs": {"md": str(transcript)}}), encoding="utf-8")
        return transcript, {"status": "ok"}
    return run


def test_local_asr_output_is_published_and_verified_before_done(tmp_path):
    drive = MemoryDrive({"_test505/voice.m4a": b"fake audio"})
    calls = []
    result = hub_worker.process_one(drive, media(drive), tmp_path,
                                    {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                                    transcribe=fake_asr(calls))
    assert len(calls) == 1
    assert result["status"] == "asr-done"
    assert result["transcript_path"].replace("\\", "/").startswith(
        "/mnt/hub/_test505/sessions/")
    assert json.loads(drive.files["_test505/voice.m4a.state.json"])["status"] == "asr-done"
    assert drive.source_statuses == ["copying", "in-progress", "awaiting-publication", "asr-done"]
    assert b"https://drive.google.com/file/d/" in drive.files["_test505/index.html"]
    assert any(name.endswith("pipeline/state.json") for name in drive.files)


def test_degraded_transcript_publishes_with_quality_warning_after_verification(tmp_path):
    drive = MemoryDrive({"553/voice.m4a": b"audio"})
    item = media(drive, project="553")
    calls = []
    quality = {"status": "degraded", "suspect_sec": 0, "suspect_ratio": 0,
               "gaps_sec": 152.17, "thresholds": {"gaps_sec_max": 30}}

    def degraded(audio, output, cfg, config_path, pid):
        transcript, _ = fake_asr(calls)(audio, output, cfg, config_path, pid)
        (output / (audio.stem + "-run-meta.json")).write_text(
            json.dumps({"status": "degraded", "quality": quality}), encoding="utf-8")
        return transcript, {"status": "degraded", "quality": quality}

    drive.fail_copy_once = True
    with pytest.raises(RuntimeError, match="interrupted"):
        hub_worker.process_one(drive, item, tmp_path, {"hub_root": "/mnt/hub"},
                               tmp_path / "node.json", transcribe=degraded)
    assert drive.state("553", "voice.m4a")["status"] == "awaiting-publication"
    journal = next(tmp_path.glob("intake/553/*/*/job.json"))
    assert json.loads(journal.read_text())["transcript_quality"] == {
        "status": "degraded", "suspect_sec": 0, "suspect_ratio": 0,
        "gaps_sec": 152.17, "thresholds": {"gaps_sec_max": 30}}

    state = hub_worker.process_one(drive, media(drive, project="553"), tmp_path,
                                   {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                                   transcribe=degraded)
    assert len(calls) == 1
    assert state["status"] == "asr-done" and state["publication_verified"] is True
    assert state["transcript_quality"]["status"] == "degraded"
    assert state["transcript_quality"]["gaps_sec"] == 152.17
    assert drive.state("553", "voice.m4a")["transcript_quality"] == state["transcript_quality"]
    assert any(path.endswith("pipeline/state.json") and
               json.loads(blob)["transcript_quality"] == state["transcript_quality"]
               for path, blob in drive.files.items())
    assert any(path.endswith("-run-meta.json") and
               json.loads(blob)["status"] == "degraded"
               for path, blob in drive.files.items())


@pytest.mark.parametrize("status,quality", [
    ("error", {}), ("degraded", None), ("degraded", {"status": "ok"}),
])
def test_incomplete_asr_is_not_published(tmp_path, status, quality):
    drive = MemoryDrive({"553/voice.m4a": b"audio"})

    def incomplete(audio, output, cfg, config_path, pid):
        transcript, _ = fake_asr([])(audio, output, cfg, config_path, pid)
        return transcript, {"status": status, "quality": quality}

    with pytest.raises(RuntimeError, match="transcriber returned|quality details"):
        hub_worker.process_one(drive, media(drive, project="553"), tmp_path,
                               {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                               transcribe=incomplete)
    assert drive.state("553", "voice.m4a")["status"] == "in-progress"
    assert not any(path.endswith("pipeline/state.json") for path in drive.files)


def test_publish_failure_resumes_without_repeating_asr(tmp_path):
    drive = MemoryDrive({"_test505/voice.m4a": b"fake audio"})
    item = media(drive)
    calls = []
    drive.fail_copy_once = True
    with pytest.raises(RuntimeError, match="interrupted"):
        hub_worker.process_one(drive, item, tmp_path, {"hub_root": "/mnt/hub"},
                               tmp_path / "node.json", transcribe=fake_asr(calls))
    assert drive.state("_test505", "voice.m4a")["status"] == "awaiting-publication"
    result = hub_worker.process_one(drive, media(drive), tmp_path, {"hub_root": "/mnt/hub"},
                                    tmp_path / "node.json", transcribe=fake_asr(calls))
    assert result["status"] == "asr-done" and len(calls) == 1


def test_copy_failure_resumes_without_false_in_progress(tmp_path):
    class FailingCopy(MemoryDrive):
        fail_download = True

        def call(self, *args):
            if args[0] == "copyto" and args[1].startswith("gdrive:") and self.fail_download:
                self.fail_download = False
                raise RuntimeError("download interrupted")
            return super().call(*args)

    drive = FailingCopy({"_test505/voice.m4a": b"audio"})
    with pytest.raises(RuntimeError, match="download interrupted"):
        hub_worker.process_one(drive, media(drive), tmp_path,
                               {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                               transcribe=fake_asr([]))
    assert drive.state("_test505", "voice.m4a")["status"] == "copying"
    result = hub_worker.process_one(drive, media(drive), tmp_path,
                                    {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                                    transcribe=fake_asr([]))
    assert result["status"] == "asr-done"


def test_corrupt_published_file_never_gets_a_green_source_marker(tmp_path):
    drive = MemoryDrive({"_test505/voice.m4a": b"fake audio"})
    drive.corrupt_output = True
    with pytest.raises(RuntimeError, match="checksum|truncated"):
        hub_worker.process_one(drive, media(drive), tmp_path,
                               {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                               transcribe=fake_asr([]))
    assert drive.state("_test505", "voice.m4a")["status"] == "awaiting-publication"


def test_duplicate_published_output_is_not_verified(tmp_path):
    class DuplicateOutput(MemoryDrive):
        def list(self, *parts, recursive=False):
            values = super().list(*parts, recursive=recursive)
            if recursive and "sessions" in parts:
                values.extend([entry for entry in values
                               if entry.get("Path", "").endswith("-transcript.md")])
            return values

    drive = DuplicateOutput({"_test505/voice.m4a": b"audio"})
    with pytest.raises(RuntimeError, match="duplicate Drive IDs"):
        hub_worker.process_one(drive, media(drive), tmp_path,
                               {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                               transcribe=fake_asr([]))
    assert drive.state("_test505", "voice.m4a")["status"] == "awaiting-publication"


def test_duplicate_and_bundle_inputs_are_quarantined():
    class DuplicateDrive(MemoryDrive):
        def list(self, *parts, recursive=False):
            return super().list(*parts, recursive=recursive) * 2
    with pytest.raises(ValueError, match="ambiguous Drive IDs"):
        media(DuplicateDrive({"_test505/voice.m4a": b"one"}))
    drive = MemoryDrive({"_test505/GMT20261017-100000_Recording.m4a": b"audio",
                         "_test505/GMT20261017-100000_Recording_avo_640x360.mp4": b"video"})
    with pytest.raises(ValueError, match="bundle"):
        media(drive, path="GMT20261017-100000_Recording.m4a")


def test_unrelated_duplicate_does_not_block_a_unique_file():
    class UnrelatedDuplicate(MemoryDrive):
        def list(self, *parts, recursive=False):
            values = super().list(*parts, recursive=recursive)
            if recursive:
                values.extend([entry for entry in values if entry.get("Path") == "other.m4a"])
            return values
    drive = UnrelatedDuplicate({"543/voice.m4a": b"audio", "543/other.m4a": b"other"})
    assert media(drive, project="543", path="voice.m4a").path == "voice.m4a"


def test_queue_inspection_includes_new_and_vtt_but_not_completed(tmp_path):
    old = json.dumps({"status": "asr-done"}).encode("utf-8")
    drive = MemoryDrive({"506/voice.m4a": b"new audio", "506/old.m4a": b"old audio",
                         "506/old.m4a.state.json": old,
                         "506/_506_inbox/w42/meeting.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n",
                         "506/notes.vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nNotes\n"})
    jobs, quarantine = hub_worker.pending_inputs(
        drive, "506", {"zoom_vtt_only_input": True}, tmp_path)
    assert {item.path for item, _ in jobs} == {"voice.m4a", "_506_inbox/w42/meeting.vtt"}
    assert quarantine == []


def test_historical_zoom_week_vtt_has_no_age_cutoff(tmp_path):
    class OldVtt(MemoryDrive):
        def list(self, *parts, recursive=False):
            values = super().list(*parts, recursive=recursive)
            for entry in values:
                if entry.get("Path") == "_Example-Course_inbox/w21/2026-05-25-lesson.vtt":
                    entry["ModTime"] = "2026-05-25T07:00:00Z"
            return values

    drive = OldVtt({"Example-Course/_Example-Course_inbox/w21/2026-05-25-lesson.vtt":
                    b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nOld cue\n"})
    jobs, held = hub_worker.pending_inputs(
        drive, "Example-Course", {"zoom_vtt_only_input": True}, tmp_path)
    assert [item.path for item, _ in jobs] == ["_Example-Course_inbox/w21/2026-05-25-lesson.vtt"]
    assert held == []


def test_unpaired_vtt_needs_explicit_manual_exception(tmp_path):
    drive = MemoryDrive({"33/GMT20260525-060146_Recording.transcript.vtt":
                         b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nOld cue\n"})
    cfg = {"zoom_vtt_only_input": True}
    jobs, held = hub_worker.pending_inputs(drive, "33", cfg, tmp_path)
    assert jobs == [] and held == []
    item = hub_worker.choose_input(drive, "33", "GMT20260525-060146_Recording.transcript.vtt",
                                   allow_vtt=True)
    with pytest.raises(ValueError, match="--manual-vtt"):
        hub_worker.require_vtt_route(item, cfg)
    hub_worker.require_vtt_route(item, cfg, manual=True)


def test_weekly_vtt_route_rejects_lookalikes_and_media_still_runs(tmp_path):
    cfg = {"zoom_vtt_only_input": True}
    assert hub_worker.zoom_dispatch_vtt("415", "_415_inbox/w42/meeting.vtt")
    for path in ("meeting.vtt", "_415_inbox/meeting.vtt",
                 "_415_inbox/W42/meeting.vtt", "_415_inbox/w54/meeting.vtt",
                 "_415_inbox/w42/deeper/meeting.vtt", "_416_inbox/w42/meeting.vtt"):
        assert not hub_worker.zoom_dispatch_vtt("415", path)
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSpeaker: hello\n"
    stem = "GMT20261019-110000_Recording"
    drive = MemoryDrive({f"415/_415_inbox/w42/{stem}.m4a": b"audio",
                         f"415/_415_inbox/w42/{stem}.transcript.vtt": vtt})
    jobs, held = hub_worker.pending_inputs(drive, "415", cfg, tmp_path)
    assert held == [] and len(jobs) == 1
    primary, peers = jobs[0]
    assert primary.path.endswith(".m4a")
    assert [peer.path for peer in peers] == [f"_415_inbox/w42/{stem}.transcript.vtt"]
    jobs, held = hub_worker.pending_inputs(drive, "415", {"zoom_vtt_only_input": False}, tmp_path)
    assert held == [] and len(jobs) == 1 and jobs[0][0].path == primary.path
    assert [peer.path for peer in jobs[0][1]] == [peers[0].path]


def test_queue_quarantines_duplicate_id_but_keeps_other_file(tmp_path):
    class DuplicateDrive(MemoryDrive):
        def list(self, *parts, recursive=False):
            values = super().list(*parts, recursive=recursive)
            if recursive:
                values.extend([e for e in values if e.get("Path") == "bad.m4a"])
            return values

    drive = DuplicateDrive({"543/bad.m4a": b"ambiguous", "543/good.m4a": b"good"})
    jobs, quarantine = hub_worker.pending_inputs(drive, "543", {}, tmp_path)
    assert [item.path for item, _ in jobs] == ["good.m4a"]
    assert any("duplicate Drive path" in reason for reason in quarantine)


def test_queue_does_not_hide_a_changed_source_behind_old_progress_marker(tmp_path):
    marker = json.dumps({"status": "awaiting-publication",
                         "source_revision": "an older revision"}).encode("utf-8")
    drive = MemoryDrive({"506/voice.m4a": b"new contents",
                         "506/voice.m4a.state.json": marker})
    jobs, quarantine = hub_worker.pending_inputs(drive, "506", {}, tmp_path)
    assert jobs == []
    assert any("revision differs" in reason for reason in quarantine)


def test_queue_admits_only_own_journaled_publication_retry(tmp_path):
    drive = MemoryDrive({"506/voice.m4a": b"audio"})
    drive.fail_copy_once = True
    with pytest.raises(RuntimeError, match="interrupted"):
        hub_worker.process_one(drive, media(drive, project="506"), tmp_path,
                               hub_worker.worker_config({"hub_root": "/mnt/hub"}),
                               tmp_path / "node.json",
                               transcribe=fake_asr([]))
    cache = tmp_path / "state-cache" / "506"
    jobs, quarantine = hub_worker.pending_inputs(
        drive, "506", {}, cache, worker_root=tmp_path)
    assert [item.path for item, _ in jobs] == ["voice.m4a"]
    assert quarantine == []
    _, held = hub_worker.pending_inputs(drive, "506", {}, cache)
    assert any("explicit resume" in reason for reason in held)


def test_failed_job_backoff_does_not_mark_source_done(tmp_path):
    drive = MemoryDrive({"506/voice.m4a": b"audio"})
    item = media(drive, project="506")
    drive.fail_copy_once = True
    with pytest.raises(RuntimeError):
        hub_worker.process_one(drive, item, tmp_path,
                               hub_worker.worker_config({"hub_root": "/mnt/hub"}),
                               tmp_path / "node.json",
                               transcribe=fake_asr([]))
    hub_worker.defer_failed_job(tmp_path, item, RuntimeError("temporary Drive issue"))
    jobs, _ = hub_worker.pending_inputs(drive, "506", {}, tmp_path / "cache", worker_root=tmp_path)
    assert jobs == []
    journal = next((tmp_path / "intake").rglob("job.json"))
    state = json.loads(journal.read_text(encoding="utf-8"))
    state["retry_not_before"] = (dt.datetime.now(dt.timezone.utc) -
                                 dt.timedelta(minutes=1)).isoformat()
    hub_worker.atomic_json(journal, state)
    jobs, _ = hub_worker.pending_inputs(drive, "506", {}, tmp_path / "cache", worker_root=tmp_path)
    assert [pending.path for pending, _ in jobs] == ["voice.m4a"]
    assert drive.state("506", "voice.m4a")["status"] != "asr-done"


def test_whole_hub_needs_recent_same_code_media_e2e(tmp_path, monkeypatch):
    drive = MemoryDrive({"506/voice.m4a": b"audio"})
    cfg = {"hub_root": "/mnt/hub", "zoom_vtt_only_input": True,
           "enable_multi_machine": True}
    monkeypatch.setattr(hub_worker, "legacy_inactive", lambda: None)
    with pytest.raises(RuntimeError, match="cutover blocked"):
        hub_worker.run_whole_hub(drive, tmp_path, cfg, tmp_path / "node.json", 1)
    item = media(drive, project="506")
    result = hub_worker.process_one(drive, item, tmp_path, cfg, tmp_path / "node.json",
                                    transcribe=fake_asr([]))
    hub_worker.media_e2e_receipt(tmp_path, cfg, item, result)
    hub_worker.require_media_e2e(drive, tmp_path, cfg)
    receipt_path = tmp_path / "media-e2e.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["code_sha256"] = "wrong code"
    hub_worker.atomic_json(receipt_path, receipt)
    with pytest.raises(RuntimeError, match="worker code"):
        hub_worker.require_media_e2e(drive, tmp_path, cfg)


def test_whole_hub_limits_processing_to_one_file_per_sweep(tmp_path, monkeypatch):
    drive = MemoryDrive({"506/proof.m4a": b"audio"})
    cfg = {"hub_root": "/mnt/hub", "zoom_vtt_only_input": True,
           "enable_multi_machine": True}
    proof = media(drive, project="506", path="proof.m4a")
    state = hub_worker.process_one(drive, proof, tmp_path, cfg, tmp_path / "node.json",
                                   transcribe=fake_asr([]))
    hub_worker.media_e2e_receipt(tmp_path, cfg, proof, state)
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"
    drive.files["506/_506_inbox/w42/lesson-a.vtt"] = vtt
    drive.files["506/_506_inbox/w42/lesson-b.vtt"] = vtt
    monkeypatch.setattr(hub_worker, "legacy_inactive", lambda: None)
    monkeypatch.setattr(hub_worker, "single_worker", lambda *_a: __import__("contextlib").nullcontext())
    monkeypatch.setattr(hub_worker, "run_claimed", lambda d, item, root, config, path, peers:
                        hub_worker.process_one(d, item, root, config, path, companions=peers))
    summary = hub_worker.run_whole_hub(drive, tmp_path, cfg, tmp_path / "node.json", 1)
    assert summary["attempted"] == summary["processed"] == 1
    assert summary["unscanned_projects"] == 0  # only one project in this fixture
    assert sum(path.endswith(".vtt.state.json") for path in drive.files) == 1


def test_media_only_sweep_keeps_vtt_as_sidecar_without_importing_it(tmp_path, monkeypatch):
    drive = MemoryDrive({"506/proof.m4a": b"proof audio"})
    cfg = {"hub_root": "/mnt/hub", "zoom_vtt_only_input": False,
           "enable_multi_machine": True}
    proof = media(drive, project="506", path="proof.m4a")
    state = hub_worker.process_one(drive, proof, tmp_path, cfg, tmp_path / "node.json",
                                   transcribe=fake_asr([]))
    hub_worker.media_e2e_receipt(tmp_path, cfg, proof, state)
    stem = "GMT20261019-110000_Recording"
    drive.files[f"506/_506_inbox/w42/{stem}.m4a"] = b"media"
    drive.files[f"506/_506_inbox/w42/{stem}.transcript.vtt"] = (
        b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nSpeaker: hello\n")
    drive.files["506/unpaired.vtt"] = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nOld\n"
    monkeypatch.setattr(hub_worker, "legacy_inactive", lambda: None)
    monkeypatch.setattr(hub_worker, "single_worker", lambda *_a: __import__("contextlib").nullcontext())
    calls = []
    monkeypatch.setattr(hub_worker, "run_claimed", lambda d, item, root, config, path, peers:
                        hub_worker.process_one(d, item, root, config, path,
                                               companions=peers, transcribe=fake_asr(calls)))
    summary = hub_worker.run_whole_hub(drive, tmp_path, cfg, tmp_path / "node.json", 1)
    assert summary["attempted"] == summary["processed"] == 1
    assert len(calls) == 1 and calls[0].suffix == ".m4a"
    assert f"506/_506_inbox/w42/{stem}.transcript.vtt.state.json" not in drive.files
    assert "506/unpaired.vtt.state.json" not in drive.files


def test_zoom_bundle_is_staged_together_and_transcribed_once(tmp_path):
    stem = "GMT20261018-120000_Recording"
    audio = f"{stem}.m4a"
    video = f"{stem}_avo_640x360.mp4"
    slides = f"{stem}_as_1920x1080.mp4"
    vtt = f"{stem}.transcript.vtt"
    drive = MemoryDrive({"_test505/" + name: content for name, content in (
        (audio, b"voice"), (video, b"small video"), (slides, b"screen video"),
        (vtt, b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTest: hello\n"))})
    primary, companions = hub_worker.choose_group(drive, "_test505", video, allow_vtt=True)
    assert primary.path == audio
    assert {item.path for item in companions} == {video, slides, vtt}
    calls = []

    def worker(source, output, cfg, config_path, pid):
        assert {p.name for p in source.parent.iterdir()} >= {audio, video, slides, vtt}
        transcript, meta = fake_asr(calls)(source, output, cfg, config_path, pid)
        (output / (source.stem + "-run-meta.json")).write_text(
            json.dumps({"status": "ok", "frames_source": str(source.parent / slides)}),
            encoding="utf-8")
        return transcript, meta

    result = hub_worker.process_one(drive, primary, tmp_path, {"hub_root": "/mnt/hub"},
                                    tmp_path / "node.json", transcribe=worker,
                                    companions=companions)
    assert result["status"] == "asr-done" and len(calls) == 1
    meta = next(json.loads(content) for path, content in drive.files.items()
                if path.endswith(audio.replace(".m4a", "-run-meta.json")))
    assert meta["frames_source"].replace("\\", "/") == f"/mnt/hub/_test505/{slides}"
    for path in (video, slides):
        state = json.loads(drive.files[f"_test505/{path}.state.json"])
        assert state["status"] == "bundle-sibling"
        assert state["bundle_primary"] == audio
    assert f"_test505/{vtt}.state.json" not in drive.files
    journal = next((tmp_path / "intake").rglob("job.json"))
    job = json.loads(journal.read_text(encoding="utf-8"))
    now = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
    job["published_at"] = (now - dt.timedelta(days=31)).isoformat()
    hub_worker.atomic_json(journal, job)
    result = hub_worker.cleanup_local(drive, tmp_path, dry_run=False, now=now)
    assert result[0]["action"] == "pruned"
    assert not (journal.parent / slides).exists()
    assert not list((tmp_path / "intake").rglob(slides))


def test_zoom_vtt_only_is_imported_without_running_asr(tmp_path):
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTest Speaker: hello\n"
    drive = MemoryDrive({"_test505/lesson.vtt": vtt})
    item = media(drive, path="lesson.vtt")
    result = hub_worker.process_one(drive, item, tmp_path,
                                    {"hub_root": "/mnt/hub", "zoom_vtt_only_input": True},
                                    tmp_path / "node.json")
    assert result["status"] == "asr-done"
    assert any(path.endswith("lesson-transcript.md") for path in drive.files)
    meta = next(data for name, data in drive.files.items()
                if name.endswith("lesson-run-meta.json"))
    assert str(tmp_path).encode() not in meta
    assert json.loads(meta)["source_file"].replace("\\", "/") == "/mnt/hub/_test505/lesson.vtt"


def test_shared_inbox_keeps_existing_shared_routing(tmp_path):
    drive = MemoryDrive({"_inbox/voice.m4a": b"audio"})
    item = media(drive, project="_inbox")
    result = hub_worker.process_one(drive, item, tmp_path,
                                    {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                                    transcribe=fake_asr([]))
    assert result["pid"] == "_shared"
    assert result["needs_classification"] is True
    assert result["transcript_path"].replace("\\", "/").startswith(
        "/mnt/hub/_shared/sessions/")
    assert "_inbox/voice.m4a.state.json" in drive.files


def test_nested_shared_inbox_without_mapper_is_quarantined(tmp_path):
    drive = MemoryDrive({"_inbox/w2/voice.m4a": b"audio"})
    with pytest.raises(ValueError, match="explicit mapper"):
        hub_worker.process_one(drive, media(drive, project="_inbox", path="w2/voice.m4a"),
                               tmp_path, {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                               transcribe=fake_asr([]))


def test_input_revision_change_cannot_mark_new_source_done(tmp_path):
    class ChangedDrive(MemoryDrive):
        def call(self, *args):
            result = super().call(*args)
            if args[0] == "copy":
                self.files["_test505/voice.m4a"] = b"different revision"
            return result

    drive = ChangedDrive({"_test505/voice.m4a": b"fake audio"})
    with pytest.raises(RuntimeError, match="revision changed"):
        hub_worker.process_one(drive, media(drive), tmp_path,
                               {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                               transcribe=fake_asr([]))
    assert drive.state("_test505", "voice.m4a")["status"] == "awaiting-publication"


def test_local_retention_only_after_thirty_days_and_reverified_output(tmp_path):
    drive = MemoryDrive({"_test505/voice.m4a": b"fake audio"})
    hub_worker.process_one(drive, media(drive), tmp_path,
                           {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                           transcribe=fake_asr([]))
    journal = next((tmp_path / "intake").rglob("job.json"))
    job = json.loads(journal.read_text(encoding="utf-8"))
    source = journal.parent / "voice.m4a"
    outputs = tmp_path / "outputs" / job["output"]
    now = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
    job["published_at"] = (now - dt.timedelta(days=29)).isoformat()
    hub_worker.atomic_json(journal, job)
    assert hub_worker.cleanup_local(drive, tmp_path, dry_run=False, now=now) == []
    assert source.is_file() and outputs.is_dir()

    job["published_at"] = (now - dt.timedelta(days=31)).isoformat()
    hub_worker.atomic_json(journal, job)
    assert hub_worker.cleanup_local(drive, tmp_path, dry_run=True, now=now)[0]["action"] == "eligible"
    assert source.is_file()
    result = hub_worker.cleanup_local(drive, tmp_path, dry_run=False, now=now)
    assert result[0]["action"] == "pruned"
    assert not source.exists() and not outputs.exists()
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "pruned"
    assert "_test505/voice.m4a" in drive.files  # Hub source is never deleted


def test_retention_holds_local_file_if_remote_marker_loses_verification(tmp_path):
    drive = MemoryDrive({"_test505/voice.m4a": b"fake audio"})
    hub_worker.process_one(drive, media(drive), tmp_path,
                           {"hub_root": "/mnt/hub"}, tmp_path / "node.json",
                           transcribe=fake_asr([]))
    journal = next((tmp_path / "intake").rglob("job.json"))
    now = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
    job = json.loads(journal.read_text(encoding="utf-8"))
    job["published_at"] = (now - dt.timedelta(days=31)).isoformat()
    hub_worker.atomic_json(journal, job)
    marker = json.loads(drive.files["_test505/voice.m4a.state.json"])
    marker["publication_verified"] = False
    drive.files["_test505/voice.m4a.state.json"] = json.dumps(marker).encode()
    result = hub_worker.cleanup_local(drive, tmp_path, dry_run=False, now=now)
    assert result[0]["action"] == "held"
    assert (journal.parent / "voice.m4a").exists()


def test_real_project_refuses_active_old_timer(monkeypatch):
    class Active:
        returncode = 0
    monkeypatch.setattr(hub_worker.os, "name", "posix")
    monkeypatch.setattr(hub_worker.subprocess, "run", lambda *_a, **_kw: Active())
    with pytest.raises(RuntimeError, match="legacy consumer"):
        hub_worker.legacy_inactive()


def test_shared_claim_is_checked_and_retired_only_after_publication(tmp_path, monkeypatch):
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTest: hello\n"
    drive = MemoryDrive({"506/lesson.vtt": vtt})
    item = media(drive, project="506", path="lesson.vtt")
    cfg = {"hub_root": "/mnt/hub", "enable_multi_machine": True,
           "zoom_vtt_only_input": True}
    claim = tmp_path / "claim.json"
    claim.write_text(json.dumps({"claim": {"claimed_by": "TEST-NODE"}}), encoding="utf-8")
    monkeypatch.setattr(hub_worker.os.path, "ismount", lambda _: True)
    monkeypatch.setattr(hub_worker.watcher, "try_claim_file", lambda *_a: True)
    monkeypatch.setattr(hub_worker.watcher, "claim_path", lambda *_a: claim)
    monkeypatch.setattr(hub_worker.watcher, "host_label_of", lambda *_a: "TEST-NODE")
    monkeypatch.setattr(hub_worker.watcher, "is_claim_expired", lambda *_a: False)

    class Heartbeat:
        def __init__(self, *_a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

    monkeypatch.setattr(hub_worker.watcher, "_ClaimHeartbeat", Heartbeat)
    retired = []
    monkeypatch.setattr(hub_worker.watcher, "retire_claim", lambda *_a: retired.append(True))
    result = hub_worker.run_claimed(drive, item, tmp_path, cfg, tmp_path / "config.json", [])
    assert result["status"] == "asr-done"
    assert retired == [True]


def test_worker_claimant_is_not_the_old_watchers_own_host():
    old = {"node": {"host_label": "GPU-NODE"}, "enable_multi_machine": True}
    new = hub_worker.worker_config(old)
    assert hub_worker.watcher.host_label_of(new) == "GPU-NODE-DRIVE"
    assert hub_worker.watcher.host_label_of(old) == "GPU-NODE"


def test_claim_denied_never_downloads_file(tmp_path, monkeypatch):
    drive = MemoryDrive({"506/voice.m4a": b"audio"})
    item = media(drive, project="506")
    monkeypatch.setattr(hub_worker.os.path, "ismount", lambda _: True)
    monkeypatch.setattr(hub_worker.watcher, "try_claim_file", lambda *_a: False)
    with pytest.raises(RuntimeError, match="held elsewhere"):
        hub_worker.run_claimed(drive, item, tmp_path,
                               {"hub_root": "/mnt/hub", "enable_multi_machine": True},
                               tmp_path / "config.json", [])
    assert not list(tmp_path.rglob("voice.m4a"))


def test_lost_claim_never_greenlights_source_or_retires_it(tmp_path, monkeypatch):
    vtt = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTest: hello\n"
    drive = MemoryDrive({"506/lesson.vtt": vtt})
    item = media(drive, project="506", path="lesson.vtt")
    cfg = {"hub_root": "/mnt/hub", "enable_multi_machine": True,
           "zoom_vtt_only_input": True}
    claim = tmp_path / "claim.json"
    claim.write_text(json.dumps({"claim": {"claimed_by": "TEST-NODE"}}), encoding="utf-8")
    monkeypatch.setattr(hub_worker.os.path, "ismount", lambda _: True)
    monkeypatch.setattr(hub_worker.watcher, "try_claim_file", lambda *_a: True)
    monkeypatch.setattr(hub_worker.watcher, "claim_path", lambda *_a: claim)
    monkeypatch.setattr(hub_worker.watcher, "host_label_of", lambda *_a: "TEST-NODE")
    calls = []

    def expired(*_args):
        calls.append(True)
        return len(calls) >= 4

    monkeypatch.setattr(hub_worker.watcher, "is_claim_expired", expired)

    class Heartbeat:
        def __init__(self, *_a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

    monkeypatch.setattr(hub_worker.watcher, "_ClaimHeartbeat", Heartbeat)
    retired = []
    monkeypatch.setattr(hub_worker.watcher, "retire_claim", lambda *_a: retired.append(True))
    with pytest.raises(RuntimeError, match="shared claim lost"):
        hub_worker.run_claimed(drive, item, tmp_path, cfg, tmp_path / "config.json", [])
    assert retired == []
    assert drive.state("506", "lesson.vtt")["status"] != "asr-done"


def test_isolated_vtt_requires_proof_from_running_watchers_config(tmp_path, monkeypatch):
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"sources": [{"root": "{hub_root}",
                                              "discover": "project-inboxes"}],
                               "scan_extensions": [".m4a", ".mp4"],
                               "zoom_vtt_only_input": False}), encoding="utf-8")
    class Service:
        returncode = 0
        stdout = "ExecStart=/usr/bin/python watcher.py --config " + str(old.resolve()) + " --once"
    monkeypatch.setattr(hub_worker.subprocess, "run", lambda *_a, **_kw: Service())
    item = MediaFile("_test505", "canary.vtt", "id", 10, "2026-10-18T10:00:00Z", "0" * 32, False)
    hub_worker.isolated_vtt_canary(item, old)
    old.write_text(json.dumps({"sources": [{"root": "{hub_root}",
                                              "discover": "project-inboxes"}],
                               "scan_extensions": [".vtt"]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="can discover VTT"):
        hub_worker.isolated_vtt_canary(item, old)
    with pytest.raises(ValueError, match="reserved"):
        hub_worker.isolated_vtt_canary(MediaFile("506", "canary.vtt", "id", 10, "", "0" * 32, False), old)
