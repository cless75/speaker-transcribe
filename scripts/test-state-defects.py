"""Регрессия-гард состояния sidecar: классификация ошибок, потолок отложек, backoff.

Три дефекта, которые держит этот тест:

1. **Отложка без потолка.** Ошибка облачного монтирования возвращала файл в очередь,
   не считая попытку. Потребитель у `attempts` ровно один — retry-логика, поэтому
   transient-класс не имел предела вообще: на M4-MAC один файл дал 14 776 прогонов
   за сутки. Теперь отложки считаются отдельно (`cloud_defers`), растянуты по времени
   (`retry_not_before`) и упираются в потолок, после которого файл идёт обычным путём.

2. **Классификация по подстроке.** В признаках стояло голое "errno 11" — это EDEADLK
   только на macOS, а на Linux это EAGAIN, штатное состояние. Плюс любая ошибка из
   staging считалась облачной, включая ENOSPC — то есть кончившееся место откладывалось
   бы вечно вместо того, чтобы позвать человека.

3. **`finished_at` без завершения.** Метку ставили до классификации исхода, поэтому её
   уносило в ветку `queued`; следующая попытка переписывала `started_at` на более
   поздний момент — запись «завершено раньше, чем начато». Инвариант проверяется здесь
   на уровне хелперов, сам порядок присвоений — в `process_one_file`.

Пуре-логика: ни файловой системы, ни облака, ни ASR.

Запуск:  python scripts/test-state-defects.py   (ALL PASS -> exit 0)
"""
from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
import sys

try:  # консоль узлов бывает cp1251 — не роняем вывод на не-ASCII
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import audio_inbox_watch as w  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


@contextlib.contextmanager
def platform_as(name: str):
    """Подмена sys.platform: классификация errno 11 зависит от ОС."""
    original = sys.platform
    sys.platform = name
    try:
        yield
    finally:
        sys.platform = original


print("[1] классификация ошибок")
with platform_as("darwin"):
    check("EDEADLK на macOS — облачная",
          w._is_transient_cloud_error("OSError: [Errno 11] Resource deadlock avoided"), True)
    check("errno 11 на macOS — облачная",
          w._is_transient_cloud_error("OSError: [Errno 11] something"), True)
with platform_as("linux"):
    check("errno 11 на Linux (EAGAIN) — НЕ облачная",
          w._is_transient_cloud_error("OSError: [Errno 11] Resource temporarily unavailable"), False)
    check("формулировка deadlock и на Linux облачная",
          w._is_transient_cloud_error("OSError: Resource deadlock avoided"), True)

check("ETIMEDOUT — облачная",
      w._is_transient_cloud_error("OSError: [Errno 60] Operation timed out"), True)
check("staging без иной причины — облачная",
      w._is_transient_cloud_error('File "media_transcribe.py", in stage_ascii_input'), True)
check("ENOSPC внутри staging — НЕ облачная",
      w._is_transient_cloud_error(
          'in stage_ascii_input\nOSError: [Errno 28] No space left on device'), False)
check("EACCES внутри staging — НЕ облачная",
      w._is_transient_cloud_error('in stage_ascii_input\nPermissionError: Permission denied'), False)
check("WinError 87 — НЕ облачная",
      w._is_transient_cloud_error("OSError: [WinError 87] Параметр задан неверно"), False)
check("обычная ошибка медиа — НЕ облачная",
      w._is_transient_cloud_error("RuntimeError: ffmpeg failed with code 1"), False)

print("[2] лестница backoff")
check("1-я отложка", w.cloud_defer_backoff_minutes(1, {}), 1.0)
check("2-я отложка", w.cloud_defer_backoff_minutes(2, {}), 5.0)
check("3-я отложка", w.cloud_defer_backoff_minutes(3, {}), 15.0)
check("4-я отложка", w.cloud_defer_backoff_minutes(4, {}), 60.0)
check("последняя ступень повторяется", w.cloud_defer_backoff_minutes(99, {}), 60.0)
check("лестница из конфига", w.cloud_defer_backoff_minutes(2, {"cloud_defer_backoff_minutes": [2, 7]}), 7.0)
check("битая ступень не роняет прогон",
      w.cloud_defer_backoff_minutes(1, {"cloud_defer_backoff_minutes": ["ой"]}), 60.0)

