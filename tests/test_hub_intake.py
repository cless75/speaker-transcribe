import hashlib
import json
import pathlib
import sys

import pytest

import hub_intake
from hub_intake import (Drive, MediaFile, hygiene_candidates, marked_vtt_inventory,
                        media_inventory, projects, publish_page, render, safe_parts,
                        snapshot, stage)


def entry(path, *, ident="file-1", size=4, md5=None):
    return {"Path": path, "ID": ident, "Name": pathlib.PurePosixPath(path).name,
            "Size": size, "ModTime": "2026-10-17T09:00:00Z", "IsDir": False,
            "Hashes": {"md5": md5} if md5 else {}}


def test_entire_hub_includes_completed_and_named_projects():
    root = [{"Name": "506", "IsDir": True}, {"Name": "Example-Course", "IsDir": True},
            {"Name": "_meta", "IsDir": True}, {"Name": "_inbox", "IsDir": True},
            {"Name": "old-completed", "IsDir": True}]
    assert projects(root) == ["506", "_inbox", "Example-Course", "old-completed"]


def test_inventory_respects_watcher_skips_and_keeps_markers():
    files = [entry("Lessons/meeting.m4a"), entry("Lessons/meeting.m4a.state.json"),
             entry("sessions/output.mp4"), entry("_failed/old.mp3"),
             entry("_Example-Course_inbox/w2/zoom.m4a", ident="inbox"),
             entry("_Example-Course_inbox/_failed/bad.m4a", ident="failed"),
             entry("Topic-A/intro.mp4", ident="video"), entry("Lessons/readme.pdf")]
    items = media_inventory("Example-Course", files)
    assert [(i.path, i.marker) for i in items] == [
        ("Lessons/meeting.m4a", True),
        ("_Example-Course_inbox/w2/zoom.m4a", False),
        ("Topic-A/intro.mp4", False)]
    with pytest.raises(ValueError, match="unsafe"):
        safe_parts("../other/meeting.m4a")


def test_hygiene_reports_duplicates_and_orphan_without_deleting():
    entries = [entry("a.m4a", md5="e" * 32),
               entry("b.m4a", ident="two", md5="e" * 32),
               entry("missing.m4a.state.json")]
    found = hygiene_candidates("Example-Course", entries, media_inventory("Example-Course", entries))
    assert {x["reason"] for x in found} == {
        "same-content-within-project", "orphan-state-marker"}


class FakeDrive(Drive):
    def __init__(self, contents=b"data", state=None):
        super().__init__("gdrive:Hub")
        self.contents = contents
        self.state_value = state
        self.copies = 0

    def call(self, *args):
        assert args[0] == "copyto"
        self.copies += 1
        pathlib.Path(args[2]).write_bytes(self.contents)
        return b""

    def list(self, *parts, recursive=False):
        return [entry("Lesson/audio.m4a", md5=hashlib.md5(b"data").hexdigest()),
                entry("Lesson/audio.m4a.state.json")]

    def state(self, project, path):
        return self.state_value or {"status": "in-progress", "started_at": "2026-10-17"}

    def states_bulk(self, project, target):
        marker = target / "Lesson" / "audio.m4a.state.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(self.state(project, "Lesson/audio.m4a")), encoding="utf-8")


def test_stage_validates_checksum_and_reuses_revision(tmp_path):
    item = MediaFile("Example-Course", "Lesson/audio.m4a", "file-1", 4,
                     "2026-10-17T09:00:00Z", hashlib.md5(b"data").hexdigest(), False)
    drive = FakeDrive()
    dest = stage(drive, item, tmp_path)
    assert dest.read_bytes() == b"data"
    assert json.loads((dest.parent / "delivery.json").read_text(encoding="utf-8"))["id"] == "file-1"
    assert stage(drive, item, tmp_path) == dest
    assert drive.copies == 1
    dest.write_bytes(b"oops")
    with pytest.raises(ValueError, match="corrupt"):
        stage(drive, item, tmp_path)


def test_stage_rejects_bad_transfer_without_exposing_file(tmp_path):
    item = MediaFile("Example-Course", "Lesson/audio.m4a", "file-1", 4,
                     "2026-10-17T09:00:00Z", hashlib.md5(b"data").hexdigest(), False)
    with pytest.raises(ValueError, match="checksum"):
        stage(FakeDrive(contents=b"evil"), item, tmp_path)
    assert not list(tmp_path.rglob("delivery.json"))
    assert not list(tmp_path.rglob("audio.m4a"))


def test_html_escapes_untrusted_names_and_marks_partial_scan():
    item = MediaFile("Example-Course", "<script>.mp3", "id", 4, "", "0" * 32, False)
    page = render("Example-Course", [{"file": item, "status": "unknown"}],
                  checked_at="today", complete=False)
    assert "&lt;script&gt;" in page and "<script>" not in page
    assert "НЕПОЛНЫЙ ОБХОД" in page
    assert 'name="artifact-version" content="0.1.0"' in page
    assert "https://drive.google.com/file/d/id/view" in page


def test_snapshot_does_not_call_legacy_done_a_verified_publication():
    drive = FakeDrive(state={"status": "asr-done", "finished_at": "2026-10-15"})
    page, report = snapshot(drive, "Example-Course")
    assert "asr-done (публикация не проверена)" in page
    assert report["complete"] is True
    assert report["counts"] == {"asr-done (публикация не проверена)": 1}


