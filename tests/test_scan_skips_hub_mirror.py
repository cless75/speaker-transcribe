"""Зеркало тяжёлого в Hub не должно возвращаться во вход.

Канон Hub 258-a кладёт исходники и выдачу в ``{hub}/{pid}/process`` и
``{hub}/{pid}/delivery``. Расширения там те же, что у дропов, поэтому без явного
пропуска рекурсивный обход папки проекта ставит зеркало в очередь на распознавание —
и на облачном диске упирается в dataless-поддерево (наблюдалось 19.09.2026: прогон
час вместо 16 с, дважды OSError 22, дроп в ``_PE45_inbox`` не взят).
"""
from __future__ import annotations

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
