"""Признак качества прогона доходит до читателей состояния (801-a3, этап 2).

Прогон, потерявший минуты речи, заканчивается обычным ``asr-done``. До этих правок
о деградации знал только ``*-run-meta.json``; здесь проверяется, что она видна в
состоянии файла, в индексе проекта и в состоянии узла.
Решение: ``org/quality-flag-travels-from-run-meta-to-queue``.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import audio_inbox_watch as watcher
import node_status


REAL_RUN = {  # форма прогона 05.09 (встреча 524): пропуски выше порога, залипания нет
    "status": "degraded",
    "model": "medium",
    "quality": {
        "loops": [],
        "warnings": [{"kind": "no_punctuation", "start": 3027.0, "end": 5944.0}] * 9,
        "suspect_sec": 0,
        "suspect_ratio": 0.0,
        "gaps_sec": 72.74,
        "thresholds": {"suspect_ratio_max": 0.03, "gaps_sec_max": 30.0},
        "rescan": "on",
        "status": "degraded",
    },
}


def write_state(path: pathlib.Path, **fields) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"status": "asr-done", "session_id": "S20260905T0636-recording", **fields}
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return path


class TestQualitySummary:
    def test_carries_status_and_numbers(self):
        summary = watcher.quality_summary(REAL_RUN)
        assert summary["status"] == "degraded"
        assert summary["gaps_sec"] == 72.74
        assert summary["warnings"] == 9
        assert summary["loops"] == 0
        assert summary["model"] == "medium"

    def test_is_a_summary_not_a_second_copy(self):
        """Массивы остаются в run-meta: второго носителя полного блока нет."""
        summary = watcher.quality_summary(REAL_RUN)
        assert isinstance(summary["warnings"], int)
        assert not any(isinstance(v, (list, dict)) for v in summary.values())

    def test_run_without_quality_block_yields_nothing(self):
        """Старый прогон не получает выдуманного вердикта."""
        assert watcher.quality_summary({"status": "ok"}) is None
        assert watcher.quality_summary(None) is None

    def test_reads_run_meta_beside_transcript(self, tmp_path):
        transcript = tmp_path / "rec-transcript.md"
        transcript.write_text("text", encoding="utf-8")
        (tmp_path / "rec-run-meta.json").write_text(
            json.dumps(REAL_RUN), encoding="utf-8")
        assert watcher.run_meta_beside(transcript)["status"] == "degraded"
        assert watcher.run_meta_beside(tmp_path / "gone-transcript.md") is None


class TestCaughtUpState:
    def test_adopted_run_keeps_its_verdict(self, tmp_path):
        """Путь входа не отбеливает прогон: catch-up несёт тот же вердикт."""
        transcript = tmp_path / "rec-transcript.md"
        transcript.write_text("text", encoding="utf-8")
        (tmp_path / "rec-run-meta.json").write_text(
            json.dumps(REAL_RUN), encoding="utf-8")
        state = watcher.caught_up_state(transcript, "524", "LENOVO-AMD")
        assert state["quality"]["status"] == "degraded"

    def test_no_run_meta_no_quality_key(self, tmp_path):
        transcript = tmp_path / "rec-transcript.md"
        transcript.write_text("text", encoding="utf-8")
        assert "quality" not in watcher.caught_up_state(transcript, "524", "H")


class TestQualityCell:
    def test_degraded_says_why(self):
        cell = watcher.quality_cell({"quality": watcher.quality_summary(REAL_RUN)})
        assert cell.startswith("degraded")
        assert "пропуски 73s" in cell
        assert "предупреждений 9" in cell

    def test_loop_share_shown_when_present(self):
        cell = watcher.quality_cell({"quality": {"status": "degraded",
                                                 "suspect_ratio": 0.052}})
        assert "залипание 5.2%" in cell

    def test_clean_and_unknown_runs(self):
        assert watcher.quality_cell({"quality": {"status": "ok"}}) == "ok"
        assert watcher.quality_cell({}) == "—"


class TestProjectIndex:
    def test_column_and_counter(self, tmp_path):
        hub = tmp_path / "hub"
        inbox = hub / "524" / "524_inbox" / "2026-09-05"
        write_state(inbox / "bad.mp4.state.json",
                    quality=watcher.quality_summary(REAL_RUN))
        write_state(inbox / "good.mp4.state.json")
        watcher._write_project_index(hub, "524", {})
        index = (hub / "524" / "_sessions-index.md").read_text(encoding="utf-8")
        assert "| Файл | Статус | Качество | SessionId |" in index
        assert "из них с деградацией: 1" in index
        assert "degraded · пропуски 73s" in index

    def test_clean_project_says_nothing_about_degradation(self, tmp_path):
        hub = tmp_path / "hub"
        write_state(hub / "524" / "524_inbox" / "ok.mp4.state.json")
        watcher._write_project_index(hub, "524", {})
        index = (hub / "524" / "_sessions-index.md").read_text(encoding="utf-8")
        assert "деградацией" not in index


class TestNodeStatus:
    def _events(self, *qualities) -> list[dict]:
        stamp = node_status._iso()
        return [{"type": "file_done", "ts": stamp, "pid": "524", "media_sec": 8830,
                 "proc_sec": 902, "frames": 341, "quality": q} for q in qualities]

    def test_day_counts_degraded_runs(self):
        events = self._events({"status": "degraded"}, {"status": "ok"}, None)
        today = node_status.summarize_day(events, dt.date.today())
        assert today["done"] == 3
        assert today["degraded"] == 1

    def test_page_shows_the_counter(self):
        snap = node_status.build_snapshot(
            host="LENOVO-AMD", phase="idle",
            events=self._events({"status": "degraded"}))
        assert snap["today"]["degraded"] == 1
        assert "С деградацией" in node_status.render_html(snap)
