"""Детектор залипания: признаки по отдельности, границы порогов, склейка зон.

Фикстуры **синтетические**. Репозиторий публичный (MIT), и содержимое реальных
расшифровок, имена участников и тексты обсуждений в него не попадают ни в каком
виде — ни выдержкой, ни «для наглядности». Воспроизводится здесь не разговор, а
шаблон петли: одна строка, повторённая подряд, и подсказка, вернувшаяся вместо
речи. Термины подсказки в тестах вымышленные (альфа/бета/гамма/дельта).
"""
from __future__ import annotations

import loop_guard


PROMPT = "Обсуждение: альфа, бета, гамма, дельта."
CONFIG = loop_guard.resolve_config(None)


def seg(start: float, end: float, text: str) -> dict:
    return {"start": start, "end": end, "text": text}


def speech(count: int, start: float = 0.0, step: float = 6.0) -> list[dict]:
    """Обычная речь: разные фразы, пунктуация на месте."""
    lines = [
        "Мы начали с того, что нужно решить сегодня.",
        "Потом договорились считать это по-другому.",
        "Осталось понять, кто именно это делает.",
        "Хорошо, тогда возвращаемся к первому пункту.",
        "И на этом заканчиваем, дальше уже детали.",
    ]
    return [seg(start + i * step, start + (i + 1) * step, lines[i % len(lines)])
            for i in range(count)]


def kinds(zones: list[dict]) -> list[str]:
    return [zone["kind"] for zone in zones]


# --- A · повтор -------------------------------------------------------------

def test_three_identical_segments_are_a_repetition_zone():
    segments = speech(3) + [seg(18 + i * 20, 38 + i * 20, "Покупатель") for i in range(3)]
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert kinds(zones) == [loop_guard.REPETITION]
    assert (zones[0]["start"], zones[0]["end"]) == (18, 78)
    assert zones[0]["repeats"] == 3


def test_two_identical_segments_are_below_the_threshold():
    segments = speech(3) + [seg(18, 38, "Покупатель"), seg(38, 58, "Покупатель")]
    assert loop_guard.detect_zones(segments, PROMPT, CONFIG) == []


def test_prefix_counts_as_the_same_line():
    """Модель обрывает ту же строку на разной длине — это тот же повтор."""
    segments = [seg(0, 20, "один и тот же поворот"),
                seg(20, 40, "один и тот же поворот, который"),
                seg(40, 60, "один и тот же")]
    assert kinds(loop_guard.detect_zones(segments, PROMPT, CONFIG)) == [loop_guard.REPETITION]


def test_a_short_stutter_is_not_a_loop():
    """«Да. Да. Да.» за шесть секунд — заминка речи, а не потерянная минута."""
    segments = [seg(0, 2, "Да."), seg(2, 4, "Да."), seg(4, 6, "Да.")] + speech(3, start=6)
    assert loop_guard.detect_zones(segments, PROMPT, CONFIG) == []


def test_normal_speech_raises_nothing():
    assert loop_guard.detect_zones(speech(40), PROMPT, CONFIG) == []


# --- B · возврат подсказки --------------------------------------------------

def test_three_prompt_terms_in_prompt_order_are_a_prompt_loop():
    segments = [seg(i * 20, (i + 1) * 20, "альфа бета гамма") for i in range(3)]
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert kinds(zones) == [loop_guard.PROMPT_LOOP]


def test_terms_inside_a_sentence_are_not_a_loop():
    """Термин, который просто прозвучал в речи, — не аттрактор: доля символов мала."""
    line = "Мы обсудили альфа и решили, что дальше смотрим на следующей неделе."
    segments = [seg(i * 20, (i + 1) * 20, line) for i in range(3)]
    # Повтор одной строки признак A всё же увидит — но не как возврат подсказки.
    assert kinds(loop_guard.detect_zones(segments, PROMPT, CONFIG)) == [loop_guard.REPETITION]
    assert loop_guard.prompt_echo(line, loop_guard.prompt_terms(PROMPT), CONFIG) is None


def test_terms_out_of_prompt_order_are_not_b1():
    assert loop_guard.prompt_echo("гамма бета альфа", loop_guard.prompt_terms(PROMPT), CONFIG) is None


def test_two_terms_in_a_row_are_below_the_threshold():
    assert loop_guard.prompt_echo("альфа бета", loop_guard.prompt_terms(PROMPT), CONFIG) is None


def test_one_term_repeated_inside_a_segment_is_a_prompt_loop():
    """Форма B2: строка целиком собрана из термина глоссария."""
    echo = loop_guard.prompt_echo("бета бета", loop_guard.prompt_terms(PROMPT), CONFIG)
    assert echo and echo["form"] == "B2"


def test_prompt_loop_outranks_repetition_on_the_same_segments():
    """Одинаковые сегменты из терминов подсказки называются источником строки."""
    segments = [seg(i * 20, (i + 1) * 20, "альфа бета гамма") for i in range(3)]
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert zones[0]["kind"] == loop_guard.PROMPT_LOOP


