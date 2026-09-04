"""Детектор залипания модели: повтор, возврат подсказки, участок без пунктуации.

Зачем модуль существует. На низкоинформативном участке аудио модель перестаёт
распознавать речь и минутами повторяет одну строку — строку из ближайшего
аттрактора: подсказки глоссария, артефакта обучающих данных или недавнего
заметного слова. 02.09 так потеряно 418 с в прогоне large-v3 и 491 с в прогоне
medium из 48 минут разговора, **и оба прогона закончились статусом ok**: ни один
слой не смотрел на содержимое выхода (801-o15).

Здесь только чистые функции над списком сегментов ``[{start, end, text, ...}]``:
ни модели, ни диска, ни сети. Повторный проход зоны и гейт живут в
``media_transcribe`` и ``jobs_queue`` — модуль обязан оставаться проверяемым
синтетическими фикстурами, потому что репозиторий публичный и реальных
расшифровок в тестах быть не может.

Три признака, каждый — равенство и порог, без эвристик «на глаз»:

* **A · повтор** — ``repeat_min_segments`` подряд сегментов, нормализованный
  текст которых совпадает с якорем зоны либо является его префиксом (или якорь
  префикс их). Метка ``repetition``.
* **B · возврат подсказки** — сегмент, который повторяет не речь, а термины
  подсказки. Две формы, обе считаются по **фактическому** ``initial_prompt``
  прогона, а не по глоссарию: подсказка обрезана лимитом 224 токена, и совпадать
  надо с тем, что реально ушло в модель. Метка ``prompt_loop``.
* **C · нет пунктуации** — ``no_punctuation_min_sec`` подряд без ``.!?``. Метка
  ``no_punctuation``, **только предупреждение**: в ``suspect_sec`` и в гейт не
  входит.

Почему у B две формы. Требование описывает признак как «≥3 канонических термина
подряд в том же порядке, что в подсказке» (форма B1) — и этой формой ловятся три
зоны из четырёх. Четвёртая, «Воркспейс воркспейс» в прогоне medium, состоит из
**одного** термина, повторённого внутри сегмента; 801-o15 в таблице приёмки ждёт
на ней метку ``prompt_loop``, а не ``repetition``, потому что источник строки —
глоссарий. Форма B2 («доля символов терминов ≥ порога при повторе токенов внутри
сегмента») и есть этот случай; без неё приёмка 4/4 недостижима, а метка зоны
называет не тот источник. Приоритет 801-o15 над формулировкой задания — прямое
указание пакета.
"""
from __future__ import annotations

import re
from typing import Iterable


# Умолчания. Значения выше — то, что 801-o15 назвал числами; они же лежат в
# config/node.example.json под ключом loop_guard и переопределяются на узле.
DEFAULTS: dict[str, float | int] = {
    # A: сколько подряд одинаковых сегментов делают повтор залипанием
    "repeat_min_segments": 3,
    # Зона короче этого не считается потерей: три подряд «Да.» — заминка речи, а
    # не петля модели. Порог стоит на длительности, а не на длине строки: мера
    # пакета — потерянные секунды, и отбирать по числу букв значило бы мерить
    # не то. Все четыре известные зоны 02.09 длятся от 132 до 286 с.
    "min_zone_sec": 10.0,
    # B1: сколько терминов подсказки подряд в порядке подсказки
    "prompt_terms_min": 3,
    # B: доля символов терминов в сегменте
    "prompt_char_ratio": 0.6,
    # C: длительность участка без .!?
    "no_punctuation_min_sec": 60.0,
    # gaps: участок без распознанной речи длиннее этого считается пропуском
    "gap_min_sec": 10.0,
    # gaps: плотность текста, ниже которой длинный сегмент — не речь, а пропуск.
    # Медиана по обеим записям 02.09 — 7,9–11,6 симв/с; зона 4:05–4:41 в
    # revyu-obzora, ради которой мера заведена, даёт 1,3 симв/с за 35 с и одним
    # правилом «нет сегментов» не ловится: сегмент там есть, речи в нём нет.
    "gap_max_chars_per_sec": 2.0,
    # гейт
    "suspect_ratio_max": 0.03,
    "gaps_sec_max": 30.0,
    # повторный проход: поля слева и справа от зоны, чтобы не рвать фразу
    "rescan_pad_sec": 5.0,
    "evidence_chars": 120,
}

REPETITION = "repetition"
PROMPT_LOOP = "prompt_loop"
NO_PUNCTUATION = "no_punctuation"

