"""Гейт run-meta-индекса по размеру дерева (13.10, 801-o17).

Клин pickup: ``sessions_root.rglob("*-run-meta.json")`` через rclone-FUSE занял
> 5 мин при watchdog 120 с (Gelya-Asr: 838 run-meta в 134 sid-каталогах).
Фикс (``org/801-pickup-runmeta-index-out-of-hotpath``): при дереве больше
``max_sids`` индекс не строится — catch-up остаётся на точечных проверках
того же sid (прямые glob'ы в ``existing_transcript``). Замер: прямой rclone
``lsf -R`` всего дерева — ~90 с, FUSE-листинг ~30x дороже API.
"""
import json
import pathlib

import pytest

from src import audio_inbox_watch as watcher


@pytest.fixture(autouse=True)
def _clear_index_cache():
    watcher._SESSIONS_INDEX_CACHE.clear()
    yield
    watcher._SESSIONS_INDEX_CACHE.clear()


def _session(hub: pathlib.Path, pid: str, month: str, sid: str, source_name: str) -> pathlib.Path:
    """Полная сессия: run-meta (source_file) + непустой транскрипт."""
    d = hub / pid / "sessions" / month / sid
    (d / "transcripts").mkdir(parents=True, exist_ok=True)
    meta = {"source_file": str(pathlib.PurePosixPath(hub) / pid / "inbox" / source_name)}
    (d / "transcripts" / f"{sid}-run-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    tr = d / "transcripts" / f"{sid}-transcript.md"
    tr.write_text("transcript\n", encoding="utf-8")
    return tr


def test_small_tree_builds_full_index(tmp_path):
    hub = tmp_path / "hub"
    tr = _session(hub, "478", "2026-08", "S20260810T2110-platforma", "Запись 260817_120718.m4a")
    index = watcher._sessions_run_meta_index(hub / "478" / "sessions")
    assert index == {"Запись 260817_120718.m4a": tr}


def test_gated_tree_skips_rglob(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    sessions_root = hub / "gelya" / "sessions"
    for i in range(31):  # 31 > дефолтный лимит 30
        _session(hub, "gelya", "2026-10", f"S20261012T{i:04d}-lesson-{i}", f"f{i}.mp3")

    def boom(*_a, **_k):
        raise AssertionError("rglob не должен выполняться на гейтованном дереве")

    monkeypatch.setattr(pathlib.Path, "rglob", boom)
    assert watcher._sessions_run_meta_index(sessions_root) == {}


def test_gate_respects_max_sids_override(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    sessions_root = hub / "478" / "sessions"
    for i in range(31):
        _session(hub, "478", "2026-10", f"S20261013T{i:04d}-x-{i}", f"f{i}.mp3")
    hits = []
    real_rglob = pathlib.Path.rglob

    def spy(self, pattern, *_a, **_k):
        hits.append(pattern)
        return real_rglob(self, pattern)

    monkeypatch.setattr(pathlib.Path, "rglob", spy)
    index = watcher._sessions_run_meta_index(sessions_root, max_sids=1000)
    assert hits  # с поднятым лимитом rglob снова работает
    assert any(pat == "*-run-meta.json" for pat in hits)
    assert len(index) == 31


def test_count_never_descends_into_session_contents(tmp_path, monkeypatch):
    """Гейт считает только 2 уровня (месяцы -> sid), внутрь сессий не ходит."""
    hub = tmp_path / "hub"
    sessions_root = hub / "gelya" / "sessions"
    for i in range(31):
        _session(hub, "gelya", "2026-10", f"S20261012T{i:04d}-lesson-{i}", f"f{i}.mp3")

    real_glob = pathlib.Path.glob

    def guard(self, pattern, **_k):
        # любой glob глубже уровня sid  запрещён внутри гейта
        rel = self.relative_to(sessions_root)
        assert len(rel.parts) <= 1, f"заход в сессию: {rel}"
        return real_glob(self, pattern)

    monkeypatch.setattr(pathlib.Path, "glob", guard)
    assert watcher._sessions_run_meta_index(sessions_root) == {}


def test_catchup_same_sid_survives_the_gate(tmp_path, monkeypatch):
    """Большое дерево: catch-up того же sid работает по прямым glob'ам."""
    hub = tmp_path / "hub"
    for i in range(20):
        _session(hub, "gelya", "2026-09", f"S20260910T{i:04d}-a-{i}", f"a{i}.m4a")
    own = _session(hub, "gelya", "2026-10", "S20261013T1117-course-overview",
                   "Course overview.mp3")
    # имя транскрипта — как у воркера (kebab-слаг от slugify_from_filename)
    tr = own.parent / "course-overview-transcript.md"
    tr.write_text("transcript\n", encoding="utf-8")

    audio = hub / "gelya" / "inbox" / "Course overview.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake")

    def boom(*_a, **_k):
        raise AssertionError("rglob не должен выполняться на гейтованном дереве")

    monkeypatch.setattr(pathlib.Path, "rglob", boom)
    hit = watcher.existing_transcript(audio, tr.parent)
    assert hit == tr


def test_gated_tree_misses_different_sid_run(tmp_path, monkeypatch):
    """Документированный компромисс гейта: чужой sid не находится."""
    hub = tmp_path / "hub"
    for i in range(31):
        _session(hub, "gelya", "2026-10", f"S20261012T{i:04d}-lesson-{i}", f"f{i}.mp3")
    # прогон того же файла, но под другим sid (не тот, что даст resolve_session_dirs)
    _session(hub, "gelya", "2026-10", "S20260101T0000-other-side", "Create the button [6 of 13].mp3")

    audio = hub / "gelya" / "inbox" / "Create the button [6 of 13].mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"fake")
    expected_dir = hub / "gelya" / "sessions" / "2026-10" / "S20261012T1124-create-the-button" / "transcripts"
    expected_dir.mkdir(parents=True, exist_ok=True)

    def boom(*_a, **_k):
        raise AssertionError("rglob не должен выполняться на гейтованном дереве")

    monkeypatch.setattr(pathlib.Path, "rglob", boom)
    assert watcher.existing_transcript(audio, expected_dir) is None