def test_without_a_prompt_signal_b_is_silent():
    segments = [seg(i * 20, (i + 1) * 20, "альфа бета гамма") for i in range(3)]
    assert kinds(loop_guard.detect_zones(segments, None, CONFIG)) == [loop_guard.REPETITION]


def test_prompt_terms_keep_prompt_order_and_drop_the_lead_in():
    assert loop_guard.prompt_terms(PROMPT) == ["альфа", "бета", "гамма", "дельта"]
    assert loop_guard.prompt_terms(None) == []


def test_longest_term_wins_so_order_is_not_broken():
    """«альфа бета» не должна распасться на «альфа»: порядок терминов собьётся."""
    prompt = "Обсуждение: альфа, альфа бета, гамма, дельта."
    terms = loop_guard.prompt_terms(prompt)
    assert loop_guard.prompt_echo("альфа бета гамма дельта", terms, CONFIG)
    assert loop_guard.prompt_echo("альфа бета гамма", terms, CONFIG) is None


# --- C · нет пунктуации -----------------------------------------------------

def rough(count: int, start: float = 0.0, step: float = 10.0) -> list[dict]:
    """Речь без единой точки — разными строками, иначе сработает признак A."""
    return [seg(start + i * step, start + (i + 1) * step,
                f"и вот тогда мы поняли что нужно делать иначе шаг {i} и дальше по списку")
            for i in range(count)]


def test_a_minute_without_punctuation_is_a_warning():
    segments = rough(7)
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert kinds(zones) == [loop_guard.NO_PUNCTUATION]


def test_half_a_minute_without_punctuation_is_not():
    segments = rough(3)
    assert loop_guard.detect_zones(segments, PROMPT, CONFIG) == []


def test_no_punctuation_is_a_warning_only_and_never_degrades_a_run():
    segments = rough(7)
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    quality = loop_guard.quality_block(segments, zones, 70.0, CONFIG)
    assert quality["loops"] == [] and len(quality["warnings"]) == 1
    assert quality["suspect_sec"] == 0.0
    assert loop_guard.gate_status(quality, CONFIG) == "ok"


# --- склейка зон ------------------------------------------------------------

def test_adjacent_segments_of_one_kind_become_one_zone():
    segments = [seg(i * 20, (i + 1) * 20, "Покупатель") for i in range(5)]
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert len(zones) == 1 and zones[0]["end"] - zones[0]["start"] == 100


def test_two_kinds_side_by_side_stay_two_zones():
    """medium 02.09: зона подсказки стоит вплотную к зоне повтора, но потери разные."""
    segments = ([seg(i * 20, (i + 1) * 20, "альфа бета гамма") for i in range(3)]
                + [seg(60 + i * 20, 80 + i * 20, "Покупатель") for i in range(3)])
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert kinds(zones) == [loop_guard.PROMPT_LOOP, loop_guard.REPETITION]
    assert zones[0]["end"] == 60 and zones[1]["start"] == 60


def test_evidence_is_the_repeated_line_and_its_count():
    segments = [seg(i * 20, (i + 1) * 20, "Покупатель") for i in range(4)]
    zone = loop_guard.detect_zones(segments, PROMPT, CONFIG)[0]
    assert zone["evidence"] == "Покупатель" and zone["repeats"] == 4


def test_evidence_is_clipped_to_the_configured_length():
    config = dict(CONFIG, evidence_chars=10)
    line = "повторяющаяся строка длиной существенно больше десяти символов"
    segments = [seg(i * 20, (i + 1) * 20, line) for i in range(3)]
    assert len(loop_guard.detect_zones(segments, PROMPT, config)[0]["evidence"]) == 10


# --- пропуски ---------------------------------------------------------------

def test_a_hole_longer_than_the_threshold_is_counted():
    segments = [seg(0, 10, "Первая фраза, сказанная в обычном темпе речи."),
                seg(46, 56, "Вторая фраза, сказанная в том же обычном темпе.")]
    assert loop_guard.measure_gaps(segments, CONFIG) == 36.0


def test_a_short_hole_is_not_a_gap():
    segments = [seg(0, 10, "Первая фраза, сказанная в обычном темпе речи."),
                seg(18, 28, "Вторая фраза, сказанная в том же обычном темпе.")]
    assert loop_guard.measure_gaps(segments, CONFIG) == 0.0


def test_a_long_segment_with_almost_no_text_is_a_gap():
    """Пропуск бывает не «нет сегментов», а один сегмент на 35 с с одной строкой."""
    segments = [seg(0, 10, "Первая фраза, сказанная в обычном темпе речи."),
                seg(10, 45, "Короткая строка."),
                seg(45, 55, "Третья фраза, сказанная в том же обычном темпе.")]
    assert loop_guard.measure_gaps(segments, CONFIG) == 35.0


