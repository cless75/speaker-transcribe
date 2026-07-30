"""Гард кодировки PowerShell-скриптов: .ps1 с не-ASCII обязан иметь UTF-8 BOM.

Зачем: Windows PowerShell 5.1 (а именно он запускает scheduled task) читает .ps1
без BOM в OEM-кодировке машины. На русской Windows это CP866: кириллица в
комментариях и строках распадается, и файл ПЕРЕСТАЁТ ПАРСИТЬСЯ целиком —
"The string is missing the terminator". Скрипт умирает до первой своей строки,
поэтому не пишет ни в лог, ни свой FATAL: задача просто возвращает 1, а лог
остаётся вчерашним. Ровно так узел молчал после регистрации вотчера.

Правило: .ps1 либо ASCII-only, либо UTF-8 с BOM. Третьего не дано.

Запуск:  python scripts/test-ps-encoding.py   (ALL PASS -> exit 0)
"""
from __future__ import annotations

import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BOM = b"\xef\xbb\xbf"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    files = sorted(ROOT.rglob("*.ps1"))
    if not files:
        print("  [FAIL] не найдено ни одного .ps1 — проверять нечего")
        return 1

    checks: list[tuple[str, bool]] = []
    for path in files:
        raw = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        non_ascii = sum(1 for b in raw if b > 127)
        has_bom = raw.startswith(BOM)
        ok = (non_ascii == 0) or has_bom
        if non_ascii == 0:
            note = "ASCII-only"
        elif has_bom:
            note = f"не-ASCII ({non_ascii} байт) + BOM"
        else:
            note = f"не-ASCII ({non_ascii} байт) БЕЗ BOM -> PS 5.1 не распарсит"
        checks.append((f"{rel}: {note}", ok))

    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    if not ok:
        print("\n  Починить: пересохранить файл как UTF-8 with BOM, напр. в PowerShell:")
        print("    $t = [IO.File]::ReadAllText($p, [Text.UTF8Encoding]::new($false))")
        print("    [IO.File]::WriteAllText($p, $t, [Text.UTF8Encoding]::new($true))")
    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
