"""FUSE metadata failures and partially published worker output (801-o17)."""
import errno
import json
import os
import sys

import pytest

import audio_inbox_watch as watcher
import media_transcribe as worker


@pytest.mark.parametrize("copy", [worker.copy2_tolerant, watcher._copy_with_edeadlk_retry])
def test_xattr_eio_preserves_payload_and_mtime(tmp_path, monkeypatch, copy):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"complete payload")
    stamp = 1_700_000_000_123_456_700
    os.utime(source, ns=(stamp, stamp))

    def no_xattr(*args, **kwargs):
        raise OSError(errno.EIO, "listxattr refused by FUSE")

    monkeypatch.setattr(worker.shutil, "copystat", no_xattr)
    copy(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mtime_ns == source.stat().st_mtime_ns


@pytest.mark.parametrize("copy", [worker.copy2_tolerant, watcher._copy_with_edeadlk_retry])
def test_payload_eio_is_not_swallowed(tmp_path, monkeypatch, copy):
    def broken_payload(*args, **kwargs):
        raise OSError(errno.EIO, "payload write failed")

    monkeypatch.setattr(worker.shutil, "copyfile", broken_payload)
    with pytest.raises(OSError, match="payload write failed"):
        copy(tmp_path / "source", tmp_path / "target")


@pytest.mark.parametrize("code, marker, result, fatal, transcript, expected", [
    (1, False, False, True, True, False),  # observed FUSE write_outputs crash
    (1, False, False, False, True, False),
    (1, True, False, False, True, False),  # marker alone is not the final JSON
    (1, False, True, False, True, False),
    (1, True, True, True, True, False),
    (1, True, True, False, True, True),  # preserve confirmed late/atexit recovery
    (0, True, True, False, True, True),
    (0, True, True, False, False, False),
])
def test_real_subprocess_completion_contract(
        tmp_path, code, marker, result, fatal, transcript, expected):
    """Exercise Popen, stdout/stderr capture and disk discovery without a model."""
    cli = tmp_path / "worker.py"
    output = tmp_path / "out"
    cli.write_text(
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output-dir') + 1])\n"
        + ("(out / 'clip-transcript.md').write_text('partial or complete')\n" if transcript else "")
        + ("print('phase=stdout_json ok', file=sys.stderr)\n" if marker else "")
        + ("print(json.dumps({'status': 'ok', 'outputs': {'md': 'clip-transcript.md'}}))\n" if result else "")
        + ("print('fatal_error: AttributeError: copytime', file=sys.stderr)\n" if fatal else "")
        + f"sys.exit({code})\n", encoding="utf-8")
    ok, error, path, meta = watcher.run_asr(
        tmp_path / "clip.mp3", {"transcribe_python": sys.executable,
                                "transcribe_cli": str(cli), "speaker_mode": "off"},
        "test", tmp_path, output, tmp_path / "config.json")
    assert ok is expected
    assert (path is not None) is expected
    assert watcher.asr_log_path(tmp_path / "clip.mp3").exists()


def test_failed_worker_is_retried_not_adopted(tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"audio")
    output, state_dir = tmp_path / "transcripts", tmp_path / "pipeline"
    output.mkdir()
    transcript = output / "clip-transcript.md"
    monkeypatch.setattr(watcher, "resolve_session_dirs",
                        lambda *a: ("test-sid", {}, output, state_dir))
    monkeypatch.setattr(watcher, "wait_for_cpu_available", lambda *a: (True, ""))
    monkeypatch.setattr(watcher, "status_event", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "publish_status", lambda *a, **k: None)
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs.get("execution_mode"))
        transcript.write_text("partial" if len(calls) == 1 else "complete")
        if len(calls) == 1:
            return False, "fatal_error: copytime", None, None
        return True, "", transcript, {"status": "ok"}

    monkeypatch.setattr(watcher, "run_asr", run)
    cfg = {"voiceprint_mode": "off", "respect_cpu_load": False,
           "cleanup_intermediates": False}
    for i in range(2):
        watcher.process_one_file(audio, tmp_path, {"route": "literal", "project": "test"},
                                 cfg, {}, tmp_path / "config.json")
        state = json.loads(watcher.state_path(audio).read_text(encoding="utf-8"))
        assert state["status"] == ("queued" if i == 0 else "asr-done")
        assert state["worker_completed"] is (i == 1)
        assert watcher.marker_processed_asr(audio).exists() is (i == 1)
    assert calls == [None, None]
    assert transcript.read_text() == "complete"