# Метки, входящие в suspect_sec и в гейт. no_punctuation — предупреждение.
GATED_KINDS = (PROMPT_LOOP, REPETITION)
# Чем сильнее метка, тем точнее она называет источник строки: зона, где сегмент
# повторяет подсказку, называется prompt_loop, даже если он же попадает под A.
KIND_PRIORITY = {PROMPT_LOOP: 3, REPETITION: 2, NO_PUNCTUATION: 1}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?]")
# Между терминами подсказки в залипшем сегменте стоят только разделители: если
# там оказалось слово, последовательность прервалась и это уже речь.
_SEPARATORS = re.compile(r"^[\s,;:.\-—…]*$", re.UNICODE)


def resolve_config(source: dict | None) -> dict:
    """Умолчания, поверх которых легли значения узла (``payload['loop_guard']``)."""
    config = dict(DEFAULTS)
    block = (source or {}).get("loop_guard") if isinstance(source, dict) else None
    if isinstance(block, dict):
        for key, value in block.items():
            if key in DEFAULTS and value is not None:
                config[key] = type(DEFAULTS[key])(value)
    return config


def normalize(text: object) -> str:
    """Нижний регистр, снятие пунктуации, схлопывание пробелов."""
    lowered = str(text or "").lower()
    return _SPACES.sub(" ", _PUNCT.sub(" ", lowered)).strip()


def prompt_terms(initial_prompt: str | None) -> list[str]:
    """Канонические термины подсказки — в том порядке, в каком они в ней стоят.

    Разбор идёт по форме самой строки, а не по знанию о префиксе «Обсуждение: »:
    форма подсказки — предмет отдельного замера (П1/П2/П3 в 801-o15), и детектор
    не должен ломаться, когда её сменят. Всё до первого двоеточия в первом
    элементе — вводная, остальное — перечисление через запятую.
    """
    raw = str(initial_prompt or "").strip()
    if not raw:
        return []
    parts = raw.split(",")
    if ":" in parts[0]:
        parts[0] = parts[0].split(":", 1)[1]
    terms: list[str] = []
    for part in parts:
        term = normalize(part)
        if term and term not in terms:
            terms.append(term)
    return terms


def _term_matches(norm_text: str, terms: list[str]) -> list[tuple[int, int, int]]:
    """Вхождения терминов в нормализованный текст: ``(начало, конец, индекс)``.

    Слева направо, на каждой позиции — самый длинный подходящий термин: иначе
    «Claude Code» распадётся на «Claude», и порядок терминов перестанет
    совпадать с порядком подсказки.
    """
    if not terms or not norm_text:
        return []
    order = sorted(range(len(terms)), key=lambda i: -len(terms[i]))
    matches: list[tuple[int, int, int]] = []
    position = 0
    length = len(norm_text)
    while position < length:
        if norm_text[position] == " ":
            position += 1
            continue
        hit = None
        for index in order:
            term = terms[index]
            if not norm_text.startswith(term, position):
                continue
            after = position + len(term)
            if after < length and norm_text[after] not in " ":
                continue  # часть более длинного слова — не термин
            hit = (position, after, index)
            break
        if hit is None:
            space = norm_text.find(" ", position)
            position = length if space < 0 else space + 1
            continue
        matches.append(hit)
        position = hit[1]
    return matches


def prompt_echo(text: object, terms: list[str], config: dict) -> dict | None:
    """Признак B для одного сегмента: вернуть разбор либо ``None``.

    B1 — ``prompt_terms_min`` терминов подряд (между ними только разделители) с
    возрастающими позициями в подсказке. B2 — те же термины, но повторяющиеся
    внутри сегмента: уникальных токенов вдвое меньше, чем токенов. Обе формы
    требуют, чтобы термины занимали ``prompt_char_ratio`` символов сегмента, —
    ровно это отделяет залипание от фразы, в которой термин просто прозвучал.
    """
    norm = normalize(text)
    if not norm or not terms:
        return None
    matches = _term_matches(norm, terms)
    if not matches:
        return None
    body = norm.replace(" ", "")
    if not body:
        return None
    term_chars = sum(len(norm[start:end].replace(" ", "")) for start, end, _ in matches)
    ratio = term_chars / len(body)
    if ratio < float(config["prompt_char_ratio"]):
        return None
    run = best = 1
    for previous, current in zip(matches, matches[1:]):
        adjacent = bool(_SEPARATORS.match(norm[previous[1]:current[0]]))
        run = run + 1 if adjacent and current[2] > previous[2] else 1
        best = max(best, run)
    tokens = norm.split(" ")
    repeated = len(tokens) >= 2 and len(set(tokens)) * 2 <= len(tokens)
    if best >= int(config["prompt_terms_min"]):
        return {"form": "B1", "ratio": round(ratio, 3), "terms_in_row": best}
    if repeated:
        return {"form": "B2", "ratio": round(ratio, 3), "terms_in_row": best}
    return None


