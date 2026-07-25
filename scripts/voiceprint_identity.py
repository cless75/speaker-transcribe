"""voiceprint_identity — детерминированные операции для навыка voiceprint-identity-matching.

Умный мэтчинг (кто есть кто) делает АГЕНТ, проходя по экзокортексу. Этот хелпер — только
механика: (1) выгрузить кандидатов-анонимов для чтения агентом, (2) атомарно проставить
подтверждённое имя/контакт в стор + проектный реестр.

Примеры:
  python scripts/voiceprint_identity.py list-candidates --store C:/work/cache/700/voiceprints.json --json
  python scripts/voiceprint_identity.py set --store C:/work/cache/700/voiceprints.json \
      --registry "G:/Мой диск/Hub/700" --voice-hash vh_b7d6... --name "Дмитрий" --contact "[[Дмитрий]]"
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from media_transcribe import (  # noqa: E402
    _find_person_by_voice_hash,
    load_voiceprint_store,
    save_voiceprint_store_atomic,
    sync_profile_to_project_registry,
)


def _alias_summary(person: dict) -> list[dict]:
    out = []
    for a in person.get("observed_aliases") or []:
        if isinstance(a, dict) and a.get("normalized_name"):
            out.append({
                "name": a.get("normalized_name"),
                "source": a.get("source"),
                "count": int(a.get("count") or 0),
            })
    return sorted(out, key=lambda x: -x["count"])


def _sessions_of(profile: dict) -> list[str]:
    seen = []
    for emb in profile.get("embeddings") or []:
        sm = emb.get("sample_meta") or {}
        src = str(sm.get("source_file") or "").strip()
        if src and src not in seen:
            seen.append(src)
    return seen


def _best_clip(profile: dict) -> str | None:
    return profile.get("best_clip_path") or None


def collect_candidates(store: dict) -> list[dict]:
    """Анонимные профили (canonical_name пуст) — кандидаты на именование."""
    cands = []
    for person in store.get("persons", []):
        if (person.get("canonical_name") or "").strip():
            continue  # уже назван — не кандидат
        aliases = _alias_summary(person)
        suggestion = aliases[0]["name"] if aliases else None
        for profile in person.get("profiles", []):
            vh = profile.get("voice_hash")
            if not vh:
                continue
            cands.append({
                "voice_hash": vh,
                "person_id": person.get("person_id"),
                "canonical_name": None,
                "suggestion": suggestion,
                "observed_aliases": aliases,
                "best_clip": _best_clip(profile),
                "sessions": _sessions_of(profile),
                "has_embedding": bool(profile.get("embeddings")),
            })
    return cands


def cmd_list(args) -> int:
    store = load_voiceprint_store(args.store)
    cands = collect_candidates(store)
    if args.json:
        print(json.dumps({"count": len(cands), "candidates": cands}, ensure_ascii=False, indent=2))
        return 0
    if not cands:
        print("Анонимных профилей нет — все именованы.")
        return 0
    print(f"Кандидаты на именование: {len(cands)}\n")
    for c in cands:
        sug = f" | предложение: {c['suggestion']}" if c["suggestion"] else " | предложения нет"
        al = ", ".join(f"{a['name']}({a['source']}×{a['count']})" for a in c["observed_aliases"]) or "—"
        print(f"  {c['voice_hash']}{sug}")
        print(f"      aliases: {al}")
        print(f"      sessions: {len(c['sessions'])} | best_clip: {c['best_clip'] or '—'}")
    return 0


def cmd_set(args) -> int:
    store = load_voiceprint_store(args.store)
    found = _find_person_by_voice_hash(store, args.voice_hash)
    if found is None:
        print(f"ОШИБКА: voice_hash не найден в сторе: {args.voice_hash}", file=sys.stderr)
        return 2
    person, _ = found
    existing = (person.get("canonical_name") or "").strip()
    if existing and not args.force:
        print(f"ПРОПУСК: профиль уже назван '{existing}' (--force чтобы перезаписать).")
        return 0

    name = args.name.strip()
    contact_ref = (args.contact or f"[[{name}]]").strip()
    person["canonical_name"] = name
    person["display_name"] = args.display or name.split()[0]
    person["contact_name"] = args.contact_name or name
    person["contact_ref"] = contact_ref
    save_voiceprint_store_atomic(args.store, store)

    synced = None
    if args.registry:
        # Собрать карточку из person+profile для sync (identity — на person'е).
        for profile in person.get("profiles", []):
            merged = {
                **profile,
                "canonical_name": person["canonical_name"],
                "display_name": person["display_name"],
                "contact_name": person["contact_name"],
                "contact_ref": person["contact_ref"],
                "observed_aliases": person.get("observed_aliases") or [],
                "voiceprints": person.get("voiceprints") or [profile.get("voice_hash")],
            }
            synced = sync_profile_to_project_registry(
                merged, pathlib.Path(args.registry), project_id=args.project)

    print(f"OK: {args.voice_hash} -> '{name}' (contact {contact_ref})"
          + (f"; реестр обновлён ({synced['index_path']})" if synced else "; реестр не задан"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="voiceprint identity: list-candidates / set")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lc = sub.add_parser("list-candidates", help="выгрузить анонимные профили-кандидаты")
    lc.add_argument("--store", required=True)
    lc.add_argument("--registry", default=None)
    lc.add_argument("--json", action="store_true")
    lc.set_defaults(func=cmd_list)

    st = sub.add_parser("set", help="проставить подтверждённое имя/контакт")
    st.add_argument("--store", required=True)
    st.add_argument("--registry", default=None)
    st.add_argument("--global", dest="global_dir", default=None)
    st.add_argument("--project", default=None)
    st.add_argument("--voice-hash", required=True)
    st.add_argument("--name", required=True)
    st.add_argument("--display", default=None)
    st.add_argument("--contact", default=None, help="wikilink контакта, по умолч. [[<name>]]")
    st.add_argument("--contact-name", dest="contact_name", default=None)
    st.add_argument("--force", action="store_true")
    st.set_defaults(func=cmd_set)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
