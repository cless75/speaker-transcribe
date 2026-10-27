import hashlib
import json
import shutil
import wave

import pytest

from zoom_bundle import validate_bundle

START = "2026-10-20T10:00:00Z"
END = "2026-10-20T10:01:00Z"


def entry(path="files/audio.m4a", role="audio_only", start=START, end=END, identity="audio"):
    return {"id": identity, "path": path, "role": role, "size": 4,
            "sha256": hashlib.sha256(b"data").hexdigest(),
            "participant_audio": role == "participant_audio",
            "recording_start": start, "recording_end": end}


def bundle(root, entries=None):
    entries = [entry()] if entries is None else entries
    root.mkdir(exist_ok=True)
    (root / "READY").write_bytes(b"")
    for item in entries:
        path = root / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")
    manifest = {"schema_version": 1, "meeting_uuid": "synthetic-meeting",
                "meeting_start": START, "completeness": "observed_api_inventory", "files": entries}
    (root / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def probe(_):
    return {"audio_streams": 1, "duration": 60.0}


def validate(root, **kwargs):
    return validate_bundle(root, allowed_root=root.parent, probe=kwargs.get("probe", probe))


def rewrite(root, manifest):
    (root / "bundle.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_mixed_audio_wins_over_video_and_interpretation(tmp_path):
    entries = [entry(), entry("files/view.mp4", "active_speaker", identity="video"),
               entry("files/translation.m4a", "audio_interpretation", identity="translation")]
    root = tmp_path / "meeting"
    bundle(root, entries)
    calls = []
    report = validate(root, probe=lambda p: (calls.append(p.name) or probe(p)))
    assert report.status == "validated"
    assert report.integrity_verified
    assert report.plan["mode"] == "mixed_audio"
    assert calls == ["audio.m4a"]
    assert len(report.plan["units"]) == 1


def test_speaker_tracks_preferred_but_not_falsely_ready(tmp_path):
    root = tmp_path / "meeting"
    bundle(root, [entry(), entry("Audio Record/person.m4a", "participant_audio", identity="person")])
    report = validate(root)
    assert report.integrity_verified
    assert report.status == "held"
    assert report.plan["mode"] == "participant_tracks"
    assert report.plan["units"][0]["id"] == "person"
    assert "participant_coverage_unverified" in report.reasons
    assert "track_alignment_unverified" in report.reasons
    assert report.plan["speaker_identity_verified"] is False


def test_two_sequential_segments_keep_offsets(tmp_path):
    root = tmp_path / "meeting"
    bundle(root, [entry(), entry("files/part2.m4a", start=END, end="2026-10-20T10:02:00Z", identity="part2")])
    report = validate(root)
    assert report.status == "validated"
    assert [u["offset_seconds"] for u in report.plan["units"]] == [0, 60]


def test_mixed_track_does_not_hide_later_video(tmp_path):
    root = tmp_path / "meeting"
    bundle(root, [entry(), entry("files/part2.mp4", "active_speaker", END, "2026-10-20T10:02:00Z", "part2")])
    report = validate(root)
    assert report.status == "held"
    assert "selected_media_incomplete" in report.reasons


def test_alternative_videos_not_transcribed_twice(tmp_path):
    root = tmp_path / "meeting"
    bundle(root, [entry("files/a.mp4", "active_speaker"),
                  entry("files/b.mp4", "gallery_view", identity="other")])
    report = validate(root)
    assert report.status == "held"
    assert "ambiguous_overlapping_media" in report.reasons


def test_single_video_fallback_without_vtt(tmp_path):
    root = tmp_path / "meeting"
    bundle(root, [entry("files/a.mp4", "active_speaker")])
    report = validate(root)
    assert report.status == "validated"
    assert report.plan["mode"] == "video_fallback"
    assert report.plan["units"][0]["speaker_source"] == "profile"


@pytest.mark.parametrize("info,reason", [
    ({"audio_streams": 0, "duration": 60}, "missing_or_ambiguous_audio_stream"),
    ({"audio_streams": 2, "duration": 60}, "missing_or_ambiguous_audio_stream"),
    ({"audio_streams": 1, "duration": float("nan")}, "missing_or_ambiguous_audio_stream"),
    ({"audio_streams": 1, "duration": 15}, "media_duration_mismatch"),
])
def test_bad_media_held_even_with_correct_hashes(tmp_path, info, reason):
    root = tmp_path / "meeting"
    bundle(root)
    report = validate(root, probe=lambda _: info)
    assert report.integrity_verified and report.status == "held"
    assert reason in report.reasons


@pytest.mark.parametrize("end", [None, "0001-01-01T00:00:00Z", "2026-10-20T10:01:00", START])
def test_unknown_or_naive_timeline_held(tmp_path, end):
    root = tmp_path / "meeting"
    bundle(root, [entry(end=end)])
    report = validate(root)
    assert report.status == "held" and report.integrity_verified
    assert "timeline_unresolved" in report.reasons


@pytest.mark.parametrize("value", ["../outside.m4a", "/outside.m4a", "C:/file.m4a",
                                   "files\\audio.m4a", "files/../audio.m4a", "files/NUL.m4a"])
def test_manifest_paths_cannot_escape(tmp_path, value):
    root = tmp_path / "meeting"
    m = bundle(root)
    m["files"][0]["path"] = value
    rewrite(root, m)
    report = validate(root)
    assert report.status == "held" and not report.integrity_verified
    assert "unsafe_member_path" in report.reasons


def test_duplicate_manifest_keys_rejected(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    (root / "bundle.json").write_text('{"schema_version":1,"schema_version":1}')
    assert "duplicate_json_key" in validate(root).reasons


def test_corrupt_content_and_unlisted_member_held(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    (root / "files/audio.m4a").write_bytes(b"evil")
    assert "member_checksum_mismatch" in validate(root).reasons
    (root / "files/audio.m4a").write_bytes(b"data")
    (root / "files/extra.m4a").write_bytes(b"data")
    assert "unlisted_bundle_members" in validate(root).reasons


def test_same_ready_raw_bundle_creates_new_revision(tmp_path):
    root = tmp_path / "meeting"
    root.mkdir()
    (root / "READY").write_bytes(b"")
    (root / "audio.m4a").write_bytes(b"first")
    first = validate(root)
    (root / "audio.m4a").write_bytes(b"second")
    second = validate(root)
    assert first.revision != second.revision
    assert first.integrity_verified and second.integrity_verified
    assert first.status == second.status == "held"  # raw directory has no timeline evidence


def test_generated_output_does_not_change_input_revision(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    before = validate(root)
    generated = root / "audio.m4a.asr/v1"
    generated.mkdir(parents=True)
    (generated / "transcript.txt").write_text("synthetic")
    assert validate(root).revision == before.revision


def test_source_mutation_during_probe_fails_snapshot(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    def changing(path):
        path.write_bytes(b"changed")
        return probe(path)
    report = validate(root, probe=changing)
    assert not report.integrity_verified
    assert "bundle_changed_during_validation" in report.reasons


def test_missing_ready_and_allowed_root_are_gates(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    (root / "READY").unlink()
    assert "missing_or_invalid_ready" in validate(root).reasons
    other = tmp_path / "other"
    other.mkdir()
    assert "bundle_outside_allowed_root" in validate_bundle(root, allowed_root=other).reasons


def test_symlink_held(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    try:
        (root / "files/link.m4a").symlink_to(root / "files/audio.m4a")
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert "symlink_or_reparse_point" in validate(root).reasons


def test_case_collision_held_on_case_sensitive_filesystem(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    other = root / "files/AUDIO.m4a"
    other.write_bytes(b"data")
    if len(list((root / "files").iterdir())) == 1:
        pytest.skip("case-insensitive filesystem")
    assert "ambiguous_normalized_path" in validate(root).reasons


def test_ready_directory_is_not_a_marker(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    (root / "READY").unlink()
    (root / "READY").mkdir()
    assert validate(root).status == "held"


def test_changed_manifest_not_accepted_as_automatic_revision(tmp_path):
    root = tmp_path / "meeting"
    bundle(root)
    (root / "files/audio.m4a").write_bytes(b"evil")
    # Automatic revisions do not license ignoring contradictory manifest hashes.
    assert "member_checksum_mismatch" in validate(root).reasons


def test_late_participant_offsets_not_flattened(tmp_path):
    root = tmp_path / "meeting"
    bundle(root, [entry("Audio Record/a.m4a", "participant_audio", identity="a"),
                  entry("Audio Record/b.m4a", "participant_audio", END,
                        "2026-10-20T10:02:00Z", "b")])
    report = validate(root)
    assert [u["offset_seconds"] for u in report.plan["units"]] == [0, 60]
    assert report.status == "held"  # offsets remain proposals until alignment evidence exists


def test_duplicate_ids_and_role_mismatch(tmp_path):
    root = tmp_path / "meeting"
    m = bundle(root, [entry(), entry("files/b.m4a")])
    assert "invalid_or_duplicate_member" in validate(root).reasons
    m["files"] = [entry("files/audio.m4a", "active_speaker")]
    (root / "files/b.m4a").unlink()
    rewrite(root, m)
    assert "role_media_type_mismatch" in validate(root).reasons


def test_summary_declared_size_is_not_actual_size(tmp_path):
    root = tmp_path / "meeting"
    summary = entry("files/summary.json", "summary", identity="summary")
    summary["api_declared_size"] = 900
    bundle(root, [entry(), summary])
    assert validate(root).status == "validated"


def test_real_ffprobe_on_synthetic_pcm(tmp_path):
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe unavailable")
    root = tmp_path / "meeting"
    m = bundle(root, [entry("files/audio.wav", end="2026-10-20T10:00:01Z")])
    path = root / "files/audio.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 16000)
    m["files"][0]["size"] = path.stat().st_size
    m["files"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    rewrite(root, m)
    report = validate_bundle(root, allowed_root=tmp_path)
    assert report.status == "validated", report.reasons
