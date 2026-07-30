"""Тест резолва секретов из конфига узла (apply_config_secrets).

Секрет описывается ИСТОЧНИКОМ, а не значением:
  env:ИМЯ    — переменная окружения машины
  file:путь  — файл (обычно {hub_root}/_meta/.hf-token — один источник на все узлы)

Проверяем:
  1. file: кладёт значение в HF_TOKEN (и {hub_root} в пути резолвится);
  2. BOM и перевод строки в файле не попадают в значение;
  3. отсутствующий файл не роняет прогон и НЕ затирает уже заданную переменную;
  4. пустой файл не затирает уже заданную переменную;
  5. env:ДРУГОЕ_ИМЯ переносится в HF_TOKEN (движок ждёт именно его);
  6. непонятный префикс игнорируется, окружение не меняется;
  7. секрет не утекает в лог (stderr вотчера).

Запуск:  python scripts/test-config-secrets.py   (ALL PASS -> exit 0)
"""
from __future__ import annotations

import contextlib
import io
import os
import pathlib
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from audio_inbox_watch import apply_config_secrets  # noqa: E402

SECRET = "hf_TESTtoken1234567890"


def run(cfg: dict, env: dict) -> tuple[dict, str]:
    """Прогнать резолв в изолированном окружении. Возвращает (env, stderr)."""
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            apply_config_secrets(cfg)
        return dict(os.environ), err.getvalue()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def main() -> int:
    checks: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as td:
        hub = pathlib.Path(td)
        meta = hub / "_meta"
        meta.mkdir()
        token_file = meta / ".hf-token"
        # BOM + хвостовой перевод строки: ровно то, что оставляют "Блокнот" и echo.
        token_file.write_text("﻿" + SECRET + "\n", encoding="utf-8")

        cfg_file = {"hub_root": str(hub),
                    "secrets": {"hf_token": "file:{hub_root}/_meta/.hf-token"}}

        env, err = run(cfg_file, {})
        checks.append(("file: кладёт значение в HF_TOKEN", env.get("HF_TOKEN") == SECRET))
        checks.append(("BOM и \\n из файла отброшены", "﻿" not in (env.get("HF_TOKEN") or "")
                       and not (env.get("HF_TOKEN") or "").endswith("\n")))
        checks.append(("значение секрета не попало в лог", SECRET not in err))

        # 3. Источник исчез (Hub не смонтирован) — прежнее значение остаётся в силе.
        cfg_missing = {"hub_root": str(hub),
                       "secrets": {"hf_token": "file:{hub_root}/_meta/.nope"}}
        env, err = run(cfg_missing, {"HF_TOKEN": "prior"})
        checks.append(("нет файла -> прежняя переменная цела", env.get("HF_TOKEN") == "prior"))
        checks.append(("нет файла -> сказано в логе", "secret hf_token" in err))

        # 4. Пустой файл — тоже не повод затирать рабочее значение.
        (meta / ".empty").write_text("   \n", encoding="utf-8")
        cfg_empty = {"hub_root": str(hub),
                     "secrets": {"hf_token": "file:{hub_root}/_meta/.empty"}}
        env, _ = run(cfg_empty, {"HF_TOKEN": "prior"})
        checks.append(("пустой файл -> прежняя переменная цела", env.get("HF_TOKEN") == "prior"))

    # 5. Конфиг указывает на переменную с другим именем, чем ждёт движок.
    cfg_env = {"secrets": {"hf_token": "env:HUGGINGFACE_TOKEN"}}
    env, _ = run(cfg_env, {"HUGGINGFACE_TOKEN": SECRET})
    checks.append(("env:ДРУГОЕ_ИМЯ переносится в HF_TOKEN", env.get("HF_TOKEN") == SECRET))

    # ...но уже заданный HF_TOKEN приоритетнее (локальный override для отладки).
    env, _ = run(cfg_env, {"HUGGINGFACE_TOKEN": SECRET, "HF_TOKEN": "explicit"})
    checks.append(("явный HF_TOKEN не перетирается", env.get("HF_TOKEN") == "explicit"))

    # 6. Мусорный источник: не падаем, ничего не меняем, говорим вслух.
    cfg_bad = {"secrets": {"hf_token": "vault:secret/hf"}}
    env, err = run(cfg_bad, {})
    checks.append(("непонятный префикс игнорируется", "HF_TOKEN" not in env))
    checks.append(("непонятный префикс отмечен в логе", "непонятный источник" in err))

    # 7. Пустой/отсутствующий блок secrets не должен ничего ломать.
    env, _ = run({}, {"HF_TOKEN": "prior"})
    checks.append(("без блока secrets окружение не трогаем", env.get("HF_TOKEN") == "prior"))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
