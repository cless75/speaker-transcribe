"""Smoke-тест ожидания hub_root: гонка старта задачи с монтированием облачного диска.

Регрессия, которую держит этот тест: задача стартует по AtLogOn раньше, чем
Google Drive поднимает букву диска. Прогон, попавший в это окно, не читал секрет
из {hub_root}/_meta/.hf-token, не пробивал ни один source root и уходил в
«nothing to do» — узел молчал, хотя минутой позже Hub был на месте.

Пуре-логика: _probe_dir и часы подменены, реального ожидания нет.

Запуск:  python scripts/test-hub-wait.py
"""
from __future__ import annotations

import pathlib
import sys

try:  # консоль узлов бывает cp1251 — не роняем вывод на не-ASCII
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import audio_inbox_watch  # noqa: E402
from audio_inbox_watch import resolved_hub_root, wait_for_hub_root  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


class FakeClock:
    """time.monotonic/sleep без реального ожидания: sleep двигает стрелки."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, sec: float) -> None:
        self.slept.append(sec)
        self.now += max(0.0, sec)


class FakeProbe:
    """_probe_dir, который становится успешным после N неудачных проб."""

    def __init__(self, ok_after: int) -> None:
        self.ok_after = ok_after
        self.calls = 0

    def __call__(self, path: pathlib.Path):
        self.calls += 1
        if self.ok_after >= 0 and self.calls > self.ok_after:
            return (True, None)
        return (False, f"cannot read source {path}: not mounted")


def run(cfg: dict, ok_after: int) -> tuple[bool, FakeClock, FakeProbe]:
    clock, probe = FakeClock(), FakeProbe(ok_after)
    real_time, real_probe = audio_inbox_watch.time, audio_inbox_watch._probe_dir
    audio_inbox_watch.time = clock
    audio_inbox_watch._probe_dir = probe
    try:
        return wait_for_hub_root(cfg), clock, probe
    finally:
        audio_inbox_watch.time = real_time
        audio_inbox_watch._probe_dir = real_probe


HUB = {"hub_root": "G:/Мой диск/Hub", "hub_wait_seconds": 300, "hub_wait_poll_sec": 10}

print("1) hub_root из конфига")
check("плейсхолдеров нет — путь как есть",
      resolved_hub_root({"hub_root": "G:/Hub"}), pathlib.Path("G:/Hub"))
check("hub_root не задан", resolved_hub_root({"node": {}}), None)

print("\n2) здоровый узел: диск на месте — задержки нет")
ok, clock, probe = run(HUB, ok_after=0)
check("продолжаем прогон", ok, True)
check("ни одного ожидания", clock.slept, [])
check("одна проба", probe.calls, 1)

print("\n3) регрессия: диск поднимается через полминуты после логина")
ok, clock, probe = run(HUB, ok_after=3)
check("дождались, а не ушли в 'nothing to do'", ok, True)
check("ждали ~30 с", clock.now, 30.0)
check("пробовали каждые 10 с", clock.slept, [10.0, 10.0, 10.0])

print("\n4) диск не смонтирован вовсе — сдаёмся по таймауту")
ok, clock, _ = run(HUB, ok_after=-1)
check("прогон не продолжаем", ok, False)
check("ждали ровно hub_wait_seconds", clock.now, 300.0)

print("\n5) ожидание отключено (узел без облачного Hub)")
ok, clock, probe = run({**HUB, "hub_wait_seconds": 0}, ok_after=-1)
check("возврат сразу", ok, False)
check("ни одного ожидания", clock.slept, [])
check("одна проба", probe.calls, 1)

print("\n6) поллинг не перепрыгивает дедлайн")
ok, clock, _ = run({**HUB, "hub_wait_seconds": 25}, ok_after=-1)
check("последняя проба ровно на дедлайне", clock.now, 25.0)

print("\n7) hub_root не задан — ждать нечего")
ok, clock, probe = run({"sources": []}, ok_after=-1)
check("продолжаем прогон", ok, True)
check("проб не было", probe.calls, 0)

print()
if failures:
    print(f"FAILED: {len(failures)} — {', '.join(failures)}")
    sys.exit(1)
print("PASS")
