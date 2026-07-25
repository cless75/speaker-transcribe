"""Тест хелпера voiceprint_identity (без GPU/аудио).

Проверяет детерминированную механику: анонимный профиль с VTT-алиасом попадает в кандидаты;
после set имя проставляется и голос узнаётся по имени в match.

Запуск:  python scripts/test-voiceprint-identity.py   (ALL PASS -> exit 0)
"""
from __future__ import annotations

import json
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
        rc = vpi.cmd_set(_ns(config=None, store=store_path, registry=None, project=None,
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

    # --- config-резолв стора + emit JSON в каталог проекта ---
    with tempfile.TemporaryDirectory() as td2:
        root = pathlib.Path(td2)
        hub = root / "Hub"
        cache = root / "cache"
        pid = "700"
        (cache / pid).mkdir(parents=True)
        (hub / pid).mkdir(parents=True)
        cfg = {
            "node": {"host_label": "TEST-HOST", "cache_root": str(cache)},
            "hub_root": str(hub),
            "voiceprint_mode": "match_enroll",
            "outputs": {"voiceprints": {
                "local_cache": "{cache_root}/{pid}",
                "project_projection": "{hub_root}/{pid}",
                "candidates_out": "project",
            }},
        }
        cfg_path = root / "node.local.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        st_path = str(cache / pid / "voiceprints.json")
        store = load_voiceprint_store(st_path)
        enroll_voiceprint_profile(
            store=store, speaker_id="S9", speaker_vectors={"S9": V},
            sample_meta={"extractor": EXTRACTOR, "source_file": "rec-cfg.m4a", "speaker_id": "S9"},
            enroll_meta=None)
        save_voiceprint_store_atomic(st_path, store)

        rc = vpi.cmd_list(_ns(config=str(cfg_path), project=pid, all=False,
                              store=None, registry=None, out=None, json=False))
        checks.append(("cmd_list по конфигу вернул 0", rc == 0))
        emitted = hub / pid / "_voiceprint-candidates-TEST-HOST.json"
        checks.append(("JSON кандидатов положен в каталог проекта", emitted.is_file()))
        if emitted.is_file():
            body = json.loads(emitted.read_text(encoding="utf-8"))
            checks.append(("в JSON есть кандидат из стора", body.get("count") == 1
                           and body["candidates"][0]["sessions"] == ["rec-cfg.m4a"]))
            checks.append(("project/host в JSON", body.get("project") == pid
                           and body.get("host") == "TEST-HOST"))

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