def test_silence_before_the_first_and_after_the_last_segment_is_not_a_gap():
    segments = [seg(120, 130, "Первая фраза, сказанная в обычном темпе речи."),
                seg(130, 140, "Вторая фраза, сказанная в том же обычном темпе.")]
    assert loop_guard.measure_gaps(segments, CONFIG) == 0.0


def test_a_hole_inside_a_loop_zone_is_not_counted_twice():
    """Секунды внутри зоны уже посчитаны в suspect_sec."""
    segments = [seg(i * 40, i * 40 + 2, "Покупатель") for i in range(4)]
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    assert loop_guard.measure_gaps(segments, CONFIG) > 0
    assert loop_guard.measure_gaps(segments, CONFIG, zones=zones) == 0.0


# --- блок quality и гейт ----------------------------------------------------

def test_quality_block_counts_only_zones_that_were_not_recovered():
    segments = ([seg(i * 20, (i + 1) * 20, "альфа бета гамма") for i in range(3)]
                + [seg(60 + i * 20, 80 + i * 20, "Покупатель") for i in range(3)])
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    zones[0]["recovered"] = True
    quality = loop_guard.quality_block(segments, zones, 1200.0, CONFIG)
    assert quality["suspect_sec"] == 60.0
    assert quality["suspect_ratio"] == 0.05
    assert len(quality["loops"]) == 2


def test_quality_block_carries_the_thresholds_it_was_judged_by():
    quality = loop_guard.quality_block([], [], 0.0, CONFIG)
    assert quality["thresholds"] == {"suspect_ratio_max": 0.03, "gaps_sec_max": 30.0}
    assert quality["suspect_ratio"] == 0.0


def test_a_run_above_the_suspect_ratio_is_degraded():
    quality = {"suspect_ratio": 0.031, "gaps_sec": 0.0}
    assert loop_guard.gate_status(quality, CONFIG) == "degraded"


def test_a_run_above_the_gaps_threshold_is_degraded():
    quality = {"suspect_ratio": 0.0, "gaps_sec": 30.1}
    assert loop_guard.gate_status(quality, CONFIG) == "degraded"


def test_exactly_at_the_threshold_is_still_ok():
    quality = {"suspect_ratio": 0.03, "gaps_sec": 30.0}
    assert loop_guard.gate_status(quality, CONFIG) == "ok"


def test_a_clean_run_stays_ok():
    segments = speech(40)
    zones = loop_guard.detect_zones(segments, PROMPT, CONFIG)
    quality = loop_guard.quality_block(segments, zones, 240.0, CONFIG)
    assert loop_guard.gate_status(quality, CONFIG) == "ok"


# --- проверка выхода повторного прохода -------------------------------------

def test_a_rescan_that_loops_again_is_rejected_however_short_it_is():
    """Порог длительности зоны на коротком куске не применяется: залипло — значит залипло."""
    stuck = [seg(0, 2, "Покупатель"), seg(2, 4, "Покупатель"), seg(4, 6, "Покупатель")]
    assert loop_guard.looks_stuck(stuck, PROMPT, CONFIG) is True


def test_a_clean_rescan_passes():
    assert loop_guard.looks_stuck(speech(4), PROMPT, CONFIG) is False


def test_a_rescan_with_no_punctuation_is_not_rejected_for_that_alone():
    """C — предупреждение: менять пустотой то, что удалось расслышать, хуже."""
    assert loop_guard.looks_stuck(rough(8), PROMPT, CONFIG) is False


# --- конфигурация -----------------------------------------------------------

def test_node_config_overrides_defaults_and_keeps_the_rest():
    config = loop_guard.resolve_config({"loop_guard": {"repeat_min_segments": 5}})
    assert config["repeat_min_segments"] == 5
    assert config["suspect_ratio_max"] == loop_guard.DEFAULTS["suspect_ratio_max"]


def test_unknown_keys_are_ignored_and_absent_config_is_defaults():
    assert loop_guard.resolve_config({"loop_guard": {"nope": 1}})["repeat_min_segments"] == 3
    assert loop_guard.resolve_config(None) == loop_guard.DEFAULTS


def test_raising_the_repeat_threshold_silences_a_short_run():
    segments = [seg(i * 20, (i + 1) * 20, "Покупатель") for i in range(3)]
    config = loop_guard.resolve_config({"loop_guard": {"repeat_min_segments": 4}})
    zones = loop_guard.detect_zones(segments, PROMPT, config)
    # Минута без единой точки остаётся предупреждением — но потерей уже не зовётся.
    assert [z for z in zones if z["kind"] in loop_guard.GATED_KINDS] == []


def test_empty_input_is_not_an_error():
    assert loop_guard.detect_zones([], PROMPT, CONFIG) == []
    assert loop_guard.measure_gaps([], CONFIG) == 0.0
