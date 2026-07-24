"""Smoke-тест частичного resume ASR (select_resumable_chunks).

Пуре-логика, без GPU/аудио/whisper-инференса: раскладываем фейковые
{base}-asr-chunk-{idx:03d}.json в temp output_dir и проверяем, какие чанки
переиспользуются, а какие уходят на пере-ASR.

Запуск:  python scripts/test-partial-resume.py
(нужен тот же интерпретатор, где ставится faster_whisper — media_transcribe.py
импортирует его на верхнем уровне.)
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

try:  # консоль узлов бывает cp1251 — не роняем вывод на не-ASCII
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from media_transcribe import select_resumable_chunks  # noqa: E402


def _fp(model: str = "medium") -> dict:
    """Отпечаток в формате build_asr_recovery_fingerprint (нужные поля)."""
    return {
        "source_file_resolved": "/hub/506/rec.mp4",
        "processing_input_resolved": "/work/job/audio.wav",
        "source_size_bytes": 428_000_000,
        "duration_sec": 9000.0,
        "chunk_minutes": 20,
        "chunk_overlap_sec": 30,
        "selected_model": model,
        "quality_preset": "medium",
    }


def _write_chunk(out: pathlib.Path, base: str, idx: int, fp: dict, n_segs: int) -> list[dict]:
    segs = [
        {"start": float(idx * 1000 + i), "end": float(idx * 1000 + i + 1),
         "text": f"chunk{idx}-seg{i}", "speaker": None, "chunk_index": idx}
        for i in range(n_segs)
    ]
    doc = {
        "chunk_index": idx,
        "chunk_start_sec": float(idx * 1170),
        "chunk_overlap_sec": 30.0,
        "language": "ru",
        "segment_count": len(segs),
        "segments": segs,
        "asr_fingerprint": fp,
    }
    (out / f"{base}-asr-chunk-{idx:03d}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return segs


def main() -> int:
    base = "506-rec"
    current = _fp("medium")
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td)

        # idx 0,1 — совпадающий fp -> reuse; 2 — нет файла; 3 — чужой fp; 4 — битый JSON.
        segs0 = _write_chunk(out, base, 0, current, 3)
        segs1 = _write_chunk(out, base, 1, current, 2)
        _write_chunk(out, base, 3, _fp("large-v3"), 4)  # mismatch fp
        (out / f"{base}-asr-chunk-004.json").write_text("{ broken", encoding="utf-8")

        jobs = [{"chunk_index": i, "audio_path": f"/tmp/chunk_{i:03d}.wav"} for i in range(5)]
        warnings: list[str] = []

        reused_segs, remaining, reused_idx = select_resumable_chunks(
            out, base, jobs, current, warnings)

        remaining_idx = sorted(j["chunk_index"] for j in remaining)

        checks = [
            ("reused indices == [0,1]", sorted(reused_idx) == [0, 1]),
            ("remaining indices == [2,3,4]", remaining_idx == [2, 3, 4]),
            ("reused segments count == 5 (3+2)", len(reused_segs) == 5),
            ("reused segments content preserved",
             reused_segs[:3] == segs0 and reused_segs[3:] == segs1),
            ("mismatch warning for chunk 3",
             any("fingerprint_mismatch:chunk=3" in w for w in warnings)),
            ("no warning for missing chunk 2",
             not any("chunk=2" in w for w in warnings)),
        ]

        # Крайние случаи.
        empty_segs, empty_rem, empty_idx = select_resumable_chunks(out, base, [], current, [])
        checks.append(("empty jobs -> empty result",
                       empty_segs == [] and empty_rem == [] and empty_idx == []))

        # Все чанки совпадают -> все reused, remaining пуст.
        for i in range(3):
            _write_chunk(out, base, 10 + i, current, 1)
        alljobs = [{"chunk_index": 10 + i} for i in range(3)]
        a_segs, a_rem, a_idx = select_resumable_chunks(out, base, alljobs, current, [])
        checks.append(("all-present -> remaining empty",
                       sorted(a_idx) == [10, 11, 12] and a_rem == []))

        ok = True
        for name, passed in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
            ok = ok and passed

        print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