def test_verified_publication_is_linked_in_project_html():
    drive = FakeDrive(state={"status": "asr-done", "publication_verified": True,
                             "transcript_url": "https://drive.google.com/file/d/transcript-id/view"})
    page, report = snapshot(drive, "Example-Course")
    assert report["counts"] == {"asr-done": 1}
    assert 'href="https://drive.google.com/file/d/transcript-id/view"' in page


def test_marked_standalone_vtt_visible_but_media_sidecar_is_not():
    files = [entry("w2/GMT20261018-115500_Recording.m4a"),
             entry("w2/GMT20261018-115500_Recording.transcript.vtt"),
             entry("w2/GMT20261018-115500_Recording.transcript.vtt.state.json"),
             entry("w2/standalone.vtt", ident="vtt"),
             entry("w2/standalone.vtt.state.json")]
    assert [item.path for item in marked_vtt_inventory("506", files)] == ["w2/standalone.vtt"]


def test_snapshot_unknown_state_is_not_empty_queue():
    drive = FakeDrive(state={"status": "unknown"})
    page, report = snapshot(drive, "Example-Course")
    assert report["complete"] is False
    assert "НЕПОЛНЫЙ ОБХОД" in page


def test_cached_marker_must_match_drive_checksum(tmp_path):
    class CheckedDrive(FakeDrive):
        def list(self, *parts, recursive=False):
            marker = json.dumps(self.state_value).encode("utf-8")
            entries = super().list(*parts, recursive=recursive)
            entries[1]["Hashes"] = {"md5": hashlib.md5(marker).hexdigest()}
            return entries

    drive = CheckedDrive(state={"status": "in-progress"})
    _, report = snapshot(drive, "Example-Course", state_cache=tmp_path)
    assert report["counts"] == {"in-progress": 1}
    drive = CheckedDrive(state={"status": "asr-done"})
    def bad_sync(project, target):
        pass  # stale local cache must not produce a false completed result
    drive.states_bulk = bad_sync
    page, report = snapshot(drive, "Example-Course", state_cache=tmp_path)
    assert report["complete"] is False
    assert "НЕПОЛНЫЙ ОБХОД" in page


def test_bulk_filter_includes_markers_at_project_root(tmp_path):
    class CaptureDrive(Drive):
        def __init__(self):
            super().__init__("gdrive:Hub")
            self.args = ()

        def call(self, *args):
            self.args = args
            return b""

    drive = CaptureDrive()
    drive.states_bulk("33", tmp_path)
    filters = [drive.args[i + 1] for i, arg in enumerate(drive.args[:-1])
               if arg == "--filter"]
    assert filters.index("+ *.state.json") < filters.index("- **")
    assert filters.index("+ **/*.state.json") < filters.index("- **")


def test_publish_never_overwrites_an_unmanaged_html(tmp_path):
    class OtherPage(Drive):
        def __init__(self):
            super().__init__("gdrive:Hub")
            self.writes = 0

        def list(self, *parts, recursive=False):
            return [{"Name": "index.html"}]

        def call(self, *args):
            if args[0] == "cat":
                return b"<h1>private hand-written page</h1>"
            self.writes += 1
            return b""

    drive = OtherPage()
    with pytest.raises(RuntimeError, match="unmanaged"):
        publish_page(drive, "Example-Course", tmp_path / "index.html")
    assert drive.writes == 0


def test_duplicate_names_in_drive_cannot_be_staged_ambiguously():
    with pytest.raises(ValueError, match="duplicate Drive paths"):
        media_inventory("700", [entry("_700_inbox/voice.m4a", ident="id-one"),
                                entry("_700_inbox/voice.m4a", ident="id-two")])


def test_snapshot_shows_both_drive_ids_without_hiding_other_media():
    class Duplicates(FakeDrive):
        def list(self, *parts, recursive=False):
            return [entry("lesson.mp3", ident="normal"),
                    entry("same.mp3", ident="id-one"),
                    entry("same.mp3", ident="id-two")]

    page, report = snapshot(Duplicates(), "700")
    assert report["complete"] is False
    assert report["counts"] == {"new": 1, "ambiguous-duplicate": 2}
    assert "https://drive.google.com/file/d/id-one/view" in page
    assert "https://drive.google.com/file/d/id-two/view" in page
    assert "НЕПОЛНЫЙ ОБХОД" in page
    assert any(c["reason"] == "duplicate-drive-path"
               for c in report["hygiene_candidates"])


def test_single_project_run_preserves_previous_hub_hygiene(tmp_path, monkeypatch):
    report_path = tmp_path / "hub-hygiene.json"
    report_path.write_text(json.dumps([{"project": "506", "complete": False,
                                        "error": "previous timeout"}]), encoding="utf-8")
    monkeypatch.setattr(hub_intake, "snapshot", lambda *_args, **_kwargs: (
        render("Example-Course", [], checked_at="now", complete=True),
        {"project": "Example-Course", "complete": True, "counts": {}}))
    monkeypatch.setattr(sys, "argv", ["hub_intake.py", "--project", "Example-Course",
                                  "--output-dir", str(tmp_path)])
    assert hub_intake.main() == 0
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert [row["project"] for row in saved] == ["506", "Example-Course"]
    assert saved[0]["error"] == "previous timeout"


def test_corrupt_hygiene_report_is_not_discarded(tmp_path, monkeypatch):
    report = tmp_path / "hub-hygiene.json"
    report.write_text("not JSON", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["hub_intake.py", "--project", "506",
                                  "--output-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        hub_intake.main()
    assert report.read_text(encoding="utf-8") == "not JSON"