def _repetition_runs(segments: list[dict], config: dict) -> set[int]:
    """Индексы сегментов, попавших в повтор (признак A)."""
    minimum = int(config["repeat_min_segments"])
    marked: set[int] = set()
    start = 0
    normalized = [normalize(seg.get("text")) for seg in segments]
    while start < len(segments):
        anchor = normalized[start]
        end = start + 1
        if anchor:
            while end < len(segments):
                other = normalized[end]
                if not other or not (other.startswith(anchor) or anchor.startswith(other)):
                    break
                end += 1
        if anchor and end - start >= minimum:
            marked.update(range(start, end))
        start = end
    return marked


def _no_punctuation_runs(segments: list[dict], config: dict) -> set[int]:
    """Индексы сегментов из участков без ``.!?`` длиннее порога (признак C)."""
    limit = float(config["no_punctuation_min_sec"])
    marked: set[int] = set()
    start = 0
    while start < len(segments):
        end = start
        while end < len(segments) and not _SENTENCE_END.search(str(segments[end].get("text") or "")):
            end += 1
        if end > start:
            span = float(segments[end - 1].get("end") or 0) - float(segments[start].get("start") or 0)
            if span >= limit:
                marked.update(range(start, end))
            start = end
        else:
            start += 1
    return marked


def _evidence(segments: list[dict], indexes: list[int], config: dict) -> tuple[str, int]:
    """Самая частая строка зоны и число её повторов."""
    counts: dict[str, int] = {}
    sample: dict[str, str] = {}
    for index in indexes:
        text = str(segments[index].get("text") or "").strip()
        key = normalize(text)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        sample.setdefault(key, text)
    if not counts:
        return "", 0
    key = max(counts, key=lambda item: (counts[item], len(item)))
    return sample[key][: int(config["evidence_chars"])], counts[key]


def detect_zones(segments: Iterable[dict], initial_prompt: str | None = None,
                 config: dict | None = None) -> list[dict]:
    """Зоны залипания: смежные сегменты одной метки, склеенные в один интервал.

    Склейка идёт **по одинаковой метке**, а не по любой помеченности: в прогоне
    medium 02.09 зона подсказки (2370,0–2599,5) стоит вплотную к зоне повтора
    (2628,8–…), и склейка «всего помеченного» превратила бы две разные потери в
    одну, назвав вторую чужим источником.
    """
    config = dict(config or DEFAULTS)
    items = list(segments)
    if not items:
        return []
    terms = prompt_terms(initial_prompt)
    repetition = _repetition_runs(items, config)
    silence = _no_punctuation_runs(items, config)
    kinds: list[str | None] = []
    for index, segment in enumerate(items):
        if terms and prompt_echo(segment.get("text"), terms, config):
            kinds.append(PROMPT_LOOP)
        elif index in repetition:
            kinds.append(REPETITION)
        elif index in silence:
            kinds.append(NO_PUNCTUATION)
        else:
            kinds.append(None)
    zones: list[dict] = []
    index = 0
    while index < len(items):
        kind = kinds[index]
        if kind is None:
            index += 1
            continue
        end = index
        while end + 1 < len(items) and kinds[end + 1] == kind:
            end += 1
        members = list(range(index, end + 1))
        evidence, repeats = _evidence(items, members, config)
        start_sec = round(float(items[index].get("start") or 0), 3)
        end_sec = round(float(items[end].get("end") or 0), 3)
        index = end + 1
        # Хвост зоны C, у которой середину забрали A или B, сам по себе порога уже
        # не держит: предупреждение о минуте без пунктуации не выписывается на
        # четыре секунды.
        floor = (float(config["min_zone_sec"]) if kind in GATED_KINDS
                 else float(config["no_punctuation_min_sec"]))
        if end_sec - start_sec < floor:
            continue
        zones.append({
            "start": start_sec,
            "end": end_sec,
            "kind": kind,
            "evidence": evidence,
            "repeats": repeats,
            "segments": [members[0], members[-1]],
        })
    return zones


def looks_stuck(segments: Iterable[dict], initial_prompt: str | None = None,
                config: dict | None = None) -> bool:
    """Залипает ли выход повторного прохода по признакам A и B.

    C сюда не входит намеренно: участок без пунктуации — предупреждение, и
    отбрасывать по нему восстановленный текст значило бы менять пустотой то, что
    удалось расслышать.
    """
    strict = dict(config or DEFAULTS)
    # Порог длительности зоны здесь снимается: он отсекает заминку речи в целой
    # записи, а тут проверяется короткий кусок, и залипание в нём короче порога
    # по определению. Подставить залипший текст обратно было бы хуже пустоты.
    strict["min_zone_sec"] = 0.0
    zones = detect_zones(segments, initial_prompt, strict)
    return any(zone["kind"] in GATED_KINDS for zone in zones)


