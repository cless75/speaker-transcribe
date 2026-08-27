#!/usr/bin/env python
"""Глоссарий проекта: подсказки модели и пост-коррекция расшифровок (801-o11).

Модуль намеренно без тяжёлых зависимостей (stdlib only): пост-коррекция и
ретро-прогон должны работать на любой машине, где нет faster-whisper.
``media_transcribe`` импортирует его как опциональный слой (по образцу
``slide_frames``); обратного импорта нет — иначе цикл.

Три ответственности:

1. Чтение ``{hub_root}/{pid}/_PROJECT-glossary.json`` — по тому же принципу, что
   ``_PROJECT-settings.json``: признак принадлежит проекту, а не машине. Порядок
   разрешения источника: запуск → проект → узел → выключено. Чтение переживает
   BOM у чужого файла (utf-8-sig); свои файлы пишутся строго без BOM.
2. Сборка ``initial_prompt`` (по убыванию weight до лимита 224 токена, одной
   естественной строкой) и ``hotwords`` — только из терминов со status=approved:
   гейт статуса относится к применению термина в прогоне целиком, подсказки —
   часть применения.
3. Пост-коррекция отдельным слоем: правится копия, исходный текст не трогается
   никогда, результат — отдельный вариант (имя через asr_variant_id), каждая
   правка — в отчёт (что, где, из чего). Замена только точная: по ``variants``
   с учётом русских падежных окончаний и по ``multiword_variants`` по точной
   последовательности токенов. Нечёткое сравнение как механизм замены запрещено
   (граница пакета 801-o11).

CLI (подкоманды):

    python src/glossary_correct.py retro   --glossary PATH file1 [file2 ...]
    python src/glossary_correct.py measure --glossary PATH --golden PATH
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import sys

PROJECT_GLOSSARY_FILENAME = "_PROJECT-glossary.json"

# Лимит initial_prompt у Whisper: последние 224 токена. Считаем консервативно
# (байты UTF-8 / 2), чтобы движок гарантированно не резал наш prompt слева —
# при переполнении он выбрасывает первые, то есть самые весомые термины.
INITIAL_PROMPT_TOKEN_LIMIT = 224
INITIAL_PROMPT_PREFIX = "Обсуждение: "

# Закрытый список русских падежных окончаний существительных. Замена «с учётом
# окончания» — это ТОЧНОЕ совпадение «вариант + окончание из списка», а не
# нечёткий подбор: список перечислим и проверяем равенством.
RUSSIAN_ENDINGS = (
    "ами", "ями",
    "ой", "ей", "ёй", "ом", "ем", "ём", "ам", "ям", "ах", "ях",
    "ов", "ев", "ёв", "ью", "ий", "ия", "ие", "ию",
    "а", "я", "у", "ю", "е", "о", "ы", "и",
)

_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_SENTENCE_BREAK_RE = re.compile(r"[.!?…]+|\n")


# ---------------------------------------------------------------------------
# Чтение глоссария и порядок разрешения источника
# ---------------------------------------------------------------------------


def read_glossary_file(path: pathlib.Path | str) -> dict | None:
    """Прочитать файл глоссария. Любая ошибка — None, прогон не роняем.

    utf-8-sig: чужой файл на Хабе может прийти с BOM (Notepad, PowerShell
    редиректы) — чтение обязано это пережить. Свои файлы пишем без BOM.
    """
    try:
        p = pathlib.Path(str(path)).expanduser()
        if not p.is_file():
            return None
        loaded = json.loads(p.read_text(encoding="utf-8-sig"))
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def load_project_glossary(payload: dict) -> dict | None:
    """Глоссарий проекта из Хаба: ``{hub_root}/{pid}/_PROJECT-glossary.json``.

    Тот же контракт, что у ``load_project_settings``: признак принадлежит
    проекту, а не машине; имя попадает под зарезервированный шаблон
    ``^_PROJECT-.*`` в ``DEFAULT_SKIP_FILENAME_PATTERNS`` — сканер файл не
    подберёт. Ошибка чтения — молча None (Хаб бывает не смонтирован).
    """
    cached = payload.get("_project_glossary_cache", Ellipsis)
    if cached is not Ellipsis:
        return cached
    glossary: dict | None = None
    hub_root = payload.get("hub_root")
    pid = payload.get("project_id")
    if hub_root and pid:
        path = pathlib.Path(str(hub_root)).expanduser() / str(pid) / PROJECT_GLOSSARY_FILENAME
        glossary = read_glossary_file(path)
    payload["_project_glossary_cache"] = glossary
    return glossary


def glossary_decision(payload: dict) -> tuple[dict | None, str]:
    """Какой глоссарий действует и кто это решил.

    Порядок — как у word_timestamps: запуск → проект → узел → выключено.
    Уровень «запуск» — ``glossary_override``: путь к файлу либо off/false —
    явное выключение, ниже не проваливаемся (запуск сильнее проекта).
    Уровень «узел» — ``glossary_path`` в конфиге узла.
    """
    override = payload.get("glossary_override")
    if override is not None:
        if override is False or str(override).strip().lower() in {"off", "none", "false", "0", ""}:
            return None, "run"
        return read_glossary_file(str(override)), "run"
    project = load_project_glossary(payload)
    if project is not None:
        return project, "project"
    node_path = payload.get("glossary_path")
    if node_path:
        return read_glossary_file(str(node_path)), "node"
    return None, "default"


# ---------------------------------------------------------------------------
# Подсказки модели: initial_prompt и hotwords (только approved)
# ---------------------------------------------------------------------------


def glossary_terms(glossary: dict | None) -> list[dict]:
    terms = (glossary or {}).get("terms")
    if not isinstance(terms, list):
        return []
    return [t for t in terms if isinstance(t, dict) and str(t.get("canonical") or "").strip()]


def approved_terms(glossary: dict | None) -> list[dict]:
    """Термины, применяемые в прогоне: status=approved — гейт применения.

    proposed попадает в отчёт кандидатов, но ни в замену, ни в подсказки:
    подсказки — тоже применение термина в прогоне (решение 801-o11).
    """
    return [t for t in glossary_terms(glossary) if str(t.get("status") or "") == "approved"]


def _terms_by_weight(glossary: dict | None) -> list[dict]:
    terms = approved_terms(glossary)
    # sorted стабилен: при равном weight сохраняется авторский порядок словаря.
    return sorted(terms, key=lambda t: -(t.get("weight") or 0))


def estimate_tokens(text: str) -> int:
    """Консервативная оценка числа токенов Whisper-BPE: байты UTF-8 / 2."""
    return max(1, math.ceil(len(text.encode("utf-8")) / 2))


def build_initial_prompt(glossary: dict | None,
                         max_tokens: int = INITIAL_PROMPT_TOKEN_LIMIT) -> str | None:
    """Одна естественная строка из канонических написаний по убыванию weight.

    Весь глоссарий в 224 токена не влезет и влезать не должен: weight и есть
    приоритет попадания. Оценка токенов консервативная — движок не должен резать
    prompt слева (он выбрасывает первые токены, то есть самые весомые термины).
    """
    budget = max_tokens - estimate_tokens(INITIAL_PROMPT_PREFIX) - 1  # завершающая точка
    chosen: list[str] = []
    for term in _terms_by_weight(glossary):
        canonical = str(term["canonical"]).strip()
        cost = estimate_tokens(canonical) + 1  # разделитель ", "
        if cost > budget:
            break
        chosen.append(canonical)
        budget -= cost
    if not chosen:
        return None
    return INITIAL_PROMPT_PREFIX + ", ".join(chosen) + "."


def build_hotwords(glossary: dict | None) -> list[str]:
    """Список канонических форм approved-терминов для hotwords."""
    return [str(t["canonical"]).strip() for t in _terms_by_weight(glossary)]


def build_hotwords_string(glossary: dict | None) -> str | None:
    """faster-whisper принимает hotwords строкой — склеиваем список пробелами."""
    words = build_hotwords(glossary)
    return " ".join(words) if words else None


def build_prompt_info(payload: dict) -> dict:
    """Решение по подсказкам для прогона: prompt, hotwords, источник, отпечаток.

    ``fingerprint`` идёт в отпечаток resume: чанки, посчитанные с разными
    подсказками, смешивать нельзя — prompt влияет на само распознавание.
    Пустая строка == подсказки выключены (совместимость со старыми отпечатками,
    у которых ключа нет вовсе).
    """
    glossary, source = glossary_decision(payload)
    prompt = build_initial_prompt(glossary) if glossary else None
    hotwords = build_hotwords_string(glossary) if glossary else None
    fingerprint = ""
    if prompt or hotwords:
        digest = hashlib.sha256(
            ((prompt or "") + "\x00" + (hotwords or "")).encode("utf-8")
        ).hexdigest()
        fingerprint = digest[:16]
    return {
        "source": source,
        "initial_prompt": prompt,
        "hotwords": hotwords,
        "prompt_terms": len(build_hotwords(glossary)) if glossary else 0,
        "fingerprint": fingerprint,
    }


# ---------------------------------------------------------------------------
# Пост-коррекция: точная замена по глоссарию, copy-only
# ---------------------------------------------------------------------------


def _sanitize_token(value: str) -> str:
    """Зеркало media_transcribe.sanitize_token (сюда его импортировать нельзя —
    media_transcribe тянет faster-whisper и сам импортирует этот модуль)."""
    safe = []
    for char in str(value or "").strip():
        if char.isalnum():
            safe.append(char.lower())
        elif char in {"-", "_"}:
            safe.append(char)
        else:
            safe.append("_")
    token = "".join(safe).strip("_")
    return token or "unknown"


def glossary_hash(glossary: dict) -> str:
    """Детерминированный отпечаток содержимого глоссария (для variant_id)."""
    dumped = json.dumps(glossary, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()[:8]


def glossary_variant_id(glossary: dict, base_variant_id: str | None = None) -> str:
    """Идентификатор варианта коррекции — логика derive_asr_variant_id (явная
    ветка: заданный id санитизируется тем же sanitize_token)."""
    suffix = f"gloss-{glossary_hash(glossary)}"
    raw = f"{base_variant_id}-{suffix}" if base_variant_id else suffix
    return _sanitize_token(raw)


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Окно контекстного правила — предложение. Границы: .!?… и перевод строки."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_BREAK_RE.finditer(text):
        spans.append((start, m.end()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def _split_ending(token_lower: str) -> list[tuple[str, str]]:
    """Все разбиения «основа + русское окончание» (включая пустое окончание).

    Основа не короче 3 символов и кончается кириллицей: окончания — свойство
    русских словоформ, к латинице не примеряются.
    """
    out: list[tuple[str, str]] = [(token_lower, "")]
    for ending in RUSSIAN_ENDINGS:
        if len(token_lower) - len(ending) >= 3 and token_lower.endswith(ending):
            stem = token_lower[: len(token_lower) - len(ending)]
            if _CYRILLIC_RE.search(stem[-1]):
                out.append((stem, ending))
    return out


def _render_replacement(canonical: str, ending: str, matched_text: str,
                        case_policy: str) -> str:
    """Каноническое написание с сохранением формы.

    Окончание переносится, только если канон — русское слово (кончается
    кириллицей): «экзопортексом» → «экзокортексом». Для латинского канона
    русское окончание не воспроизводится — ставится канон как есть
    («Клода» → «Claude»); исходная форма остаётся в отчёте.

    case_policy=preserve-sentence-case: капитализация начала предложения
    сохраняется у полностью строчного канона («Экокордекс» → «Экзокортекс»);
    авторский регистр канона (Claude, BoK) не трогается.
    """
    replacement = canonical
    if ending and canonical and _CYRILLIC_RE.search(canonical[-1]):
        replacement = canonical + ending
    if case_policy == "preserve-sentence-case":
        if matched_text[:1].isupper() and replacement == replacement.lower():
            replacement = replacement[0].upper() + replacement[1:]
    return replacement


class GlossaryCorrector:
    """Точная словарная замена по глоссарию. Никакого нечёткого сравнения:
    совпадение либо точное (вариант или вариант+окончание из закрытого списка,
    последовательность токенов для multiword), либо его нет."""

    def __init__(self, glossary: dict):
        self.glossary = glossary or {}
        rules = (self.glossary.get("rules") or {})
        self.case_policy = str(rules.get("case_policy") or "preserve-sentence-case")
        # rules.min_confidence зарезервировано в v1 и намеренно игнорируется.

        self.protect: dict[str, dict] = {}          # одиночная форма -> {"term":, "rules": []}
        # Многословный protect: ТОЧНАЯ последовательность токенов (идиомы
        # «бок о бок», «под боком» — org/bok-homonym-replaced-by-default).
        self.protect_multiword: list[tuple[tuple[str, ...], dict]] = []
        self.variants: dict[str, dict] = {}         # одиночный вариант -> term
        self.multiword: list[tuple[tuple[str, ...], dict]] = []
        self.candidate_variants: dict[str, dict] = {}
        self.candidate_multiword: list[tuple[tuple[str, ...], dict]] = []

        for term in glossary_terms(self.glossary):
            is_approved = str(term.get("status") or "") == "approved"
            if is_approved:
                for form in term.get("protect") or []:
                    form_l = " ".join(str(form).strip().lower().split())
                    if not form_l:
                        continue
                    frules = [
                        r for r in (term.get("context_rules") or [])
                        if isinstance(r, dict)
                        and " ".join(str(r.get("form") or "").strip().lower().split()) == form_l
                    ]
                    if " " in form_l:
                        self.protect_multiword.append(
                            (tuple(form_l.split()),
                             {"term": term, "rules": frules, "form": form_l}))
                    else:
                        self.protect[form_l] = {"term": term, "rules": frules}
            var_map = self.variants if is_approved else self.candidate_variants
            mw_list = self.multiword if is_approved else self.candidate_multiword
            for variant in term.get("variants") or []:
                v = str(variant).strip().lower()
                if not v:
                    continue
                if " " in v:
                    mw_list.append((tuple(v.split()), term))
                else:
                    var_map[v] = term
            for variant in term.get("multiword_variants") or []:
                v = str(variant).strip().lower()
                if v:
                    mw_list.append((tuple(v.split()), term))
        # Длинные последовательности первыми: точное совпадение большего окна сильнее.
        self.multiword.sort(key=lambda item: -len(item[0]))
        self.candidate_multiword.sort(key=lambda item: -len(item[0]))
        self.protect_multiword.sort(key=lambda item: -len(item[0]))

    # -- контекст ------------------------------------------------------------

    def _context_allows(self, rules: list[dict], sentence: str,
                        span: tuple[int, int], sent_start: int) -> bool:
        """Условная замена защищённой формы: только при соседях из
        replace_if_near и никогда при never_if_near. never сильнее replace."""
        if not rules:
            return False
        # Токены предложения без самой заменяемой формы.
        tokens = [
            m.group(0).lower()
            for m in _WORD_RE.finditer(sentence)
            if not (sent_start + m.start() >= span[0] and sent_start + m.end() <= span[1])
        ]
        sentence_l = sentence.lower()

        def near_matches(entry: str) -> bool:
            e = entry.strip().lower()
            if not e:
                return False
            if " " in e:
                return e in sentence_l
            # По префиксу: запись «облак» ловит «облако/облаках». Это условие
            # контекста, не механизм замены — сама замена остаётся точной.
            return any(tok.startswith(e) for tok in tokens)

        for rule in rules:
            if any(near_matches(str(e)) for e in rule.get("never_if_near") or []):
                return False
        for rule in rules:
            if any(near_matches(str(e)) for e in rule.get("replace_if_near") or []):
                return True
        return False

    # -- сопоставление -------------------------------------------------------

    @staticmethod
    def _match_sequence(tokens: list[tuple[str, int, int]], i: int,
                        vt: tuple[str, ...]):
        """Точная последовательность токенов; окончание — только у последнего.
        Возвращает (span, ending, consumed) либо None."""
        n = len(vt)
        if i + n > len(tokens):
            return None
        window = tokens[i: i + n]
        if any(window[j][0].lower() != vt[j] for j in range(n - 1)):
            return None
        last = window[-1][0].lower()
        ending = None
        if last == vt[-1]:
            ending = ""
        else:
            for stem, end in _split_ending(last):
                if end and stem == vt[-1]:
                    ending = end
                    break
        if ending is None:
            return None
        return (window[0][1], window[-1][2]), ending, n

    def _try_multiword(self, tokens: list[tuple[str, int, int]], i: int,
                       table: list[tuple[tuple[str, ...], dict]]):
        for vt, term in table:
            found = self._match_sequence(tokens, i, vt)
            if found is None:
                continue
            span, ending, consumed = found
            return {
                "term": term, "ending": ending, "rule": "multiword",
                "span": span, "consumed": consumed,
            }
        return None

    def _try_protect_multiword(self, tokens: list[tuple[str, int, int]], i: int):
        """Многословный protect: идиома защищается как точная последовательность
        и сильнее одиночного варианта своего же термина («бок о бок» не трогает
        ни один «бок», даже когда «бок» лежит в variants)."""
        for vt, entry in self.protect_multiword:
            found = self._match_sequence(tokens, i, vt)
            if found is None:
                continue
            span, ending, consumed = found
            return {
                "term": entry["term"], "ending": ending, "rule": "context",
                "span": span, "consumed": consumed,
                "rules": entry["rules"], "form": entry["form"],
            }
        return None

# Замечание к приоритету (две находки ретро-прогона, 24.08):
# 1) protect «cloud» термина Claude гасил «cloud code» ЧУЖОГО термина
#    Claude Code — 4 незаменённых вхождения;
# 2) первая починка (блок только «своего» термина) дала регрессию на боевом
#    глоссарии: protect «кортекс» у «экзокортекса» гасил СОБСТВЕННУЮ
#    последовательность «за кортексом».
# Итоговое правило: одиночный protect многословную замену не гасит ВООБЩЕ —
# точная многословная последовательность сама снимает неоднозначность, ради
# которой форма защищалась. Одиночная защита действует ровно там, где форма
# стоит ВНЕ последовательности: голое «кортекс» ловится одиночным шагом
# прохода и уходит в protected_kept.

    def _try_single(self, tokens: list[tuple[str, int, int]], i: int,
                    table: dict[str, dict]):
        tok, start, end = tokens[i]
        for stem, ending in _split_ending(tok.lower()):
            term = table.get(stem)
            if term is not None:
                return {
                    "term": term, "ending": ending, "rule": "variant",
                    "span": (start, end), "consumed": 1,
                }
        return None

    def _try_protect(self, tokens: list[tuple[str, int, int]], i: int):
        tok, start, end = tokens[i]
        for stem, ending in _split_ending(tok.lower()):
            entry = self.protect.get(stem)
            if entry is not None:
                return {
                    "term": entry["term"], "ending": ending, "rule": "context",
                    "span": (start, end), "consumed": 1,
                    "rules": entry["rules"], "form": stem,
                }
        return None

    # -- проход --------------------------------------------------------------

    def correct(self, text: str, base_offset: int = 0) -> tuple[str, dict]:
        """Коррекция копии текста. Возвращает (новый текст, отчёт).

        Отчёт: replacements — каждая правка (что, где, из чего);
        candidates — совпадения proposed-терминов (в замену не идут);
        protected_kept — защищённые формы, оставленные без замены.
        base_offset сдвигает координаты отчёта (например, на длину frontmatter).
        """
        tokens = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
        spans = _sentence_spans(text)

        def locate(offset: int) -> tuple[int, int]:
            line = text.count("\n", 0, offset) + 1
            col = offset - (text.rfind("\n", 0, offset) + 1) + 1
            return line, col

        def sentence_of(offset: int) -> tuple[int, int]:
            for s, e in spans:
                if s <= offset < e:
                    return s, e
            return spans[-1] if spans else (0, len(text))

        replacements: list[dict] = []
        candidates: list[dict] = []
        protected_kept: list[dict] = []
        pieces: list[str] = []
        cursor = 0
        i = 0
        while i < len(tokens):
            # Порядок силы: многословный protect (точная последовательность,
            # идиомы) → многословный вариант (одиночный protect его не гасит
            # НИКОГДА — ни чужой, ни свой: точная последовательность сама
            # снимает неоднозначность, см. замечание к приоритету выше) →
            # одиночный protect (форма ВНЕ последовательности) → одиночный
            # вариант.
            match = self._try_protect_multiword(tokens, i)
            if match is None:
                match = self._try_multiword(tokens, i, self.multiword)
            if match is None:
                match = self._try_protect(tokens, i)
            if match is None:
                match = self._try_single(tokens, i, self.variants)
            if match is not None and match["rule"] == "context":
                span = match["span"]
                sent_start, sent_end = sentence_of(span[0])
                allowed = self._context_allows(
                    match["rules"], text[sent_start:sent_end], span, sent_start)
                if not allowed:
                    line, col = locate(span[0])
                    protected_kept.append({
                        "form": match["form"],
                        "matched": text[span[0]:span[1]],
                        "offset": base_offset + span[0],
                        "line": line, "col": col,
                        "canonical": match["term"].get("canonical"),
                    })
                    i += match["consumed"]
                    continue
            if match is None:
                cand = (self._try_multiword(tokens, i, self.candidate_multiword)
                        or self._try_single(tokens, i, self.candidate_variants))
                if cand is not None:
                    span = cand["span"]
                    line, col = locate(span[0])
                    candidates.append({
                        "canonical": cand["term"].get("canonical"),
                        "status": cand["term"].get("status"),
                        "matched": text[span[0]:span[1]],
                        "offset": base_offset + span[0],
                        "line": line, "col": col,
                        "note": "status!=approved: в отчёт кандидатов, не в замену",
                    })
                    i += cand["consumed"]
                    continue
                i += 1
                continue
            span = match["span"]
            matched_text = text[span[0]:span[1]]
            canonical = str(match["term"].get("canonical") or "")
            replacement = _render_replacement(
                canonical, match["ending"], matched_text, self.case_policy)
            line, col = locate(span[0])
            replacements.append({
                "canonical": canonical,
                "matched": matched_text,
                "replacement": replacement,
                "rule": match["rule"],
                "offset": base_offset + span[0],
                "length": span[1] - span[0],
                "line": line, "col": col,
            })
            pieces.append(text[cursor:span[0]])
            pieces.append(replacement)
            cursor = span[1]
            i += match["consumed"]
        pieces.append(text[cursor:])
        report = {
            "replacements": replacements,
            "candidates": candidates,
            "protected_kept": protected_kept,
        }
        return "".join(pieces), report


def correct_text(text: str, glossary: dict, base_offset: int = 0) -> tuple[str, dict]:
    return GlossaryCorrector(glossary).correct(text, base_offset=base_offset)


# ---------------------------------------------------------------------------
# Файловый слой: атомарная запись, frontmatter, вариант рядом с исходником
# ---------------------------------------------------------------------------


def write_text_atomic(target: pathlib.Path, text: str) -> None:
    """temp + os.replace: корпус живёт на облачном диске. UTF-8 строго без BOM."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, target)


