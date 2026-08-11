"""Тест приоритета имён спикеров: голос связывает, но не переименовывает молча.

Проверяет правило org/voiceprint-links-identities-not-renames на трёх случаях,
взятых с реальных записей проекта 506:

  1. слабое совпадение (0.40, band=low) НЕ подменяет имя из экспорта встречи —
     на W1 пятеро из семи имели matched=false с чужими целевыми именами;
  2. уверенное совпадение (0.66, band=high) подменяет — на W2 так сошлись два
     подключения одного человека («Dmitry» и «Дмитрий Безуглый»);
  3. имя из экспорта всегда сохраняется в export_speaker_name, а расхождение
     видно в speaker_review как name_conflict.

Плюс инварианты: manual_map неприкосновенна; voice_hash проставляется всегда;
спикер без экспорта ведёт себя как раньше.

Запуск:  python scripts/test-speaker-name-priority.py   (ALL PASS -> exit 0)
"""
from __future__ import annotations

import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from media_transcribe import (  # noqa: E402
    apply_voiceprint_identity,
    build_speaker_review,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}{(' — ' + detail) if detail else ''}")


def segment(speaker_id: str, name: str | None, source: str, text: str = "реплика") -> dict:
    return {
        "speaker_id": speaker_id,
        "speaker_name": name,
        "speaker": name,
        "speaker_source": source,
        "text": text,
        "start": 0.0,
        "end": 1.0,
    }


def match(name: str, score: float, band: str, voice_hash: str) -> dict:
    return {
        "matched": True,
        "voice_hash": voice_hash,
        "speaker_profile_id": voice_hash,
        "canonical_name": name,
        "contact_name": name,
        "score": score,
        "confidence_band": band,
    }


def test_weak_match_keeps_export_name() -> None:
    print("1. Слабое совпадение не переименовывает участника")
    segments = [segment("Speaker 2", "Konstantin Mashukov", "zoom_vtt")]
    apply_voiceprint_identity(
        segments,
        {"Speaker 2": match("Дмитрий Безуглый", 0.401, "low", "vh_dmitry")},
    )
    seg = segments[0]
    check("имя из экспорта уцелело", seg["speaker_name"] == "Konstantin Mashukov", seg["speaker_name"])
    check("источник остался zoom_vtt", seg["speaker_source"] == "zoom_vtt", seg["speaker_source"])
    check("voice_hash всё равно проставлен", seg.get("voice_hash") == "vh_dmitry")
    check("исходное имя сохранено", seg.get("export_speaker_name") == "Konstantin Mashukov")


def test_high_match_links_two_connections() -> None:
    print("2. Уверенное совпадение сводит два подключения одного человека")
    segments = [segment("Speaker 6", "Dmitry", "zoom_vtt")]
    apply_voiceprint_identity(
        segments,
        {"Speaker 6": match("Дмитрий Безуглый", 0.664, "high", "vh_dmitry")},
    )
    seg = segments[0]
    check("имя подменено на каноническое", seg["speaker_name"] == "Дмитрий Безуглый", seg["speaker_name"])
    check("источник стал voiceprint_contact", seg["speaker_source"] == "voiceprint_contact")
    check("имя из экспорта не потеряно", seg.get("export_speaker_name") == "Dmitry")


def test_manual_map_untouched() -> None:
    print("3. Ручная разметка неприкосновенна")
    segments = [segment("Speaker 1", "Иван Петров", "manual_map")]
    apply_voiceprint_identity(
        segments,
        {"Speaker 1": match("Кто-то Другой", 0.99, "high", "vh_other")},
    )
    seg = segments[0]
    check("имя не тронуто", seg["speaker_name"] == "Иван Петров")
    check("voice_hash не проставлен", seg.get("voice_hash") is None)


def test_no_export_behaves_as_before() -> None:
    print("4. Без экспорта поведение прежнее")
    segments = [segment("Speaker 3", "SPEAKER_03", "diarization")]
    apply_voiceprint_identity(
        segments,
        {"Speaker 3": match("Denis Simonov", 0.58, "medium", "vh_denis")},
    )
    seg = segments[0]
    check("имя подставлено из профиля", seg["speaker_name"] == "Denis Simonov", seg["speaker_name"])
    check("источник voiceprint_contact", seg["speaker_source"] == "voiceprint_contact")
    check("export_speaker_name отсутствует", "export_speaker_name" not in seg)


def test_review_shows_conflict() -> None:
    print("5. Расхождение видно в ревью спикеров")
    segments = [segment("Speaker 2", "Konstantin Mashukov", "zoom_vtt")]
    matches = {"Speaker 2": match("Дмитрий Безуглый", 0.401, "low", "vh_dmitry")}
    apply_voiceprint_identity(segments, matches)
    review = build_speaker_review({"segments": segments, "voiceprint": {"matches": matches}})
    row = review["speakers"][0]
    check("конфликт помечен", row["name_conflict"] is True)
    check("оба имени доступны",
          row["export_name"] == "Konstantin Mashukov"
          and row["voiceprint_match"]["canonical_name"] == "Дмитрий Безуглый")
    check("скор доступен для решения", row["voiceprint_match"]["score"] == 0.401)

    print("   и не помечается там, где расхождения нет")
    same = [segment("Speaker 6", "Dmitry", "zoom_vtt")]
    same_matches = {"Speaker 6": match("Dmitry", 0.9, "high", "vh_dmitry")}
    apply_voiceprint_identity(same, same_matches)
    review2 = build_speaker_review({"segments": same, "voiceprint": {"matches": same_matches}})
    check("конфликта нет", review2["speakers"][0]["name_conflict"] is False)


def main() -> int:
    print("Тест: приоритет имён спикеров (801-o8)\n")
    test_weak_match_keeps_export_name()
    test_high_match_links_two_connections()
    test_manual_map_untouched()
    test_no_export_behaves_as_before()
    test_review_shows_conflict()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
