"""Счётчик доли канона: правило непересечения и что не входит в знаменатель.

Инструмент измерения проверяется отдельно от того, что он измеряет: замер 28.08
считался руками, и повторить его вторым человеком было нечем. Глоссарий здесь
синтетический — репозиторий публичный, боевые термины и расшифровки в него не
попадают.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import term_share  # noqa: E402
import glossary_correct as gc  # noqa: E402


GLOSSARY = {
    "version": 1,
    "terms": [
        {"canonical": "альфа", "status": "approved", "weight": 5,
         "variants": ["альва", "альфу-бета"], "multiword_variants": ["аль фа"],
         "protect": ["альт"]},
        {"canonical": "бета гамма", "status": "approved", "weight": 4,
         "variants": [], "multiword_variants": ["бета гама"]},
        {"canonical": "гаммон", "status": "approved", "weight": 3, "variants": ["гамон"]},
        {"canonical": "дельта", "status": "proposed", "weight": 1, "variants": ["делта"]},
    ],
}


def corrector() -> gc.GlossaryCorrector:
    return gc.GlossaryCorrector(GLOSSARY)


def share(text: str) -> dict:
    return term_share.report(pathlib.Path("x.txt"), term_share.count_forms(text, corrector()))


def test_canonical_and_variant_land_on_different_sides():
    result = share("альфа и ещё раз альва")
    assert result["canon"] == 1 and result["variant"] == 1
    assert result["share"] == 0.5


def test_russian_endings_count_as_the_canonical_form():
    """«гаммоном» — та же каноническая форма, а не отдельное слово."""
    assert share("гаммоном и гаммоны")["canon"] == 2


def test_a_vowel_final_term_declines_beyond_the_ending_list():
    """Ограничение то же, что у самой замены: окончание приписывается, а не
    заменяет последнюю букву. «альфой» точным совпадением не ловится — счёт и
    механизм замены обязаны врать одинаково, иначе замер меряет не тот механизм."""
    assert share("альфой")["canon"] == 0


def test_multiword_canonical_is_one_hit_not_two():
    result = share("бета гамма")
    assert result["canon"] == 1 and result["variant"] == 0


def test_a_position_is_counted_once():
    """Длинное совпадение закрывает позицию: внутри него ничего не досчитывается."""
    result = share("бета гама")
    assert result["variant"] == 1 and result["canon"] == 0


def test_protected_forms_stay_out_of_the_denominator():
    result = share("альт и ещё альт")
    assert result["canon"] == 0 and result["variant"] == 0
    assert result["protected_excluded"] == 2 and result["share"] is None


def test_proposed_terms_are_not_counted_at_all():
    """В прогоне применяются только approved — счёт обязан считать то же."""
    assert share("дельта и делта")["canon"] == 0


def test_loop_zone_segments_are_dropped_before_counting(tmp_path):
    """Зона «альфа альфа» ×3 добавила бы к канону шесть вхождений, которых не было."""
    segments = [{"start": 0, "end": 20, "text": "Мы обсудили альфа и пошли дальше."}]
    segments += [{"start": 20 + i * 20, "end": 40 + i * 20, "text": "альфа альфа"}
                 for i in range(3)]
    path = tmp_path / "run-segments.jsonl"
    path.write_text(chr(10).join(json.dumps(s, ensure_ascii=False) for s in segments),
                    encoding="utf-8")

    text, dropped = term_share.read_text(path, GLOSSARY, exclude_loops=False)
    assert term_share.count_forms(text, corrector())["альфа"]["canon"] == 7 and dropped == 0

    text, dropped = term_share.read_text(path, GLOSSARY, exclude_loops=True)
    assert term_share.count_forms(text, corrector())["альфа"]["canon"] == 1
    assert dropped == 3


def test_the_prompt_of_the_run_is_read_from_run_meta(tmp_path):
    """Подсказка берётся из run-meta прогона, а не собирается заново из глоссария."""
    (tmp_path / "x-run-meta.json").write_text(
        json.dumps({"glossary": {"initial_prompt": "Обсуждение: альфа, бета гамма."}},
                   ensure_ascii=False), encoding="utf-8")
    assert term_share.run_prompt(tmp_path / "x-segments.jsonl", GLOSSARY).endswith("бета гамма.")


def test_without_run_meta_the_prompt_falls_back_to_the_glossary(tmp_path):
    prompt = term_share.run_prompt(tmp_path / "x-segments.jsonl", GLOSSARY)
    assert prompt and "альфа" in prompt
