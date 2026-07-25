"""Тест хелпера voiceprint_identity (без GPU/аудио).

Проверяет детерминированную механику: анонимный профиль с VTT-алиасом попадает в кандидаты;
после set имя проставляется и голос узнаётся по имени в match.

Запуск:  python scripts/test-voiceprint-identity.py   (ALL PASS -> exit 0)
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
SCRIPTS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SCRIPTS))

from media_transcribe import (  # noqa: E402
    VOICE_EMBEDDING_DEFAULT_EXTRACTOR,
    enroll_voiceprint_profile,
    load_voiceprint_store,
    match_voiceprint_profiles,
    save_voiceprint_store_atomic,
)
import importlib  # noqa: E402

vpi = importlib.import_module("voiceprint_identity")

EXTRACTOR = VOICE_EMBEDDING_DEFAULT_EXTRACTOR
V = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def main() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as td:
        store_path = str(pathlib.Path(td) / "voiceprints.json")

        # Анонимный энролл + наблюдённый VTT-алиас на person'е.
        store = load_voiceprint_store(store_path)
        enrolled = enroll_voiceprint_profile(
            store=store, speaker_id="S1", speaker_vectors={"S1": V},
            sample_meta={"extractor": EXTRACTOR, "source_file": "rec-700.m4a", "speaker_id": "S1"},
            enroll_meta=None,
        )
        vh = enrolled["voice_hash"]
        person = None
        for p in store["persons"]:
            if vh in (p.get("voiceprints") or []):
                person = p
        person["observed_aliases"] = [{"source": "zoom_vtt", "normalized_name": "Дмитрий",
                                       "raw_name": "Дмитрий", "count": 2}]
        save_voiceprint_store_atomic(store_path, store)

        # list-candidates
        store = load_voiceprint_store(store_path)
        cands = vpi.collect_candidates(store)
        c = next((x for x in cands if x["voice_hash"] == vh), None)
        checks.append(("кандидат найден", c is not None))
        checks.append(("предложение из VTT == 'Дмитрий'", c and c["suggestion"] == "Дмитрий"))
        checks.append(("сессия rec-700 в кандидате", c and "rec-700.m4a" in c["sessions"]))

        # set имя
        rc = vpi.cmd_set(_ns(store=store_path, registry=None, global_dir=None, project=None,
                             voice_hash=vh, name="Дмитрий", display=None, contact=None,
                             contact_name=None, force=False))
        checks.append(("set вернул 0", rc == 0))

        # перезагрузка: имя проставлено, из кандидатов ушёл
        store = load_voiceprint_store(store_path)
        named = next((p for p in store["persons"] if vh in (p.get("voiceprints") or [])), None)
        checks.append(("canonical_name == 'Дмитрий'", named and named.get("canonical_name") == "Дмитрий"))
        checks.append(("contact_ref == '[[Дмитрий]]'", named and named.get("contact_ref") == "[[Дмитрий]]"))
        checks.append(("больше не кандидат", vpi.collect_candidates(store) == []
                       or all(x["voice_hash"] != vh for x in vpi.collect_candidates(store))))

        # match теперь возвращает имя
        m = match_voiceprint_profiles(store, {"X": V}, 0.55, extractor=EXTRACTOR).get("X", {})
        checks.append(("голос узнаётся с именем", m.get("matched") is True
                       and m.get("canonical_name") == "Дмитрий"))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


class _ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


if __name__ == "__main__":
    sys.exit(main())
