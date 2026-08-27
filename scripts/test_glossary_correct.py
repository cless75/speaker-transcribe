"""Глоссарий и пост-коррекция (801-o11): pytest-набор.

Что держит этот набор:

1. Контрольный набор до/после по классам приёмки (безусловная замена 10/10,
   условная 2/2) — на ТЕСТОВОМ глоссарии. Переразметки по вердиктам:
   org/term-canon-by-nature-cyrillic-concepts — exocortex обычный вариант
   «экзокортекса»; org/bok-homonym-replaced-by-default — «бок» заменяется по
   умолчанию (146:1), protect BoK сжат до идиом, «даю бок» и «положить бок»
   безусловные. Плюс приоритет protect: одиночный protect действует внутри
   своего термина и не гасит точную многословную последовательность чужого
   (находка ретро-прогона: «Cloud Code» против protect «cloud»).
2. Защищённые формы не тронуты без выполненного контекстного условия.
3. Многословная замена только по точной последовательности токенов;
   перестановка или вставка токена — замены нет.
4. proposed не заменяется (гейт status), но попадает в отчёт кандидатов.
5. Исходный текст сохранён, вариант отдельный (имя через asr_variant_id),
   отчёт правок полон — правки восстанавливают corrected из исходника.
6. Атомарная запись без BOM; чтение переживает чужой BOM.
7. Подсказки модели: только approved, порядок по weight, лимит 224 токена,
   порядок источников запуск → проект → узел → выключено, kwargs и отпечаток.

Запуск:  python -m pytest scripts/test_glossary_correct.py -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import glossary_correct as gc  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
GLOSSARY_PATH = FIXTURES / "project-glossary.test.json"
GOLDEN_PATH = FIXTURES / "golden-phrases.json"


@pytest.fixture(scope="module")
def glossary() -> dict:
    loaded = gc.read_glossary_file(GLOSSARY_PATH)
    assert loaded is not None
    return loaded


@pytest.fixture(scope="module")
def golden_cases() -> list[dict]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8-sig"))["cases"]


def _case(golden_cases: list[dict], observed: str) -> dict:
    for case in golden_cases:
        if case["observed"].lower() == observed:
            return case
    raise AssertionError(f"кейс не найден: {observed}")


def _reconstruct(source: str, replacements: list[dict]) -> str:
    """Отчёт полон, если правки восстанавливают corrected из исходника."""
    out = []
    cursor = 0
    for rep in sorted(replacements, key=lambda r: r["offset"]):
        out.append(source[cursor:rep["offset"]])
        assert source[rep["offset"]:rep["offset"] + rep["length"]] == rep["matched"]
        out.append(rep["replacement"])
        cursor = rep["offset"] + rep["length"]
    out.append(source[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# 1. Контрольный набор по классам
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("observed,expect_fragment", [
    ("экзопортексом", "экзокортексом"),
    ("экокордекс", "Экзокортекс"),
    ("за кортексом", "экзокортексом"),
    ("хармнес", "harness"),
    ("chart space", "shared space"),
    ("клода", "Claude"),
    ("обсидиант", "Obsidian"),
    # org/term-canon-by-nature-cyrillic-concepts: exocortex — обычный вариант;
    # «Экзокортекс» с заглавной: preserve-sentence-case сохраняет регистр
    # исходного «Exocortex».
    ("exocortex", "Экзокортекс"),
    # org/bok-homonym-replaced-by-default: «бок» заменяется по умолчанию,
    # context_rules у BoK сняты — оба кейса стали безусловными.
    ("даю бок", "BoK"),
    ("положить бок", "BoK"),
])
def test_golden_unconditional(glossary, golden_cases, observed, expect_fragment):
    case = _case(golden_cases, observed)
    corrected, report = gc.correct_text(case["fragment"], glossary)
    assert observed not in corrected.lower()
    assert expect_fragment in corrected
    assert report["replacements"], "правка обязана быть в отчёте"
    assert _reconstruct(case["fragment"], report["replacements"]) == corrected


@pytest.mark.parametrize("observed,rule_expected", [
    ("кладе", "context"),
    ("install cloud", "context"),
])
def test_golden_conditional(glossary, golden_cases, observed, rule_expected):
    case = _case(golden_cases, observed)
    corrected, report = gc.correct_text(case["fragment"], glossary)
    assert observed not in corrected.lower()
    rules = {r["rule"] for r in report["replacements"]}
    assert rule_expected in rules, "условная замена идёт через context_rules"


def test_measure_class_layout():
    """Раскладка классов после вердиктов: «вне замера» пуст, безусловных 10
    (в т.ч. exocortex и оба кейса «бок»), условных 2 (омонимы Claude)."""
    assert not gc.MEASURE_OUT_OF_SCOPE
    assert len(gc.MEASURE_UNCONDITIONAL) == 10
    assert {"exocortex", "даю бок", "положить бок"} <= gc.MEASURE_UNCONDITIONAL
    assert gc.MEASURE_CONDITIONAL == {"кладе", "install cloud"}


# ---------------------------------------------------------------------------
# 2. Защищённые формы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", gc.PROTECTED_CONTROL_PHRASES)
def test_protected_untouched_without_condition(glossary, phrase):
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase, "protect без условия — 0 нарушений"
    assert not report["replacements"]
    assert report["protected_kept"], "оставленная форма фиксируется в отчёте"


def test_protected_never_wins_over_replace(glossary):
    """never_if_near сильнее replace_if_near в одном предложении."""
    phrase = "Сессия агента пишет логи в Google Cloud."  # триггеры есть, но и never есть
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]


def test_protected_window_is_sentence(glossary):
    """Условие из соседнего предложения не действует: окно — предложение."""
    phrase = "Мы говорили про install и npm. Потом пришёл cloud."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]


def test_bok_replaced_by_default(glossary):
    """org/bok-homonym-replaced-by-default: «бок» без идиомы — обычный вариант,
    заменяется по умолчанию (146:1 по корпусу), в любом падеже."""
    corrected, report = gc.correct_text("Просто бок.", glossary)
    assert corrected == "Просто BoK."
    assert report["replacements"][0]["rule"] == "variant"
    corrected, _ = gc.correct_text("Разложить по бокам знаний.", glossary)
    assert corrected == "Разложить по бокам знаний.", \
        "идиома «по бокам» защищена точной последовательностью"


# ---------------------------------------------------------------------------
# 2а. Приоритет protect: внутри термина, не через границу терминов
# ---------------------------------------------------------------------------


def test_foreign_multiword_beats_single_protect(glossary):
    """Находка ретро-прогона: protect «cloud» термина Claude не должен гасить
    точную последовательность «Cloud Code» ЧУЖОГО термина Claude Code."""
    phrase = "Запусти Cloud Code и посмотри логи."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == "Запусти Claude Code и посмотри логи."
    assert report["replacements"][0]["rule"] == "multiword"
    assert report["replacements"][0]["matched"] == "Cloud Code"


def test_single_protected_cloud_still_guarded(glossary):
    """Негатив: одиночный «cloud» без триггеров по-прежнему не заменяется."""
    phrase = "Смотри, cloud тут просто упомянут."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]
    assert report["protected_kept"][0]["form"] == "cloud"


def test_multiword_protect_idiom_bok_o_bok(glossary):
    """«бок о бок» — многословный protect: точная последовательность сильнее
    одиночного варианта «бок» своего же термина, ни один «бок» не заменяется."""
    phrase = "Мы работали бок о бок весь день."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]
    assert any(k["form"] == "бок о бок" for k in report["protected_kept"])


def test_multiword_protect_corpus_phrase_s_boku(glossary):
    """Единственное прямое значение «бок» на весь корпус 700 (S20260605):
    при «бок» в variants и идиоме «с боку» в protect — ни одной замены."""
    phrase = "сидит этих человек с боку с рукой"
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]
    assert any(k["form"] == "с боку" for k in report["protected_kept"])


def test_single_bok_outside_idiom_replaced(glossary):
    corrected, report = gc.correct_text("Я даю бок.", glossary)
    assert corrected == "Я даю BoK."
    assert report["replacements"][0]["rule"] == "variant"


def test_own_multiword_beats_own_protect_regression(glossary):
    """Регрессия боевого прогона 24.08: protect «кортекс» у «экзокортекса»
    гасил СОБСТВЕННУЮ последовательность «за кортексом». Многословный контекст
    сам снимает неоднозначность — замена обязана сработать."""
    corrected, report = gc.correct_text("Цепь за кортексом.", glossary)
    assert corrected == "Цепь экзокортексом."
    assert report["replacements"][0]["rule"] == "multiword"
    assert report["replacements"][0]["matched"] == "за кортексом"


def test_bare_kortex_protected(glossary):
    """Голое «кортекс» вне последовательности — одиночная защита действует:
    не заменяется и фиксируется в protected_kept."""
    phrase = "Моторная кора, то есть кортекс, отвечает за движение."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]
    assert any(k["form"] == "кортекс" for k in report["protected_kept"])


def test_fixture_matches_combat_layout(glossary):
    """Указание владельца потока (24.08): приёмка — только боевой глоссарий с
    Хаба; фикстура — для юнит-тестов, и её расхождение с боевой раскладкой
    ключевых терминов — само по себе дефект. Тест пинит контракт раскладки."""
    terms = {t["canonical"]: t for t in glossary["terms"]}
    assert "кортекс" in terms["экзокортекс"]["protect"], \
        "у экзокортекса непустой protect (боевой файл: кортекс)"
    bok = terms["BoK"]
    assert {"бок", "бока", "боку", "боком", "боков"} <= set(bok["variants"]), \
        "падежи «бок*» в variants"
    assert set(bok["protect"]) == {"бок о бок", "с боку", "под боком",
                                   "на боку", "по бокам"}, \
        "protect BoK — ровно пять идиом строками"
    assert "context_rules" not in bok, "context_rules у BoK сняты вердиктом"
    assert "cloud" in terms["Claude"]["protect"]
    assert "cloud code" in terms["Claude Code"]["multiword_variants"]


# ---------------------------------------------------------------------------
# 3. Многословная замена: точная последовательность токенов
# ---------------------------------------------------------------------------


def test_multiword_exact_sequence_with_ending(glossary):
    corrected, report = gc.correct_text("Цепь за кортексом.", glossary)
    assert corrected == "Цепь экзокортексом."
    assert report["replacements"][0]["rule"] == "multiword"
    assert report["replacements"][0]["matched"] == "за кортексом"


def test_multiword_negative_reorder(glossary):
    """Перестановка токенов — замены нет."""
    phrase = "Цепь кортексом за."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]


def test_multiword_negative_inserted_token(glossary):
    """Вставленный токен рвёт последовательность — замены нет."""
    phrase = "Цепь за большим кортексом."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]


def test_multiword_negative_missing_token(glossary):
    """Одинокий последний токен последовательности — замены нет."""
    phrase = "Просто кортексом дело не заканчивается."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase
    assert not report["replacements"]


# ---------------------------------------------------------------------------
# 4. Гейт статуса: proposed не заменяется
# ---------------------------------------------------------------------------


def test_proposed_not_replaced_but_reported(glossary):
    phrase = "Наш workflow собирает тренды."
    corrected, report = gc.correct_text(phrase, glossary)
    assert corrected == phrase, "proposed не попадает в замену"
    assert report["candidates"], "proposed попадает в отчёт кандидатов"
    assert report["candidates"][0]["canonical"] == "воркфлоу"
    assert report["candidates"][0]["status"] == "proposed"


def test_prompts_exclude_proposed(glossary):
    """Подсказки — часть применения термина: только approved."""
    prompt = gc.build_initial_prompt(glossary)
    hotwords = gc.build_hotwords(glossary)
    assert "воркфлоу" not in (prompt or "")
    assert "воркфлоу" not in hotwords
    assert "экзокортекс" in hotwords
    assert "скилл" in hotwords, "скилл approved вердиктом — в подсказках"


def test_skill_canon_cyrillic_keeps_endings(glossary):
    """Решающий довод вердикта — морфологический: кириллический канон «скилл»
    сохраняет падежи, латиница заменяется на канон."""
    corrected, _ = gc.correct_text("Прокачай свои скилы.", glossary)
    assert corrected == "Прокачай свои скиллы."
    corrected, _ = gc.correct_text("Каждый skill работает сам.", glossary)
    assert corrected == "Каждый скилл работает сам."


# ---------------------------------------------------------------------------
# 5. Файловый слой: исходник сохранён, вариант отдельный, отчёт полон
# ---------------------------------------------------------------------------


TRANSCRIPT_MD = (
    "---\n"
    'source_file: "S20260810T2110-platforma.m4a"\n'
    'asr_variant_id: "medium-balanced-cpu-20260810T2110"\n'
    "---\n"
    "\n"
    "## Session Transcript\n"
    "\n"
    "[00:01:00] SPEAKER_01: тебе нужен полноценный хармнес, какой-то экзопортекс.\n"
    "[00:02:00] SPEAKER_02: Цепь за кортексом. Мы работали бок о бок.\n"
)


def test_retro_source_preserved_variant_separate(tmp_path, glossary):
    src = tmp_path / "S1-transcript.md"
    src.write_text(TRANSCRIPT_MD, encoding="utf-8")
    before = src.read_bytes()

    summary = gc.correct_file(src, glossary, glossary_path=str(GLOSSARY_PATH))

    assert src.read_bytes() == before, "исходный текст не трогается никогда"
    variant = pathlib.Path(summary["variant_file"])
    assert variant.exists() and variant != src
    vid = summary["asr_variant_id"]
    assert vid in variant.name, "имя варианта несёт asr_variant_id"
    assert vid.startswith("medium-balanced-cpu-20260810t2110-gloss-"), \
        "variant_id наследует исходный asr_variant_id (логика derive_asr_variant_id)"
    variant_text = variant.read_text(encoding="utf-8")
    assert "harness" in variant_text and "экзокортекс" in variant_text
    assert "экзокортексом" in variant_text  # многословная с окончанием
    assert "бок о бок" in variant_text  # идиома из protect не тронута
    assert f"asr_variant_id: {json.dumps(vid)}" in variant_text, \
        "frontmatter варианта различим по asr_variant_id"

    report = json.loads(pathlib.Path(summary["report_file"]).read_text(encoding="utf-8"))
    assert report["replacements_total"] == len(report["replacements"]) == 3
    for rep in report["replacements"]:
        for key in ("canonical", "matched", "replacement", "rule", "offset", "line", "col"):
            assert key in rep, f"в отчёте есть {key}: что, где, из чего"
    # Отчёт полон: правки восстанавливают тело варианта из исходника.
    assert _reconstruct(TRANSCRIPT_MD, report["replacements"]) \
        .endswith(variant_text.split("---\n", 2)[2])


def test_retro_no_changes_no_variant(tmp_path, glossary):
    src = tmp_path / "clean-transcript.md"
    src.write_text("Обычный текст без терминов.\n", encoding="utf-8")
    summary = gc.correct_file(src, glossary)
    assert summary["variant_file"] is None and summary["report_file"] is None
    assert summary["replacements_total"] == 0
    assert list(tmp_path.iterdir()) == [src], "без правок ничего не пишется"


def test_retro_cli_runs(tmp_path, glossary):
    src = tmp_path / "S2-transcript.md"
    src.write_text(TRANSCRIPT_MD, encoding="utf-8")
    rc = gc.main(["retro", "--glossary", str(GLOSSARY_PATH), str(src)])
    assert rc == 0
    assert any("-gloss-" in p.name for p in tmp_path.iterdir() if p != src)


def test_measure_cli_green(capsys):
    rc = gc.main(["measure", "--glossary", str(GLOSSARY_PATH),
                  "--golden", str(GOLDEN_PATH)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "итог: 10 из 10" in out
    assert "итог: 2 из 2" in out
    assert "ИТОГ ЗАМЕРА: ПРОЙДЕН" in out


# ---------------------------------------------------------------------------
# 6. Атомарная запись и BOM
# ---------------------------------------------------------------------------


def test_bom_read_tolerated_own_files_without_bom(tmp_path, glossary):
    src = tmp_path / "bom-transcript.md"
    src.write_text(TRANSCRIPT_MD, encoding="utf-8-sig")  # чужой файл с BOM
    before = src.read_bytes()
    assert before.startswith(b"\xef\xbb\xbf")

    summary = gc.correct_file(src, glossary)

    assert src.read_bytes() == before
    variant_bytes = pathlib.Path(summary["variant_file"]).read_bytes()
    report_bytes = pathlib.Path(summary["report_file"]).read_bytes()
    assert not variant_bytes.startswith(b"\xef\xbb\xbf"), "свои файлы — строго без BOM"
    assert not report_bytes.startswith(b"\xef\xbb\xbf")
    assert not list(tmp_path.glob("*.tmp")), "атомарная запись не оставляет temp"


def test_glossary_read_with_bom(tmp_path):
    path = tmp_path / "gl.json"
    path.write_text(GLOSSARY_PATH.read_text(encoding="utf-8"), encoding="utf-8-sig")
    loaded = gc.read_glossary_file(path)
    assert loaded is not None and loaded["terms"]


def test_write_text_atomic_replaces_existing(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("старое", encoding="utf-8")
    gc.write_text_atomic(target, "новое содержимое")
    assert target.read_text(encoding="utf-8") == "новое содержимое"
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 7. Подсказки: состав, порядок, лимит, источники, kwargs
# ---------------------------------------------------------------------------


def test_initial_prompt_natural_line_weight_order(glossary):
    prompt = gc.build_initial_prompt(glossary)
    assert prompt.startswith("Обсуждение: ")
    assert prompt.endswith(".")
    terms = prompt[len("Обсуждение: "):-1].split(", ")
    assert terms[0] == "экзокортекс", "наибольший weight — первым"
    assert terms[1] == "Claude"
    assert set(terms) == {"экзокортекс", "Claude", "Claude Code", "скилл",
                          "harness", "shared space", "Obsidian", "BoK"}


def test_initial_prompt_token_limit():
    many = {"terms": [
        {"canonical": f"термин{i:03d}", "weight": 1000 - i, "status": "approved"}
        for i in range(300)
    ]}
    prompt = gc.build_initial_prompt(many)
    assert gc.estimate_tokens(prompt) <= gc.INITIAL_PROMPT_TOKEN_LIMIT
    assert "термин000" in prompt, "самый весомый термин не выпадает"
    assert "термин299" not in prompt, "хвост за лимитом отрезан"


def test_glossary_decision_order(tmp_path):
    hub = tmp_path / "hub"
    proj_dir = hub / "700t"
    proj_dir.mkdir(parents=True)
    project_gl = {"version": 1, "terms": [
        {"canonical": "проектный", "weight": 1, "status": "approved"}]}
    # Проектный файл с BOM: чтение обязано пережить чужой BOM.
    (proj_dir / gc.PROJECT_GLOSSARY_FILENAME).write_text(
        json.dumps(project_gl, ensure_ascii=False), encoding="utf-8-sig")
    node_gl_path = tmp_path / "node-glossary.json"
    node_gl_path.write_text(json.dumps({"version": 1, "terms": [
        {"canonical": "узловой", "weight": 1, "status": "approved"}]},
        ensure_ascii=False), encoding="utf-8")
    run_gl_path = tmp_path / "run-glossary.json"
    run_gl_path.write_text(json.dumps({"version": 1, "terms": [
        {"canonical": "запусковый", "weight": 1, "status": "approved"}]},
        ensure_ascii=False), encoding="utf-8")

    base = {"hub_root": str(hub), "project_id": "700t",
            "glossary_path": str(node_gl_path)}

    gl, source = gc.glossary_decision({**base, "glossary_override": str(run_gl_path)})
    assert source == "run" and gl["terms"][0]["canonical"] == "запусковый"

    gl, source = gc.glossary_decision({**base, "glossary_override": "off"})
    assert source == "run" and gl is None, "запуск выключает, к проекту не проваливаемся"

    gl, source = gc.glossary_decision(dict(base))
    assert source == "project" and gl["terms"][0]["canonical"] == "проектный"

    gl, source = gc.glossary_decision(
        {"hub_root": str(hub), "project_id": "нет-такого", "glossary_path": str(node_gl_path)})
    assert source == "node" and gl["terms"][0]["canonical"] == "узловой"

    gl, source = gc.glossary_decision({})
    assert source == "default" and gl is None


def test_media_transcribe_kwargs_and_fingerprint():
    pytest.importorskip("faster_whisper")
    import media_transcribe as mt

    payload = {"glossary_override": str(GLOSSARY_PATH)}
    kwargs = {"beam_size": 5, "vad_filter": True, "condition_on_previous_text": True}
    info = mt.apply_glossary_to_kwargs(payload, kwargs)
    assert kwargs["initial_prompt"].startswith("Обсуждение: ")
    assert "экзокортекс" in kwargs["hotwords"]
    assert info["source"] == "run" and info["fingerprint"]

    off_payload = {"glossary_override": "off"}
    off_kwargs = {"beam_size": 5}
    mt.apply_glossary_to_kwargs(off_payload, off_kwargs)
    assert off_kwargs == {"beam_size": 5}, "без глоссария kwargs прежние до ключа"

    # Отпечаток resume: отсутствие ключа у старых отпечатков == выключено.
    stored_old = {"source_file_resolved": "a", "processing_input_resolved": "b",
                  "source_size_bytes": 1, "duration_sec": 10.0, "chunk_minutes": 20,
                  "chunk_overlap_sec": 30, "selected_model": "medium",
                  "quality_preset": "medium"}
    current_off = {**stored_old, "glossary_prompt": ""}
    assert mt.fingerprint_matches(stored_old, current_off)
    current_on = {**stored_old, "glossary_prompt": info["fingerprint"]}
    assert not mt.fingerprint_matches(stored_old, current_on), \
        "чанки с разными подсказками в одном resume не смешиваются"


def test_watcher_skip_pattern_covers_glossary_name():
    """Имя _PROJECT-glossary.json прикрыто шаблоном ^_PROJECT-.* — сканер не подберёт."""
    import re as _re
    sys.path.insert(0, str(SRC))
    import audio_inbox_watch as aiw
    assert any(_re.match(pat, gc.PROJECT_GLOSSARY_FILENAME)
               for pat in aiw.DEFAULT_SKIP_FILENAME_PATTERNS)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
