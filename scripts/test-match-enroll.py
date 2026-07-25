"""Тест композиции voiceprint match_enroll (без GPU/аудио).

Проверяет примитивы, на которых стоит режим match_enroll:
  1. match_voiceprint_profiles различает известный голос (matched, с именем) и новый (not matched);
  2. enroll_voiceprint_profile заводит профиль новому голосу;
  3. после энролла тот же голос уже узнаётся (matched на себя).

Запуск:  python scripts/test-match-enroll.py   (ALL PASS -> exit 0)
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

from media_transcribe import (  # noqa: E402
    VOICE_EMBEDDING_DEFAULT_EXTRACTOR,
    enroll_voiceprint_profile,
    load_voiceprint_store,
    match_voiceprint_profiles,
    save_voiceprint_store_atomic,
)

EXTRACTOR = VOICE_EMBEDDING_DEFAULT_EXTRACTOR
THRESHOLD = 0.55
V_KNOWN = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
V_NOVEL = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # ортогонален -> cosine 0


def _enroll(store, sid, vec, name=None):
    meta = None
    if name:
        meta = {"canonical_name": name, "display_name": name, "contact_name": name, "contact_ref": f"[[{name}]]"}
    return enroll_voiceprint_profile(
        store=store, speaker_id=sid, speaker_vectors={sid: vec},
        sample_meta={"extractor": EXTRACTOR, "source_file": "test", "speaker_id": sid, "mode": "test"},
        enroll_meta=meta,
    )


def main() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as td:
        path = str(pathlib.Path(td) / "voiceprints.json")

        # Шаг 1: завести известный именованный голос.
        store = load_voiceprint_store(path)
        known = _enroll(store, "KNOWN", V_KNOWN, name="Дмитрий")
        save_voiceprint_store_atomic(path, store)

        # Шаг 2: match знакомого + нового.
        store = load_voiceprint_store(path)
        matches = match_voiceprint_profiles(
            store, {"S1": V_KNOWN, "S2": V_NOVEL}, THRESHOLD, extractor=EXTRACTOR)
        s1, s2 = matches.get("S1", {}), matches.get("S2", {})
        checks.append(("S1 (знакомый голос) matched", s1.get("matched") is True))
        checks.append(("S1 несёт canonical_name 'Дмитрий'", s1.get("canonical_name") == "Дмитрий"))
        checks.append(("S1 voice_hash == known", s1.get("voice_hash") == known["voice_hash"]))
        checks.append(("S2 (новый голос) NOT matched", s2.get("matched") is False))

        # Шаг 3: match_enroll завёл бы S2 — эмулируем enroll несовпавшего.
        new = _enroll(store, "S2", V_NOVEL)  # аноним
        save_voiceprint_store_atomic(path, store)
        checks.append(("S2 получил новый профиль (hash != known)",
                       bool(new.get("voice_hash")) and new["voice_hash"] != known["voice_hash"]))

        # Шаг 4: теперь S2 узнаётся сам на себя.
        store = load_voiceprint_store(path)
        rematch = match_voiceprint_profiles(store, {"X": V_NOVEL}, THRESHOLD, extractor=EXTRACTOR)
        x = rematch.get("X", {})
        checks.append(("после enroll новый голос узнаётся", x.get("matched") is True))
        checks.append(("узнан именно как заведённый профиль", x.get("voice_hash") == new["voice_hash"]))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
