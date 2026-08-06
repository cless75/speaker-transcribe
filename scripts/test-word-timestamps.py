"""Пословные границы: правило шва, обратная совместимость, отпечаток resume.

Регрессии, которые держит этот тест:

1. Слово на шве чанков. Перекрытие — последние ``overlap`` секунд чанка k и первые
   ``overlap`` секунд чанка k+1. Слово, пересекающее шов, у чанка k обрезано концом
   его аудио, а у чанка k+1 звучит целиком. Если наивно унаследовать сегментное
   правило (подрезать начало по ``overlap``), у слова получится ложное начало — и
   резка по слову въедет в речь. Владелец такого слова — k+1, начало НЕ подрезается,
   у k слово выбрасывается.
2. Выключённый признак обязан оставлять выход прежним: ключа ``words`` в сегменте
   быть не должно вовсе — ни пустым списком, ни null.
3. Отпечаток частичного resume должен различать прогоны с границами и без. Без
   этого возобновлённый прогон смешает чанки, и половина сегментов молча останется
   без слов.

Пуре-логика: движок и файловая система не задействованы, вместо сегментов и слов —
подделки с теми же атрибутами.

Запуск:  python scripts/test-word-timestamps.py
"""
from __future__ import annotations

import pathlib
import sys

try:  # консоль узлов бывает cp1251 — не роняем вывод на не-ASCII
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import media_transcribe  # noqa: E402
from media_transcribe import (  # noqa: E402
    build_word_timestamps_meta,
    collect_chunk_words,
    fingerprint_matches,
    word_timestamps_decision,
)

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


class FakeWord:
    def __init__(self, word: str, start: float, end: float, probability: float | None = 0.9):
        self.word = word
        self.start = start
        self.end = end
        self.probability = probability


class FakeSegment:
    def __init__(self, words):
        self.words = words


CHUNK_SIZE = 1200.0   # 20 минут
OVERLAP = 30.0
STEP = CHUNK_SIZE - OVERLAP


def job(index: int, *, words: bool = True, last: bool = False, duration=CHUNK_SIZE) -> dict:
    return {
        "chunk_index": index,
        "chunk_start": index * STEP,
        "chunk_overlap_sec": OVERLAP,
        "chunk_duration": duration,
        "is_last_chunk": last,
        "word_timestamps": words,
    }


print("== правило шва ==")

# Чанк 1 (второй по счёту). Локальные времена:
#   5.0–5.4   — целиком в перекрытии, принадлежит чанку 0
#   29.6–30.4 — пересекает шов: у чанка 0 обрезано, здесь звучит целиком
#   40.0–40.5 — обычное слово после шва
seam = FakeSegment([
    FakeWord("раньше", 5.0, 5.4),
    FakeWord("через", 29.6, 30.4),
    FakeWord("после", 40.0, 40.5),
])
got = collect_chunk_words(seam, job(1))
check("слово из зоны перекрытия выброшено", [w["word"] for w in got], ["через", "после"])
check(
    "слово на шве сохранило ИСТИННОЕ начало, а не обрезанное по overlap",
    got[0]["start"],
    round(29.6 + STEP, 3),
)
check("слово на шве сохранило конец", got[0]["end"], round(30.4 + STEP, 3))
check("обычное слово смещено на chunk_start", got[1]["start"], round(40.0 + STEP, 3))

# Тот же шов со стороны чанка 0: слово упирается в конец его аудио (1200.0)
# и должно быть выброшено — иначе задвоится с версией из чанка 1.
edge = FakeSegment([
    FakeWord("целое", 1100.0, 1100.6),
    FakeWord("через", 1199.6, 1200.0),
])
got0 = collect_chunk_words(edge, job(0))
check("слово, обрезанное краем своего чанка, выброшено", [w["word"] for w in got0], ["целое"])

# Последний чанк обрезать нечем — там слово у края остаётся.
got_last = collect_chunk_words(edge, job(0, last=True))
check("в последнем чанке слово у края остаётся", [w["word"] for w in got_last], ["целое", "через"])

# Итог по шву: слово "через" встречается ровно один раз на оба чанка.
total = [w["word"] for w in got0] + [w["word"] for w in got]
check("на шве слово ровно одно, не задвоено и не потеряно", total.count("через"), 1)

print("== содержимое слова ==")
check("текст обрезан от ведущего пробела", collect_chunk_words(
    FakeSegment([FakeWord(" привет", 1.0, 1.4)]), job(0))[0]["word"], "привет")
check("уверенность округлена до двух знаков", collect_chunk_words(
    FakeSegment([FakeWord("да", 1.0, 1.2, probability=0.98765)]), job(0))[0]["probability"], 0.99)
check("без уверенности поле не выдумывается", "probability" in collect_chunk_words(
    FakeSegment([FakeWord("да", 1.0, 1.2, probability=None)]), job(0))[0], False)
check("пустое слово пропущено", collect_chunk_words(
    FakeSegment([FakeWord("   ", 1.0, 1.2)]), job(0)), [])

print("== выключённый признак ==")
check("границы не запрашивались — слов нет", collect_chunk_words(seam, job(1, words=False)), [])
check("движок не отдал слова — пустой список", collect_chunk_words(FakeSegment(None), job(0)), [])

print("== порядок разрешения признака ==")
media_transcribe.load_project_settings = lambda payload: payload.get("_fake_project") or {}
check("умолчание", word_timestamps_decision({}), (False, "default"))
check("конфиг узла", word_timestamps_decision({"word_timestamps": True}), (True, "node"))
check(
    "проект сильнее узла",
    word_timestamps_decision({"word_timestamps": True, "_fake_project": {"word_timestamps": False}}),
    (False, "project"),
)
check(
    "запуск сильнее проекта",
    word_timestamps_decision({
        "word_timestamps_override": True,
        "_fake_project": {"word_timestamps": False},
    }),
    (True, "run"),
)

print("== отпечаток resume ==")
base = {
    "source_file_resolved": "a", "processing_input_resolved": "b", "source_size_bytes": 10,
    "duration_sec": 100.0, "chunk_minutes": 20, "chunk_overlap_sec": 30,
    "selected_model": "medium", "quality_preset": "medium",
}
check("одинаковые отпечатки совпадают",
      fingerprint_matches({**base, "word_timestamps": True}, {**base, "word_timestamps": True}), True)
check("разный признак — отпечатки различаются",
      fingerprint_matches({**base, "word_timestamps": False}, {**base, "word_timestamps": True}), False)
check("старый отпечаток без ключа == выключено (resume не рвётся)",
      fingerprint_matches(dict(base), {**base, "word_timestamps": False}), True)
check("старый отпечаток против включённого признака — не совпадает",
      fingerprint_matches(dict(base), {**base, "word_timestamps": True}), False)

print("== метаданные прогона ==")
segs = [{"words": [1, 2, 3]}, {"words": [4]}, {}]
meta_on = build_word_timestamps_meta({"word_timestamps_override": True, "_asr_elapsed_sec": 12.5}, segs)
check("считает слова", meta_on["words_total"], 4)
check("считает сегменты со словами", meta_on["segments_with_words"], 2)
check("фиксирует время распознавания", meta_on["asr_sec"], 12.5)
check("фиксирует, кто решил", meta_on["decided_by"], "run")
meta_off = build_word_timestamps_meta({}, segs)
check("при выключенном не выдумывает счётчики", "words_total" in meta_off, False)
check("при выключенном время всё равно фиксируется", "asr_sec" in meta_off, True)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("все проверки пройдены")