_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_VARIANT_LINE_RE = re.compile(r"^asr_variant_id:\s*(.*)$", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter, body). Коррекция в YAML-шапку не лезет: там пути и id."""
    m = _FRONTMATTER_RE.match(text)
    if m:
        return text[: m.end()], text[m.end():]
    return "", text


def _base_variant_id(frontmatter: str) -> str | None:
    m = _VARIANT_LINE_RE.search(frontmatter)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        parsed = json.loads(raw)
        return str(parsed) if parsed else None
    except ValueError:
        return raw.strip('"') or None


def correct_file(source: pathlib.Path | str, glossary: dict,
                 glossary_path: str | None = None) -> dict:
    """Пост-коррекция одного файла расшифровки. copy-only: исходник не трогается.

    Вариант и отчёт ложатся рядом с исходником:
        {stem}-{variant_id}{ext} и {stem}-{variant_id}-report.json
    Пишутся только при наличии правок; итог возвращается всегда.
    """
    src = pathlib.Path(str(source)).expanduser()
    text = src.read_text(encoding="utf-8-sig")  # переживаем чужой BOM
    frontmatter, body = _split_frontmatter(text)
    corrected_body, report = correct_text(body, glossary, base_offset=len(frontmatter))
    variant_id = glossary_variant_id(glossary, _base_variant_id(frontmatter))

    summary = {
        "schema": "glossary-correction-report-v1",
        "source_file": str(src),
        "asr_variant_id": variant_id,
        "glossary_path": glossary_path,
        "glossary_sha": glossary_hash(glossary),
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "replacements_total": len(report["replacements"]),
        "replacements": report["replacements"],
        "candidates": report["candidates"],
        "protected_kept": report["protected_kept"],
        "variant_file": None,
        "report_file": None,
    }
    if not report["replacements"]:
        return summary

    out_fm = frontmatter
    if frontmatter and _VARIANT_LINE_RE.search(frontmatter):
        out_fm = _VARIANT_LINE_RE.sub(
            f"asr_variant_id: {json.dumps(variant_id)}", frontmatter, count=1)
    variant_path = src.with_name(f"{src.stem}-{variant_id}{src.suffix}")
    report_path = src.with_name(f"{src.stem}-{variant_id}-report.json")
    write_text_atomic(variant_path, out_fm + corrected_body)
    summary["variant_file"] = str(variant_path)
    summary["report_file"] = str(report_path)
    write_text_atomic(report_path, json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI: ретро-прогон
# ---------------------------------------------------------------------------


def run_retro(args: argparse.Namespace) -> int:
    glossary = read_glossary_file(args.glossary)
    if glossary is None:
        print(f"ОШИБКА: глоссарий не прочитан: {args.glossary}", file=sys.stderr)
        return 2
    paths = [pathlib.Path(p) for p in (args.paths or [])]
    if args.files_from:
        listed = pathlib.Path(args.files_from).read_text(encoding="utf-8-sig")
        paths += [pathlib.Path(line.strip()) for line in listed.splitlines() if line.strip()]
    if not paths:
        print("ОШИБКА: не задан ни один файл (позиционно или --files-from)", file=sys.stderr)
        return 2
    failures = 0
    total_repl = 0
    for path in paths:
        try:
            summary = correct_file(path, glossary, glossary_path=str(args.glossary))
        except OSError as exc:
            failures += 1
            print(f"FAIL {path}: {exc}")
            continue
        total_repl += summary["replacements_total"]
        if summary["variant_file"]:
            print(f"ok   {path.name}: правок {summary['replacements_total']} "
                  f"-> {pathlib.Path(summary['variant_file']).name}")
        else:
            print(f"ok   {path.name}: правок нет, вариант не создан")
        for rep in summary["replacements"]:
            print(f"       строка {rep['line']}: «{rep['matched']}» -> "
                  f"«{rep['replacement']}» [{rep['rule']}]")
        for cand in summary["candidates"]:
            print(f"       кандидат (proposed): «{cand['matched']}» ~ {cand['canonical']}")
    print(f"итого: файлов {len(paths)}, правок {total_repl}, ошибок {failures}")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI: раннер замера 1 (пост-коррекция по контрольному набору, по классам)
# ---------------------------------------------------------------------------

# Классы кейсов — из приёмки 801-o11 (замер 1). Ключ — observed в нижнем регистре.
# Переразметки по вердиктам владельца:
# - org/term-canon-by-nature-cyrillic-concepts (22.08): латиница exocortex —
#   обычный заменяемый вариант «экзокортекса», кейс в безусловном классе;
# - org/bok-homonym-replaced-by-default (24.08): «бок» заменяется по умолчанию
#   (146:1 по корпусу), context_rules у BoK сняты — «даю бок» и «положить бок»
#   стали безусловными. Условными остались только омонимы Claude.
# Смысл замера — показать, КАКИМ механизмом сработал каждый кейс: вывод
# печатает правило (variant/multiword/context) по каждой правке.
MEASURE_UNCONDITIONAL = {
    "экзопортексом", "экокордекс", "за кортексом", "хармнес",
    "chart space", "клода", "обсидиант", "exocortex",
    "даю бок", "положить бок",
}
MEASURE_CONDITIONAL = {"кладе", "install cloud"}
MEASURE_OUT_OF_SCOPE: set[str] = set()

# Контроль «защищённые формы не тронуты»: одиночный protect без выполненного
# условия и многословный protect (идиомы — точные последовательности).
# Последняя фраза — единственное прямое значение «бок» на весь корпус 700
# (S20260605, довод вердикта 146:1). Синтетика объявлена в выводе.
PROTECTED_CONTROL_PHRASES = [
    "Cloud EMD стоит в каждом проекте компании.",
    "Сессия пишет логи в Google Cloud.",
    "Мы работали бок о бок весь день.",
    "сидит этих человек с боку с рукой",
    "Моторная кора, то есть кортекс, отвечает за движение.",
]


def _canonical_present(corrected: str, canonical: str) -> bool:
    pattern = r"(?<![^\W_])" + re.escape(canonical.lower()) + r"(?![^\W\d_]{4,})"
    return re.search(pattern, corrected.lower()) is not None


def _case_ok(case: dict, corrected: str, report: dict) -> bool:
    observed = str(case["observed"]).lower()
    canonical = str(case["canonical"])
    gone = observed not in corrected.lower()
    present = _canonical_present(corrected, canonical)
    traced = bool(report["replacements"])
    return gone and present and traced


def run_measure(args: argparse.Namespace) -> int:
    glossary = read_glossary_file(args.glossary)
    if glossary is None:
        print(f"ОШИБКА: глоссарий не прочитан: {args.glossary}", file=sys.stderr)
        return 2
    golden_path = pathlib.Path(args.golden)
    try:
        golden = json.loads(golden_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"ОШИБКА: контрольный набор не прочитан: {exc}", file=sys.stderr)
        return 2
    corrector = GlossaryCorrector(glossary)

    classes: dict[str, list[str]] = {
        "unconditional": [], "conditional": [], "out_of_scope": [], "unknown": []}
    passed = {"unconditional": 0, "conditional": 0}
    trace_violations: list[str] = []

    print(f"Замер 1 — пост-коррекция. Глоссарий: {args.glossary}")
    print(f"Контрольный набор: {golden_path} (кейсов: {len(golden.get('cases') or [])})")
    print()
    for case in golden.get("cases") or []:
        observed = str(case.get("observed") or "")
        fragment = str(case.get("fragment") or "")
        key = observed.lower()
        corrected, report = corrector.correct(fragment)
        if key in MEASURE_OUT_OF_SCOPE:
            note = ("без изменения — вне классов замера"
                    if corrected == fragment else "ИЗМЕНЁН (не ожидалось)")
            classes["out_of_scope"].append(f"  вне    «{observed}»: {note}")
            continue
        cls = ("unconditional" if key in MEASURE_UNCONDITIONAL
               else "conditional" if key in MEASURE_CONDITIONAL else "unknown")
        ok = _case_ok(case, corrected, report)
        if cls in passed and ok:
            passed[cls] += 1
        mark = "ok  " if ok else "FAIL"
        detail = "; ".join(
            f"«{r['matched']}»->«{r['replacement']}» [{r['rule']}] стр.{r['line']}"
            for r in report["replacements"]) or "правок нет"
        classes.setdefault(cls, []).append(f"  {mark} «{observed}» -> {case.get('canonical')}: {detail}")
        # Прослеживаемость: изменённый текст обязан быть покрыт отчётом.
        if corrected != fragment and not report["replacements"]:
            trace_violations.append(observed)

    # Защищённые формы без условия — на контрольных фразах.
    protect_violations: list[str] = []
    for phrase in PROTECTED_CONTROL_PHRASES:
        corrected, report = corrector.correct(phrase)
        if corrected != phrase:
            protect_violations.append(
                f"  НАРУШЕНИЕ: «{phrase}» -> «{corrected}»")

    n_uncond = len(classes["unconditional"])
    n_cond = len(classes["conditional"])
    print(f"Безусловная замена (variants + multiword_variants), порог 10 из 10:")
    print("\n".join(classes["unconditional"]))
    print(f"  итог: {passed['unconditional']} из {n_uncond}")
    print()
    print(f"Условная замена (context_rules, омонимы и protect), порог 2 из 2:")
    print("\n".join(classes["conditional"]))
    print(f"  итог: {passed['conditional']} из {n_cond}")
    print()
    print("Вне замера:")
    print("\n".join(classes["out_of_scope"]) or "  —")
    if classes["unknown"]:
        print()
        print("Кейсы вне известных классов:")
        print("\n".join(classes["unknown"]))
    print()
    print("Защищённые формы не тронуты (контрольные фразы без условия), порог 0 нарушений:")
    print("\n".join(protect_violations) or "  нарушений нет")
    print()
    print("Правки прослеживаются (что, где, из чего):",
          "да" if not trace_violations else f"НЕТ: {trace_violations}")

    ok_all = (passed["unconditional"] == n_uncond == 10
              and passed["conditional"] == n_cond == 2
              and not protect_violations
              and not trace_violations)
    print()
    print("ИТОГ ЗАМЕРА:", "ПРОЙДЕН" if ok_all else "НЕ ПРОЙДЕН")
    return 0 if ok_all else 1


def main(argv: list[str] | None = None) -> int:
    try:  # консоль узлов бывает cp1251 — не роняем вывод на не-ASCII
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_retro = sub.add_parser(
        "retro", help="ретро-прогон пост-коррекции по списку расшифровок (copy-only)")
    p_retro.add_argument("--glossary", required=True, help="путь к файлу глоссария")
    p_retro.add_argument("paths", nargs="*", help="файлы расшифровок")
    p_retro.add_argument("--files-from", default=None,
                         help="файл со списком путей, по одному на строку")
    p_retro.set_defaults(func=run_retro)

    p_measure = sub.add_parser(
        "measure", help="замер 1: контрольный набор по классам кейсов (801-o11)")
    p_measure.add_argument("--glossary", required=True, help="путь к файлу глоссария")
    p_measure.add_argument("--golden", required=True,
                           help="путь к 801-o11-golden-phrases.json")
    p_measure.set_defaults(func=run_measure)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
