"""Тест источника кадров и фильтра содержательности (801-o7).

Кадры снимаются с видео, где видна демонстрация экрана, а не с той дорожки, что
идёт в ASR. Проверяется на раскладке реального Zoom-бандла проекта 506:

  GMT20260807-140209_Recording_2560x1080.mp4   <- демонстрация экрана
  GMT20260807-140209_Recording_avo_640x360.mp4 <- активный говорящий, идёт в ASR

Плюс фильтр: интервальная съёмка даёт покрытие ценой шума (говорящая голова без
слайда, один слайд десятью кадрами подряд) — такие кадры не попадают в транскрипт,
но остаются на диске с причиной.

Запуск:  python scripts/test-frames-source.py   (ALL PASS -> exit 0)
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from audio_inbox_watch import frames_video_for  # noqa: E402
from slide_frames import (  # noqa: E402
    _fingerprint_similarity,
    apply_content_filter,
    frames_source_path,
    resolve_config,
)

FAILURES: list[str] = []

BUNDLE = [
    "GMT20260807-140209_Recording_2560x1080.mp4",
    "GMT20260807-140209_Recording_avo_640x360.mp4",
    "GMT20260807-140209_Recording.transcript.vtt",
]
SOURCE_RULE = {
    "prefer_patterns": ["_as_", r"_\d{3,}x\d{3,}"],
    "exclude_patterns": ["_avo_"],
    "fallback": "largest",
}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")


def make_bundle(root: pathlib.Path, names=BUNDLE) -> None:
    for name in names:
        path = root / name
        # размер важен только для fallback: largest
        path.write_bytes(b"x" * (2000 if "2560x1080" in name else 100))


def test_picks_screen_video() -> None:
    print("1. Кадры берутся с видео демонстрации, ASR — с лёгкой дорожки")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        make_bundle(root)
        asr_input = root / "GMT20260807-140209_Recording_avo_640x360.mp4"
        chosen = frames_video_for(asr_input, {"video_frames": {"source": SOURCE_RULE}})
        check("выбрано экранное видео", chosen is not None and chosen.name.endswith("2560x1080.mp4"),
              str(chosen))


def test_no_rule_keeps_old_behaviour() -> None:
    print("2. Без правила в конфиге поведение прежнее")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        make_bundle(root)
        asr_input = root / "GMT20260807-140209_Recording_avo_640x360.mp4"
        check("без video_frames — None", frames_video_for(asr_input, {}) is None)
        check("с пустым правилом — None",
              frames_video_for(asr_input, {"video_frames": {"source": {}}}) is None)


def test_excluded_and_single_file() -> None:
    print("3. Исключённое видео и одиночный файл")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        make_bundle(root, ["GMT20260807-140209_Recording_avo_640x360.mp4"])
        asr_input = root / "GMT20260807-140209_Recording_avo_640x360.mp4"
        chosen = frames_video_for(asr_input, {"video_frames": {"source": SOURCE_RULE}})
        check("единственный кандидат исключён -> None", chosen is None, str(chosen))


def test_payload_path_selection() -> None:
    print("4. Стадия снимает кадры с frames_input_path")
    payload = {"input_path": r"C:\x\a_avo_640x360.mp4", "frames_input_path": r"C:\x\a_2560x1080.mp4"}
    check("путь кадров переопределён", frames_source_path(payload).endswith("a_2560x1080.mp4"))
    check("без переопределения — вход ASR",
          frames_source_path({"input_path": r"C:\x\a.mp4"}).endswith("a.mp4"))

    print("   и не считает видео по расширению файла ASR")
    cfg = resolve_config({
        "input_path": r"C:\x\a.m4a",           # аудио
        "frames_input_path": r"C:\x\a.mp4",    # видео
        "video_frames": {"mode": "interval"},
    })
    check("аудио-вход + видео-источник = стадия работает",
          cfg is not None and not cfg.get("_not_video"), str(cfg))


def test_content_filter() -> None:
    print("5. Фильтр: кадры без текста и дубли не идут в транскрипт")
    slides = [
        {"time_hms": "00:00:00", "ocr_text": "Дмитрий Безуглый 2026-08-07 17:02:09"},
        {"time_hms": "00:01:00", "ocr_text": "Strategy OKRs Discovery Real Progress reduce uncertainty with evidence"},
        {"time_hms": "00:02:00", "ocr_text": "Strategy OKRs Discovery Real Progress reduce uncertainty with evidence"},
        {"time_hms": "00:03:00", "ocr_text": "Signals and trends: weak signal, driver, wildcard, mapping the horizon"},
    ]
    stats = apply_content_filter(slides, min_ocr_chars=40, dedupe_similarity=0.9)
    check("плашка с именем отброшена", slides[0]["embed"] is False and slides[0]["filter_reason"] == "no_text")
    check("первый слайд оставлен", slides[1]["embed"] is True)
    check("его дубль отброшен", slides[2]["embed"] is False and slides[2]["filter_reason"] == "duplicate")
    check("следующий слайд оставлен", slides[3]["embed"] is True)
    check("статистика сходится", stats == {"embedded": 2, "no_text": 1, "duplicate": 1}, str(stats))
    check("отбракованные остаются в списке", len(slides) == 4)

    print("   выключенный фильтр не трогает ничего")
    plain = [{"ocr_text": ""}, {"ocr_text": ""}]
    stats2 = apply_content_filter(plain, min_ocr_chars=0, dedupe_similarity=0)
    check("все кадры встраиваются", all(s["embed"] for s in plain) and stats2["embedded"] == 2)


def test_visual_dedupe() -> None:
    print("6. Визуальный дедуп ловит то, что прячет нестабильный OCR")
    # Один и тот же слайд, прочитанный OCR по-разному — текстовый дедуп его пропустит.
    slides = [
        {"ocr_text": "To make sure your strategy clearly says what is in focus and what not",
         "fingerprint": "ffff0000ffff0000"},
        {"ocr_text": "To make sure your strar clearly says what s in focus and what not OKRsare",
         "fingerprint": "ffff0000ffff0001"},
        {"ocr_text": "Signals and trends: weak signal, driver, wildcard, mapping the horizon",
         "fingerprint": "0000ffff0000ffff"},
    ]
    stats = apply_content_filter(slides, min_ocr_chars=40, dedupe_similarity=0.95, dedupe_visual=0.95)
    check("тот же слайд отброшен по картинке",
          slides[1]["embed"] is False and slides[1].get("duplicate_of") == "picture")
    check("новый слайд оставлен", slides[2]["embed"] is True)
    check("в транскрипт ушли два кадра", stats["embedded"] == 2, str(stats))

    print("   мера сходства отпечатков")
    check("идентичные = 1.0", _fingerprint_similarity("ffff0000ffff0000", "ffff0000ffff0000") == 1.0)
    check("один бит разницы ≈ 0.984",
          abs(_fingerprint_similarity("ffff0000ffff0000", "ffff0000ffff0001") - 63 / 64) < 1e-9)
    check("нет отпечатка — не дубль", _fingerprint_similarity(None, "ffff0000ffff0000") == 0.0)


def main() -> int:
    print("Тест: источник кадров и фильтр (801-o7)\n")
    test_picks_screen_video()
    test_no_rule_keeps_old_behaviour()
    test_excluded_and_single_file()
    test_payload_path_selection()
    test_content_filter()
    test_visual_dedupe()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
