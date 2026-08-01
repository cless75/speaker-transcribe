"""Smoke-тест SessionId: время записи из имени файла + разведение коллизий.

Регрессия, которую держит этот тест: SessionId строился от mtime, поэтому пачка
файлов, скопированная в inbox одним махом, получала ОДИН SessionId — прогоны
писали в общий каталог и затирали артефакты друг друга.

Пуре-логика, без ASR и аудио: temp-файлы + фейковые run-meta.

Запуск:  python scripts/test-session-id.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import tempfile

try:  # консоль узлов бывает cp1251 — не роняем вывод на не-ASCII
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import audio_inbox_watch  # noqa: E402
from audio_inbox_watch import (  # noqa: E402
    existing_transcript,
    generate_session_id,
    parse_started_from_name,
    session_dir_taken_by_other,
    started_at_for,
)

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        failures.append(label)


def touch(path: pathlib.Path, mtime: dt.datetime) -> pathlib.Path:
    path.write_bytes(b"")
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))
    return path


def write_run_meta(directory: pathlib.Path, stem: str, source_file: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}-run-meta.json").write_text(
        json.dumps({"source_file": source_file}), encoding="utf-8")


print("1) метка времени в имени")
check("диктофон 260727_101652",
      parse_started_from_name("Голос 260727_101652"),
      dt.datetime(2026, 7, 27, 10, 16, 52))
check("Zoom GMT20260603-065526",
      parse_started_from_name("GMT20260603-065526_Recording"),
      dt.datetime(2026, 6, 3, 6, 55, 26))
check("копия «(1)» — та же запись",
      parse_started_from_name("Голос 260727_101652 (1)"),
      dt.datetime(2026, 7, 27, 10, 16, 52))
check("нет метки", parse_started_from_name("совещание-финал"), None)
check("не дата (месяц 99)", parse_started_from_name("id 999999_999999"), None)

with tempfile.TemporaryDirectory() as tmp:
    box = pathlib.Path(tmp)
    copied_at = dt.datetime(2026, 7, 30, 16, 4, 0)  # общий mtime всей пачки

    print("\n2) started_at_for: имя важнее mtime")
    a = touch(box / "Голос 260727_101652.m4a", copied_at)
    check("время записи, не копирования", started_at_for(a), dt.datetime(2026, 7, 27, 10, 16, 52))
    plain = touch(box / "совещание.m4a", copied_at)
    check("без метки — fallback на mtime", started_at_for(plain), copied_at)

    print("\n3) регрессия: пачка с общим mtime не схлопывается")
    batch = [touch(box / f"Голос {name}.m4a", copied_at) for name in
             ("260727_101652", "260727_110557", "260728_100852")]
    sids = [generate_session_id(f, started_at_for(f)) for f in batch]
    check("три записи — три SessionId", len(set(sids)), 3)
    check("SessionId от времени записи", sids[0], "S20260727T1016-golos")

    print("\n4) владение каталогом сессии")
    sess = box / "S20260727T1016-golos" / "transcripts"
    check("каталога нет — свободен", session_dir_taken_by_other(sess, batch[0]), False)
    sess.mkdir(parents=True)
    check("пустой каталог — свободен", session_dir_taken_by_other(sess, batch[0]), False)
    write_run_meta(sess, "golos_260727_101652", r"G:\Hub\552\_inbox\Голос 260727_101652.m4a")
    check("run-meta нашего файла — наш", session_dir_taken_by_other(sess, batch[0]), False)
    check("для другого файла — занят", session_dir_taken_by_other(sess, batch[1]), True)

    other = box / "S20260727T1016-other" / "transcripts"
    other.mkdir(parents=True)
    (other / "golos-transcript.md").write_text("x", encoding="utf-8")
    check("транскрипт без run-meta — не коллизия",
          session_dir_taken_by_other(other, batch[1]), False)

    print("\n5) готовый прогон находится под ЧУЖИМ slug (по run-meta)")
    sessions = box / "552" / "sessions" / "2026-07"
    # так папку назвал сторонний скрипт: slug с таймстампом, watcher строит другой
    done = sessions / "S20260727T1105-golos-260727-110557" / "transcripts"
    done.mkdir(parents=True)
    (done / "golos_260727_110557-transcript.md").write_text("текст", encoding="utf-8")
    write_run_meta(done, "golos_260727_110557", r"G:\Hub\552\_inbox\Голос 260727_110557.m4a")
    audio = touch(box / "Голос 260727_110557.m4a", copied_at)
    expected = sessions / "S20260727T1105-golos" / "transcripts"  # ожидание watcher'а: пусто
    audio_inbox_watch._SESSIONS_INDEX_CACHE.clear()
    found = existing_transcript(audio, expected)
    check("транскрипт найден", found is not None, True)
    check("именно наш", found.name if found else None, "golos_260727_110557-transcript.md")

    audio_inbox_watch._SESSIONS_INDEX_CACHE.clear()
    stranger = touch(box / "Голос 260101_000000.m4a", copied_at)
    check("чужому файлу не отдаём", existing_transcript(stranger, expected), None)

    audio_inbox_watch._SESSIONS_INDEX_CACHE.clear()
    empty_done = sessions / "S20260101T0000-empty" / "transcripts"
    empty_done.mkdir(parents=True)
    (empty_done / "golos_260101_000001-transcript.md").write_text("", encoding="utf-8")
    write_run_meta(empty_done, "golos_260101_000001", r"G:\Hub\552\_inbox\Голос 260101_000001.m4a")
    oborvan = touch(box / "Голос 260101_000001.m4a", copied_at)
    check("пустой транскрипт не считается готовым",
          existing_transcript(oborvan, expected), None)

print()
if failures:
    print(f"FAILED: {len(failures)} — {', '.join(failures)}")
    sys.exit(1)
print("PASS")
