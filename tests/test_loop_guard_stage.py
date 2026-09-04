"""Повторный проход зоны и гейт прогона внутри media_transcribe (801-o15).

Модель здесь не поднимается: проверяется решение о подстановке, а не качество
распознавания. ``rescan_zone`` подменяется — именно на его результате и стоит
правило «подставить, только если сам не залипает». Фикстуры синтетические:
репозиторий публичный, реальные расшифровки в него не попадают.
"""
from __future__ import annotations

import media_transcribe as mt


PROMPT = "Обсуждение: альфа, бета, гамма, дельта."


def seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


def stuck_run() -> list[dict]:
    """Прогон, где 60 из 600 с отданы повтору одной строки."""
    good = [seg(i * 20, (i + 1) * 20, f"Обычная фраза номер {i}, сказанная в нормальном темпе.")
            for i in range(27)]
    bad = [seg(540 + i * 20, 560 + i * 20, "Покупатель") for i in range(3)]
    return good + bad


def payload_for(tmp_path, **extra) -> dict:
    payload = {
        "output_dir": str(tmp_path),
        "execution_mode": "full",
        "_glossary_prompt_cache": {"source": "project", "initial_prompt": PROMPT,
                                   "hotwords": "альфа бета", "prompt_terms": 4,
                                   "fingerprint": "abc"},
    }
    payload.update(extra)
    return payload


def test_without_a_rescan_context_the_zone_is_only_reported(tmp_path):
    """Режим speaker_pass звук не переслушивает — но потерю обязан назвать."""
    segments = stuck_run()
    warnings: list[str] = []
    quality = mt.apply_loop_guard(payload_for(tmp_path), segments, "audio.wav", tmp_path, warnings)
    assert quality["rescan"] == "off"
    assert [loop["kind"] for loop in quality["loops"]] == ["repetition"]
    assert quality["suspect_sec"] == 60.0
    assert segments[-1]["text"] == "Покупатель"  # ничего не стёрто
    assert any("quality_degraded" in w for w in warnings)


def test_a_clean_rescan_replaces_the_zone_and_clears_the_suspicion(tmp_path, monkeypatch):
    fresh = [seg(541, 555, "Вернувшаяся речь, которую слышно целиком."),
             seg(555, 599, "И вторая фраза, которая раньше терялась в повторе.")]
    monkeypatch.setattr(mt, "rescan_zone", lambda *a, **k: list(fresh))
    segments = stuck_run()
    quality = mt.apply_loop_guard(
        payload_for(tmp_path, _rescan_context={"kwargs": {}}), segments, "audio.wav", tmp_path, [])
    assert quality["loops"][0]["recovered"] is True
    assert quality["suspect_sec"] == 0.0
    assert quality["status"] == "ok"
    assert [s["text"] for s in segments[-2:]] == [s["text"] for s in fresh]


def test_a_rescan_that_loops_again_leaves_an_empty_marked_zone(tmp_path, monkeypatch):
    """Догадки не подставляются: пусть будет видно, что здесь неизвестно."""
    monkeypatch.setattr(mt, "rescan_zone",
                        lambda *a, **k: [seg(541 + i * 5, 546 + i * 5, "Покупатель") for i in range(3)])
    segments = stuck_run()
    quality = mt.apply_loop_guard(
        payload_for(tmp_path, _rescan_context={"kwargs": {}}), segments, "audio.wav", tmp_path, [])
    assert quality["loops"][0]["recovered"] is False
    assert quality["suspect_sec"] == 60.0
    assert [s["text"] for s in segments[-3:]] == ["", "", ""]
    assert {s["quality_flag"] for s in segments[-3:]} == {"repetition"}


def test_a_failing_rescan_is_a_warning_not_a_dead_run(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg упал")

    monkeypatch.setattr(mt, "rescan_zone", boom)
    warnings: list[str] = []
    segments = stuck_run()
    quality = mt.apply_loop_guard(
        payload_for(tmp_path, _rescan_context={"kwargs": {}}), segments, "audio.wav", tmp_path, warnings)
    assert quality["loops"][0]["recovered"] is False
    assert any("loop_guard_rescan_failed" in w for w in warnings)


def test_a_clean_run_keeps_status_ok_and_touches_nothing(tmp_path):
    segments = [seg(i * 20, (i + 1) * 20, f"Обычная фраза номер {i}, сказанная в нормальном темпе.")
                for i in range(30)]
    before = [dict(s) for s in segments]
    quality = mt.apply_loop_guard(payload_for(tmp_path), segments, "audio.wav", tmp_path, [])
    assert quality["status"] == "ok" and quality["loops"] == []
    assert segments == before


def test_the_rescan_drops_the_prompt_and_opens_the_feedback_loop(tmp_path, monkeypatch):
    """Аттрактор убирается вместе с подсказкой; hotwords остаются (801-o11)."""
    seen: dict = {}

    def fake_worker(job, model=None):
        seen.update(job)
        return {"chunk_index": 0, "language": "ru", "segments": []}

    monkeypatch.setattr(mt, "transcribe_chunk_worker", fake_worker)
    monkeypatch.setattr(mt, "cut_audio_span", lambda *a, **k: "zone.wav")
    context = {
        "kwargs": {"beam_size": 5, "condition_on_previous_text": True,
                   "initial_prompt": PROMPT, "hotwords": "альфа бета"},
        "model_path": "models/medium", "runtime_device": "cuda",
        "runtime_compute_type": "float16", "audio_duration": 600.0,
    }
    zone = {"start": 540.0, "end": 600.0, "kind": "repetition"}
    mt.rescan_zone(payload_for(tmp_path), zone, "audio.wav", tmp_path, context,
                   mt.resolve_loop_guard_config({}))
    assert "initial_prompt" not in seen["kwargs"]
    assert seen["kwargs"]["condition_on_previous_text"] is False
    assert seen["kwargs"]["hotwords"] == "альфа бета"
    # Поля слева и справа, обрезанные длительностью записи.
    assert seen["chunk_start"] == 535.0 and seen["chunk_duration"] == 65.0


def test_the_gate_thresholds_come_from_the_node_config(tmp_path):
    payload = payload_for(tmp_path, loop_guard={"suspect_ratio_max": 0.5})
    quality = mt.apply_loop_guard(payload, stuck_run(), "audio.wav", tmp_path, [])
    assert quality["suspect_sec"] == 60.0 and quality["status"] == "ok"


def test_the_rescan_can_be_switched_off_while_the_gate_stays_on(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "rescan_zone", lambda *a, **k: [seg(541, 599, "Вернувшаяся речь.")])
    payload = payload_for(tmp_path, _rescan_context={"kwargs": {}},
                          loop_guard={"enabled": False})
    quality = mt.apply_loop_guard(payload, stuck_run(), "audio.wav", tmp_path, [])
    assert quality["rescan"] == "off" and quality["status"] == "degraded"