print("[3] потолок отложек")
check("свежий файл — не исчерпан", w._cloud_defers_exhausted({}, {}), False)
check("19 из 20 — не исчерпан", w._cloud_defers_exhausted({"cloud_defers": 19}, {}), False)
check("20 из 20 — исчерпан", w._cloud_defers_exhausted({"cloud_defers": 20}, {}), True)
check("потолок из конфига", w._cloud_defers_exhausted({"cloud_defers": 3}, {"max_cloud_defers": 3}), True)
check("нулевой потолок выключает отложки",
      w._cloud_defers_exhausted({}, {"max_cloud_defers": 0}), True)

print("[4] ворота backoff")
now = dt.datetime(2026, 8, 6, 12, 0, 0)
future = (now + dt.timedelta(minutes=5)).isoformat(timespec="seconds")
past = (now - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
check("без метки — открыты", w.retry_gate_open({}, now), True)
check("метка в будущем — закрыты", w.retry_gate_open({"retry_not_before": future}, now), False)
check("метка в прошлом — открыты", w.retry_gate_open({"retry_not_before": past}, now), True)
check("нечитаемая метка не запирает файл",
      w.retry_gate_open({"retry_not_before": "не дата"}, now), True)
check("null-метка не запирает файл", w.retry_gate_open({"retry_not_before": None}, now), True)

print("[5] сброс после успеха")
state = {"cloud_defers": 7, "retry_not_before": future, "status": "asr-done"}
w.clear_retry_gate(state)
check("cloud_defers снят", "cloud_defers" in state, False)
check("retry_not_before снят", "retry_not_before" in state, False)
check("остальное состояние не тронуто", state.get("status"), "asr-done")

print("[6] ретрай staging (пропускается без ASR-зависимостей)")
try:
    import media_transcribe as mt  # noqa: E402  — тянет faster_whisper, есть не везде
except Exception as exc:
    print(f"  skip media_transcribe недоступен: {type(exc).__name__}")
else:
    import errno as _errno
    import time as _time

    original_copy, original_sleep = mt._copy_file_bounded, _time.sleep
    _time.sleep = lambda _s: None
    try:
        calls = {"n": 0}

        def flaky(src, dst, timeout_sec=180, chunk=0):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(_errno.EDEADLK, "Resource deadlock avoided")

        mt._copy_file_bounded = flaky
        warns: list[str] = []
        mt._stage_copy_with_retry(pathlib.Path("src"), pathlib.Path("dst"), 180, warns)
        check("облачный отказ ретраится до успеха", calls["n"], 3)
        check("успех после ретрая помечен в warnings",
              any(x.startswith("input_staging_succeeded_after_retry") for x in warns), True)

        calls["n"] = 0

        def enospc(src, dst, timeout_sec=180, chunk=0):
            calls["n"] += 1
            raise OSError(_errno.ENOSPC, "No space left on device")

        mt._copy_file_bounded = enospc
        try:
            mt._stage_copy_with_retry(pathlib.Path("src"), pathlib.Path("dst"), 180, [])
            check("ENOSPC пробрасывается сразу", "не упало", "OSError")
        except OSError:
            check("ENOSPC пробрасывается сразу, без ретраев", calls["n"], 1)

        calls["n"] = 0

        def always_wedged(src, dst, timeout_sec=180, chunk=0):
            calls["n"] += 1
            raise OSError(_errno.EDEADLK, "Resource deadlock avoided")

        mt._copy_file_bounded = always_wedged
        try:
            mt._stage_copy_with_retry(pathlib.Path("src"), pathlib.Path("dst"), 180, [])
            check("бесконечный отказ сдаётся", "не упало", "OSError")
        except OSError:
            check("бесконечный отказ сдаётся после лестницы", calls["n"], 4)
    finally:
        mt._copy_file_bounded, _time.sleep = original_copy, original_sleep

print()
if failures:
    print(f"RESULT: {len(failures)} FAIL")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("RESULT: ALL PASS")
