"""voiceprint_identity — детерминированные операции для навыка voiceprint-identity-matching.

Умный мэтчинг (кто есть кто) делает АГЕНТ, проходя по экзокортексу (People/, сессии, VTT, best_clip).
Этот хелпер — только механика: (1) выгрузить кандидатов-анонимов (в stdout и/или файл в Hub по
конфигу), (2) атомарно проставить подтверждённое имя/контакт в стор + проектный реестр.

Пути стора/реестра берутся из node-конфига (`--config`) по проекту (`--project`/`--all`) — те же
плейсхолдеры, что у watcher (`{hub_root}`/`{cache_root}`/`{pid}`). Или явно через `--store`/`--registry`.

Примеры:
  # по конфигу: стор резолвится, JSON кандидатов кладётся по outputs.voiceprints.candidates_out
  python scripts/voiceprint_identity.py list-candidates --config config/node.local.json --project 700
  python scripts/voiceprint_identity.py list-candidates --config config/node.local.json --all
  # явный стор в stdout
  python scripts/voiceprint_identity.py list-candidates --store C:/work/cache/700/voiceprints.json --json
  # проставить имя (стор/реестр из конфига)
  python scripts/voiceprint_identity.py set --config config/node.local.json --project 700 \
      --voice-hash vh_b7d6... --name "Дмитрий" --contact "[[Дмитрий]]"
"""
from __future__ import annotations

import argparse
import datetime as dt
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
# Резолв плейсхолдеров конфига — тот же, что у watcher (без дублирования семантики).
from audio_inbox_watch import (  # noqa: E402
    base_placeholder_ctx,
    host_label_of,
    load_config,
    resolve_template,
)

DEFAULT_CANDIDATES_OUT = "project"  # project | meta | root | <path-template>


# ------------------------------------------------------------------ resolution
def resolve_project_paths(cfg: dict, pid: str) -> dict:
    """store / registry / global / host для проекта из node-конфига (как в watcher, 1820)."""
    vp = (cfg.get("outputs") or {}).get("voiceprints") or {}
    ctx = base_placeholder_ctx(cfg)
    ctx["pid"] = str(pid)
    store_tpl = vp.get("local_cache")
    reg_tpl = vp.get("project_projection")
    gr_tpl = vp.get("global_registry")
    store = None
    if store_tpl:
        store = str(pathlib.Path(resolve_template(store_tpl, ctx)) / "voiceprints.json")
    return {
        "store": store,
        "registry": resolve_template(reg_tpl, ctx) if reg_tpl else None,
        "global": resolve_template(gr_tpl, ctx) if gr_tpl else None,
        "host": host_label_of(cfg),
        "hub_root": cfg.get("hub_root", ""),
    }


def candidates_out_path(cfg: dict, pid: str, registry: str | None, mode: str) -> pathlib.Path:
    """Куда положить JSON кандидатов. mode: project|meta|root|<path-template>."""
    host = host_label_of(cfg)
    hub = cfg.get("hub_root", "")
    if mode == "project":
        base = pathlib.Path(registry) if registry else pathlib.Path(hub) / str(pid)
        return base / f"_voiceprint-candidates-{host}.json"
    if mode == "meta":
        return pathlib.Path(hub) / "_meta" / f"801-voiceprint-candidates-{host}.json"
    if mode == "root":
        return pathlib.Path(hub) / f"_voiceprint-candidates-{host}.json"
    ctx = base_placeholder_ctx(cfg)
    ctx["pid"] = str(pid)
    ctx["host"] = host
    return pathlib.Path(resolve_template(mode, ctx))


def discover_project_stores(cfg: dict) -> list[tuple[str, str]]:
    """[(pid, store_path)] для всех проектов с voiceprints.json в cache_root (для --all)."""
    vp = (cfg.get("outputs") or {}).get("voiceprints") or {}
    store_tpl = vp.get("local_cache")
    if not store_tpl:
        return []
    # cache_root = базовая часть шаблона до {pid}
    ctx = base_placeholder_ctx(cfg)
    ctx["pid"] = "\x00PID\x00"
    resolved = resolve_template(store_tpl, ctx)
    if "\x00PID\x00" not in resolved:
        return []
    root = pathlib.Path(resolved.split("\x00PID\x00", 1)[0])
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        store = child / "voiceprints.json"
        if store.is_file():
            out.append((child.name, str(store)))
    return out


# ------------------------------------------------------------------ candidates
def _alias_summary(person: dict) -> list[dict]:
    out = []
    for a in person.get("observed_aliases") or []:
        if isinstance(a, dict) and a.get("normalized_name"):
            out.append({"name": a.get("normalized_name"), "source": a.get("source"),
                        "count": int(a.get("count") or 0)})
    return sorted(out, key=lambda x: -x["count"])


def _sessions_of(profile: dict) -> list[str]:
    seen = []
    for emb in profile.get("embeddings") or []:
        src = str((emb.get("sample_meta") or {}).get("source_file") or "").strip()
        if src and src not in seen:
            seen.append(src)
    return seen


def collect_candidates(store: dict) -> list[dict]:
    """Анонимные профили (canonical_name пуст) — кандидаты на именование."""
    cands = []
    for person in store.get("persons", []):
        if (person.get("canonical_name") or "").strip():
            continue
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
                "best_clip": profile.get("best_clip_path") or None,
                "sessions": _sessions_of(profile),
                "has_embedding": bool(profile.get("embeddings")),
            })
    return cands


