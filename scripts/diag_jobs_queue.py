"""Почему узел не взял ни одного задания — разбор очереди тем же кодом, что и вотчер.

Догадки здесь не годятся: вердикт отбора должен выноситься теми же функциями, что
работают в свипе, иначе диагностика уверенно объяснит несуществующую причину. Скрипт
читает конфиг узла, повторяет отбор ``jobs_queue`` по каждому заданию и печатает, что
именно помешало — статус, requires, неисполнимый параметр или чужой живой claim.

Ничего не меняет: только чтение. Запускается как самим человеком, так и из
collect-diag.ps1.

    python scripts/diag_jobs_queue.py [--config config\node.local.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

try:
    import jobs_queue
except Exception as exc:  # pragma: no cover - diagnostics must never die
    print(f"FATAL: cannot import jobs_queue: {type(exc).__name__}: {exc}")
    print("The node is running code without the jobs queue — that alone explains "
          "an idle sweep. Check: git -C <repo> log --oneline -1")
    raise SystemExit(0)


def load_config(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def claim_state(job_path: pathlib.Path) -> str:
    claim = job_path.with_name(job_path.name + ".claim.json")
    if not claim.is_file():
        return "-"
    try:
        data = (json.loads(claim.read_text(encoding="utf-8-sig")) or {}).get("claim", {})
    except Exception as exc:
        return f"unreadable ({type(exc).__name__})"
    return (f"by={data.get('claimed_by')} phase={data.get('claim_phase')} "
            f"lease_until={data.get('lease_until')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(pathlib.Path("config") / "node.local.json"))
    args = ap.parse_args()

    print("=== jobs queue diagnosis ===")
    print(f"time      : {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    cfg_path = pathlib.Path(args.config)
    if not cfg_path.is_file():
        print(f"FATAL: node config not found: {cfg_path}")
        return 0
    cfg = load_config(cfg_path)

    hub_raw = cfg.get("hub_root")
    print(f"hub_root  : {hub_raw}")
    if not hub_raw:
        print("VERDICT: hub_root missing from the node config — the queue cannot be read.")
        return 0
    hub = pathlib.Path(str(hub_raw)).expanduser()
    if not hub.is_dir():
        print("VERDICT: hub_root is NOT reachable from this user right now.")
        print("  A sweep that starts before the cloud drive is mounted sees no queue and")
        print("  finishes silently — the usual shape of 'the node started and took nothing'.")
        return 0
    jobs_dir = hub / "_jobs"
    print(f"_jobs     : {jobs_dir} — {'present' if jobs_dir.is_dir() else 'MISSING'}")
    if not jobs_dir.is_dir():
        print("VERDICT: no _jobs directory: process_next_job returns immediately, "
              "and by design says nothing.")
        return 0

    caps = jobs_queue._node_value(cfg, "capabilities", []) or []
    print(f"node caps : {sorted(caps)}")
    print(f"free VRAM : {jobs_queue._node_value(cfg, 'free_vram_gb', None) or 'probed at run time'}")
    print()

    files = [p for p in sorted(jobs_dir.glob("*.json")) if not p.name.endswith(".claim.json")]
    if not files:
        print("VERDICT: the queue is empty — nothing to take.")
        return 0

    runnable = []
    for path in files:
        try:
            job = jobs_queue._read_job(path)
        except Exception as exc:
            print(f"{path.name}\n   BROKEN JSON: {type(exc).__name__}: {exc}")
            continue
        status = job.get("status")
        if status == "blocked":
            verdict = "never taken: status=blocked"
        elif status in jobs_queue.TERMINAL_STATUSES:
            verdict = f"finished earlier: status={status}"
        elif status not in jobs_queue.READY_STATUSES:
            verdict = (f"not in selection: status={status} "
                       f"(reclaimed only once the claim lease expires)")
        else:
            reason = jobs_queue._eligibility(job, cfg)
            verdict = "RUNNABLE — this node can take it" if reason is None else f"skipped: {reason}"
            if reason is None:
                runnable.append(path.name)
        print(f"{path.name}\n   {verdict}\n   claim: {claim_state(path)}")

    print()
    if runnable:
        print(f"VERDICT: {len(runnable)} job(s) ARE runnable on this node: {', '.join(runnable)}")
        print("  If the sweep still took none, the queue step never ran. Look in")
        print("  logs/watch.log for 'jobs queue skipped this sweep' (the guard that keeps a")
        print("  queue failure from stopping the inbox flow — it logs there, not to the Hub).")
    else:
        print("VERDICT: no job is runnable on this node — an idle sweep is correct behaviour.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
