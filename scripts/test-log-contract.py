"""Регрессия-гард контракта логов ↔ log_critical.

Проверяет, что ни одна лог-строка, ПОНИЖЕННАЯ до level="debug" (и потому не попадающая
в файл лога при дефолтном ASR_LOG_DEBUG), не матчит ни один PATTERN из log_critical.py.
Иначе критический сигнал (EDEADLK, DIARIZATION FAILED, aborting sweep …) молча исчез бы
из анализатора. Статический AST-скан — GPU/whisper не требуется.

Запуск:  python scripts/test-log-contract.py   (ALL PASS → exit 0)
"""
from __future__ import annotations

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from log_critical import PATTERNS  # noqa: E402  (list[(re.Pattern, level, label)])

# Позиция аргумента-сообщения по имени функции и файлу:
#   media_transcribe.log(payload, msg, level)   -> msg = args[1]
#   media_transcribe.stderr_log_line(msg, level) -> msg = args[0]
#   audio_inbox_watch.log(msg, level)            -> msg = args[0]
TARGETS = {
    "src/media_transcribe.py": {"log": 1, "stderr_log_line": 0},
    "src/audio_inbox_watch.py": {"log": 0},
}


def static_text(node: ast.AST) -> str:
    """Собрать статический текст сообщения (литералы f-string/конкатенации; {expr} игнор)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return static_text(node.left) + static_text(node.right)
    return ""


def level_is_debug(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "level" and isinstance(kw.value, ast.Constant):
            return kw.value.value == "debug"
    return False


def scan_file(rel: str, fn_arg: dict[str, int]) -> tuple[int, list[str]]:
    path = REPO / rel
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    debug_count = 0
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        fname = node.func.id
        if fname not in fn_arg or not level_is_debug(node):
            continue
        debug_count += 1
        idx = fn_arg[fname]
        if idx >= len(node.args):
            continue
        text = static_text(node.args[idx])
        for pat, plevel, label in PATTERNS:
            if pat.search(text):
                violations.append(
                    f"{rel}:{node.lineno} level=debug матчит PATTERN "
                    f"[{plevel}:{label}] -> {text!r}"
                )
    return debug_count, violations


def main() -> int:
    total_debug = 0
    all_violations: list[str] = []
    for rel, fn_arg in TARGETS.items():
        n, viol = scan_file(rel, fn_arg)
        total_debug += n
        all_violations.extend(viol)
        print(f"  {rel}: level=debug вызовов = {n}")

    print(f"\n  всего debug-строк: {total_debug} | PATTERNS: {len(PATTERNS)}")
    if all_violations:
        print("\n  [FAIL] нарушения контракта (критическая строка спрятана в debug):")
        for v in all_violations:
            print("   -", v)
        print("\nRESULT: FAIL")
        return 1
    # Санити: должны реально что-то демотировать (иначе скан сломан).
    ok_nonzero = total_debug > 0
    print(f"  [{'PASS' if ok_nonzero else 'FAIL'}] найдены debug-строки для проверки")
    print("\nRESULT:", "ALL PASS" if ok_nonzero else "FAIL (0 debug-строк — скан сломан?)")
    return 0 if ok_nonzero else 1


if __name__ == "__main__":
    sys.exit(main())
