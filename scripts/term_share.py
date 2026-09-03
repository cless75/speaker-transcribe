"""Доля канонических форм терминов в расшифровке — замер 2 из 801-o11.

Зачем скрипт. Замер «доля канона» 28.08 и сопоставление 02.09 считались руками, и
метод жил только прозой требования: «непересекающиеся шаблоны с дедупликацией по
позициям — канон, `variants`, `multiword_variants` и `protect` накладываются по
убыванию длины, занятая позиция повторно не считается; формы из `protect` в
знаменатель не входят». Приёмка 801-o15 требует сравнить долю канона до и после
пакета — то есть посчитать её ещё раз, тем же способом. Способ, который живёт
прозой, второй раз даёт другое число.

Скрипт **ничего не меняет**: он читает расшифровку и печатает числа. Разбор форм
берётся из `glossary_correct` — тот же токенизатор, тот же разбор русских
окончаний, те же таблицы approved-терминов, что и у замены. Иначе счёт разошёлся
бы с механизмом, о котором отчитывается.

    python scripts/term_share.py --glossary PATH файл [файл ...]
    python scripts/term_share.py --glossary PATH --exclude-loops файл
    python scripts/term_share.py --glossary PATH --json файл

Принимает `*-segments.jsonl` (берётся поле `text`), `.md`, `.txt` и `*-raw.json`.

`--exclude-loops` выбрасывает сегменты внутри зон залипания (`src/loop_guard.py`)
и печатает, сколько выброшено. Без этого замер врёт в обе стороны сразу: зона
«Воркспейс воркспейс» ×8 добавляет к канону 16 вхождений термина, которых в речи
не было, — а после починки они исчезают, и доля канона «падает» ровно на том, что
потеря устранена. Сравнивать до и после нужно на одной и той же речи.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import glossary_correct as gc  # noqa: E402
import loop_guard  # noqa: E402


# Кто выигрывает позицию при равной длине совпадения. Порядок повторяет порядок
# силы в самой замене (`GlossaryCorrector.correct`): защищённая последовательность
# сильнее многословного варианта, многословное сильнее одиночного.
CLASS_ORDER = ["protect_mw", "canon_mw", "variant_mw", "protect", "canon", "variant"]
COUNTED = {"canon_mw": "canon", "canon": "canon", "variant_mw": "variant", "variant": "variant"}


def read_segments(path: pathlib.Path) -> list[dict] | None:
    """Сегменты расшифровки, если файл их несёт; иначе None."""
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        segments = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            try:
                segments.append(json.loads(line))
            except ValueError:
                continue
        return segments
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        segments = data.get("segments") or []
        if not segments and data.get("segments_ref"):
            ref = path.parent / str((data.get("segments_ref") or {}).get("file") or "")
            if ref.is_file():
                return read_segments(ref)
        return segments
    return None


def run_prompt(path: pathlib.Path, glossary: dict | None) -> str | None:
    """Подсказка этого прогона: из run-meta рядом, иначе собранная из глоссария.

    Прогоны до 801-o15 текст подсказки не писали — для них она восстанавливается
    из глоссария, и это верно ровно до тех пор, пока глоссарий тот же. Новые
    прогоны пишут `glossary.initial_prompt` в run-meta, и гадать больше не нужно.
    """
    for meta_path in sorted(path.parent.glob("*run-meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        prompt = (meta.get("glossary") or {}).get("initial_prompt")
        if prompt:
            return str(prompt)
    return gc.build_initial_prompt(glossary) if glossary else None


def read_text(path: pathlib.Path, glossary: dict | None = None,
              exclude_loops: bool = False) -> tuple[str, int]:
    """Текст расшифровки и число сегментов, выброшенных как залипание."""
    segments = read_segments(path)
    if segments is None:
        return path.read_text(encoding="utf-8-sig"), 0
    dropped = 0
    if exclude_loops and segments:
        config = loop_guard.resolve_config(None)
        zones = loop_guard.detect_zones(segments, run_prompt(path, glossary), config)
        skip: set[int] = set()
        for zone in zones:
            if zone["kind"] in loop_guard.GATED_KINDS:
                first, last = zone["segments"]
                skip.update(range(first, last + 1))
        dropped = len(skip)
        segments = [s for i, s in enumerate(segments) if i not in skip]
    joined = chr(10).join(str(s.get("text") or "") for s in segments)
    return joined, dropped


def build_patterns(corrector: gc.GlossaryCorrector) -> list[tuple[tuple[str, ...], dict, str]]:
    """Все формы approved-терминов одним списком, длинные впереди.

    Канон входит наравне с вариантами: доля канона — отношение между ними, и
    считать их разными проходами значит разрешить одной позиции попасть в оба.
    """
    patterns: list[tuple[tuple[str, ...], dict, str]] = []
    for term in gc.approved_terms(corrector.glossary):
        canonical = str(term.get("canonical") or "").strip()
        if canonical:
            tokens = tuple(t.lower() for t in gc._WORD_RE.findall(canonical))
            if tokens:
                patterns.append((tokens, term, "canon_mw" if len(tokens) > 1 else "canon"))
        for form in term.get("protect") or []:
            tokens = tuple(t.lower() for t in gc._WORD_RE.findall(str(form)))
            if tokens:
                patterns.append((tokens, term, "protect_mw" if len(tokens) > 1 else "protect"))
        for variant in (term.get("variants") or []) + (term.get("multiword_variants") or []):
            tokens = tuple(t.lower() for t in gc._WORD_RE.findall(str(variant)))
            if tokens:
                patterns.append((tokens, term, "variant_mw" if len(tokens) > 1 else "variant"))
    patterns.sort(key=lambda item: (-len(item[0]), CLASS_ORDER.index(item[2])))
    return patterns


def count_forms(text: str, corrector: gc.GlossaryCorrector) -> dict[str, dict[str, int]]:
    """Счёт по терминам: сколько канона, сколько вариантов, сколько защищённых.

    Проход один, слева направо: совпавшая позиция закрывается и повторно не
    рассматривается. Без этого «экзокортекс» внутри «за кортексом» посчитался бы
    дважды и сразу в обе стороны.
    """
    patterns = build_patterns(corrector)
    tokens = [(m.group(0), m.start(), m.end()) for m in gc._WORD_RE.finditer(text)]
    counts: dict[str, dict[str, int]] = {}
    index = 0
    while index < len(tokens):
        hit = None
        for form, term, kind in patterns:
            if len(form) > 1:
                if corrector._match_sequence(tokens, index, form) is not None:
                    hit = (term, kind, len(form))
                    break
            else:
                stems = [stem for stem, _ in gc._split_ending(tokens[index][0].lower())]
                if form[0] in stems:
                    hit = (term, kind, 1)
                    break
        if hit is None:
            index += 1
            continue
        term, kind, consumed = hit
        name = str(term.get("canonical"))
        bucket = counts.setdefault(name, {"canon": 0, "variant": 0, "protect": 0})
        bucket[COUNTED.get(kind, "protect")] += 1
        index += consumed
    return counts


def report(path: pathlib.Path, counts: dict[str, dict[str, int]], dropped: int = 0) -> dict:
    canon = sum(item["canon"] for item in counts.values())
    variant = sum(item["variant"] for item in counts.values())
    protected = sum(item["protect"] for item in counts.values())
    total = canon + variant
    return {
        "file": str(path),
        "segments_dropped_as_loops": dropped,
        "canon": canon,
        "variant": variant,
        "protected_excluded": protected,
        "share": round(canon / total, 4) if total else None,
        "terms": {name: item for name, item in sorted(counts.items())
                  if item["canon"] or item["variant"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Доля канонических форм терминов в расшифровке")
    parser.add_argument("--glossary", required=True, help="путь к _PROJECT-glossary.json")
    parser.add_argument("--exclude-loops", action="store_true",
                        help="выбросить сегменты внутри зон залипания (801-o15)")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    glossary = gc.read_glossary_file(args.glossary)
    if glossary is None:
        print(f"ОШИБКА: глоссарий не прочитан: {args.glossary}", file=sys.stderr)
        return 2
    corrector = gc.GlossaryCorrector(glossary)

    reports = []
    for raw in args.paths:
        path = pathlib.Path(raw)
        try:
            text, dropped = read_text(path, glossary, args.exclude_loops)
        except OSError as exc:
            print(f"FAIL {path}: {exc}", file=sys.stderr)
            return 1
        reports.append(report(path, count_forms(text, corrector), dropped))

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for item in reports:
        share = "—" if item["share"] is None else f"{item['share'] * 100:.1f}%"
        loops = (f", выброшено сегментов как залипание: {item['segments_dropped_as_loops']}"
                 if item["segments_dropped_as_loops"] else "")
        print(f"{pathlib.Path(item['file']).name}: канон {item['canon']}, "
              f"вариантов {item['variant']}, доля канона {share} "
              f"(защищённых вне знаменателя: {item['protected_excluded']}){loops}")
        for name, term in item["terms"].items():
            print(f"    {name}: {term['canon']} / {term['canon'] + term['variant']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