def _emit_file(path: pathlib.Path, pid: str, host: str, store_path: str, cands: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema": "voiceprint-candidates-v1",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "host": host,
        "project": pid,
        "store_path": store_path,
        "count": len(cands),
        "candidates": cands,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _run_one(cfg: dict | None, pid: str, store_path: str, registry: str | None,
             out_mode: str | None, want_json: bool) -> list[dict]:
    store = load_voiceprint_store(store_path)
    cands = collect_candidates(store)
    # Запись в Hub по конфигу (если задан out_mode и есть cfg).
    if cfg is not None and out_mode:
        host = host_label_of(cfg)
        dst = candidates_out_path(cfg, pid, registry, out_mode)
        _emit_file(dst, pid, host, store_path, cands)
        print(f"[{pid}] кандидатов: {len(cands)} -> {dst}")
    if want_json:
        print(json.dumps({"project": pid, "count": len(cands), "candidates": cands},
                         ensure_ascii=False, indent=2))
    elif not (cfg is not None and out_mode):
        if not cands:
            print(f"[{pid}] анонимных профилей нет — все именованы.")
        else:
            print(f"[{pid}] кандидаты: {len(cands)}")
            for c in cands:
                sug = f" | предложение: {c['suggestion']}" if c["suggestion"] else " | предложения нет"
                al = ", ".join(f"{a['name']}({a['source']}×{a['count']})" for a in c["observed_aliases"]) or "—"
                print(f"  {c['voice_hash']}{sug}")
                print(f"      aliases: {al} | sessions: {len(c['sessions'])} | best_clip: {c['best_clip'] or '—'}")
    return cands


def cmd_list(args) -> int:
    cfg = load_config(pathlib.Path(args.config)) if args.config else None
    out_mode = args.out
    if cfg is not None and out_mode is None:
        out_mode = ((cfg.get("outputs") or {}).get("voiceprints") or {}).get(
            "candidates_out", DEFAULT_CANDIDATES_OUT)

    if args.store:  # явный стор — один проект
        _run_one(cfg, args.project or "?", args.store, args.registry, out_mode if cfg else None, args.json)
        return 0
    if cfg is None:
        print("ОШИБКА: нужен --store или --config", file=sys.stderr)
        return 2
    if args.all:
        pairs = discover_project_stores(cfg)
        if not pairs:
            print("Проекты со стором не найдены под cache_root.")
        for pid, store_path in pairs:
            paths = resolve_project_paths(cfg, pid)
            _run_one(cfg, pid, store_path, paths["registry"], out_mode, args.json)
        return 0
    if not args.project:
        print("ОШИБКА: с --config укажите --project <pid> или --all", file=sys.stderr)
        return 2
    paths = resolve_project_paths(cfg, args.project)
    if not paths["store"]:
        print("ОШИБКА: outputs.voiceprints.local_cache не задан в конфиге", file=sys.stderr)
        return 2
    _run_one(cfg, args.project, paths["store"], paths["registry"], out_mode, args.json)
    return 0


def cmd_set(args) -> int:
    store_path, registry = args.store, args.registry
    if args.config and args.project and not store_path:
        cfg = load_config(pathlib.Path(args.config))
        paths = resolve_project_paths(cfg, args.project)
        store_path = store_path or paths["store"]
        registry = registry or paths["registry"]
    if not store_path:
        print("ОШИБКА: нужен --store или (--config + --project)", file=sys.stderr)
        return 2

    store = load_voiceprint_store(store_path)
    found = _find_person_by_voice_hash(store, args.voice_hash)
    if found is None:
        print(f"ОШИБКА: voice_hash не найден: {args.voice_hash}", file=sys.stderr)
        return 2
    person, _ = found
    existing = (person.get("canonical_name") or "").strip()
    if existing and not args.force:
        print(f"ПРОПУСК: уже назван '{existing}' (--force чтобы перезаписать).")
        return 0

    name = args.name.strip()
    contact_ref = (args.contact or f"[[{name}]]").strip()
    person["canonical_name"] = name
    person["display_name"] = args.display or name.split()[0]
    person["contact_name"] = args.contact_name or name
    person["contact_ref"] = contact_ref
    save_voiceprint_store_atomic(store_path, store)

    synced = None
    if registry:
        for profile in person.get("profiles", []):
            merged = {**profile, "canonical_name": person["canonical_name"],
                      "display_name": person["display_name"], "contact_name": person["contact_name"],
                      "contact_ref": person["contact_ref"],
                      "observed_aliases": person.get("observed_aliases") or [],
                      "voiceprints": person.get("voiceprints") or [profile.get("voice_hash")]}
            synced = sync_profile_to_project_registry(merged, pathlib.Path(registry), project_id=args.project)

    print(f"OK: {args.voice_hash} -> '{name}' (contact {contact_ref})"
          + (f"; реестр обновлён" if synced else "; реестр не задан"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="voiceprint identity: list-candidates / set")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lc = sub.add_parser("list-candidates", help="выгрузить анонимные профили-кандидаты")
    lc.add_argument("--config", default=None, help="node.local.json — резолв стора/реестра по проекту")
    lc.add_argument("--project", default=None, help="pid проекта (со --config)")
    lc.add_argument("--all", action="store_true", help="все проекты со стором под cache_root")
    lc.add_argument("--store", default=None, help="явный путь к voiceprints.json (вместо --config)")
    lc.add_argument("--registry", default=None)
    lc.add_argument("--out", default=None, help="куда писать JSON: project|meta|root|<path> (по умолч. из конфига)")
    lc.add_argument("--json", action="store_true", help="печатать кандидатов в stdout JSON")
    lc.set_defaults(func=cmd_list)

    st = sub.add_parser("set", help="проставить подтверждённое имя/контакт")
    st.add_argument("--config", default=None)
    st.add_argument("--project", default=None)
    st.add_argument("--store", default=None)
    st.add_argument("--registry", default=None)
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