def _speech_intervals(items: list[dict], config: dict) -> list[tuple[float, float]]:
    """Интервалы, где распознана речь.

    Длинный сегмент с плотностью текста ниже ``gap_max_chars_per_sec`` речью не
    считается: пропуск 4:05–4:41 в ``revyu-obzora`` — это не «нет сегментов», а
    один сегмент на 35 с с одной строкой внутри. Правило «нет сегментов» его не
    видит, и ровно поэтому 36 с потери прошли мимо приёмки.
    """
    limit = float(config["gap_min_sec"])
    density_floor = float(config["gap_max_chars_per_sec"])
    speech: list[tuple[float, float]] = []
    for segment in items:
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or 0)
        span = end - start
        text = str(segment.get("text") or "").strip()
        if span >= limit and (len(text) / span if span > 0 else 0) < density_floor:
            continue
        speech.append((start, end))
    return sorted(speech)


def _subtract(spans: list[tuple[float, float]],
              cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Что остаётся от ``spans`` после вычитания ``cuts``."""
    result = list(spans)
    for cut_start, cut_end in cuts:
        carried: list[tuple[float, float]] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                carried.append((start, end))
                continue
            if start < cut_start:
                carried.append((start, cut_start))
            if cut_end < end:
                carried.append((cut_end, end))
        result = carried
    return result


def measure_gaps(segments: Iterable[dict], config: dict | None = None,
                 zones: Iterable[dict] | None = None) -> float:
    """Секунды без распознанной речи, не объяснённые залипанием.

    Считаются только внутренние промежутки — от первого сегмента до последнего.
    Тишина в начале и в конце есть у всякой записи и говорит о монтаже, а не о
    потерянной речи. Участки, уже вошедшие в зоны залипания, вычитаются: там
    потеря посчитана в ``suspect_sec``, и второй раз она не считается.
    """
    config = dict(config or DEFAULTS)
    limit = float(config["gap_min_sec"])
    items = [s for s in segments if s.get("start") is not None]
    if not items:
        return 0.0
    speech = _speech_intervals(items, config)
    span_start = min(float(s.get("start") or 0) for s in items)
    span_end = max(float(s.get("end") or 0) for s in items)
    holes: list[tuple[float, float]] = []
    reach = span_start
    for start, end in speech:
        if start - reach > limit:
            holes.append((reach, start))
        reach = max(reach, end)
    if span_end - reach > limit:
        holes.append((reach, span_end))
    cuts = [(float(z["start"]), float(z["end"]))
            for z in (zones or []) if z.get("kind") in GATED_KINDS]
    return round(sum(end - start for start, end in _subtract(holes, cuts)), 3)


def quality_block(segments: Iterable[dict], zones: Iterable[dict],
                  duration_sec: float, config: dict | None = None) -> dict:
    """Блок ``quality`` для raw.json и run-meta.json.

    ``suspect_sec`` считается **после** повторного прохода: зона с
    ``recovered: true`` в него не входит — потеря, которую вернули, потерей уже
    не является.
    """
    config = dict(config or DEFAULTS)
    items = list(segments)
    found = list(zones)
    loops = [z for z in found if z.get("kind") in GATED_KINDS]
    warnings = [z for z in found if z.get("kind") == NO_PUNCTUATION]
    suspect = sum(
        float(z["end"]) - float(z["start"]) for z in loops if not z.get("recovered")
    )
    duration = float(duration_sec or 0)
    gaps = measure_gaps(items, config, zones=found)
    return {
        "loops": [{k: v for k, v in z.items() if k != "segments"} for z in loops],
        "warnings": [{k: v for k, v in z.items() if k != "segments"} for z in warnings],
        "suspect_sec": round(suspect, 3),
        "suspect_ratio": round(suspect / duration, 4) if duration > 0 else 0.0,
        "gaps_sec": gaps,
        "thresholds": {
            "suspect_ratio_max": float(config["suspect_ratio_max"]),
            "gaps_sec_max": float(config["gaps_sec_max"]),
        },
    }


def gate_status(quality: dict, config: dict | None = None) -> str:
    """``degraded``, если доля подозрительного или пропуски выше порога.

    Прогон, потерявший минуты речи, не имеет права закончиться словом ``ok``:
    именно молчаливое ``ok`` и оставило потерю 02.09 незамеченной сутки.
    """
    config = dict(config or DEFAULTS)
    ratio = float((quality or {}).get("suspect_ratio") or 0)
    gaps = float((quality or {}).get("gaps_sec") or 0)
    if ratio > float(config["suspect_ratio_max"]) or gaps > float(config["gaps_sec_max"]):
        return "degraded"
    return "ok"
