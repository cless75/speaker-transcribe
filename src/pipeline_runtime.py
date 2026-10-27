"""Independent local stage consumers; adapters are trusted injected Python code.

No production CLI or implicit service switch. A deployment adapter must enforce
source claims, mounts, quality, mandatory outputs and real publication evidence.
"""
from __future__ import annotations

import json
import pathlib
import threading
from contextlib import ExitStack

from pipeline_budget import DiskPressure
from pipeline_queue import LeaseLost, PipelineQueue
from pipeline_storage import EvidenceError, ResourceBusy, file_lock


class HoldJob(ValueError):
    pass


class RetryJob(RuntimeError):
    pass


class StageRunner:
    """One instance/SQLite connection per consumer thread or process."""

    def __init__(self, db_path: pathlib.Path, receipts, *, gpu_lock: pathlib.Path,
                 handlers: dict, owner: str, budget=None, reserve_bytes=None,
                 lease_seconds: float = 300, retry_seconds: float = 60,
                 max_attempts: int = 5, clock=None):
        if set(handlers) != {"discovered", "ready", "publish_pending"}:
            raise ValueError("all_stage_handlers_required")
        if lease_seconds < 0.3 or retry_seconds <= 0 or max_attempts < 1:
            raise ValueError("invalid_runner_limits")
        if (budget is None) != (reserve_bytes is None):
            raise ValueError("budget_and_estimator_required_together")
        self.db_path, self.receipts, self.gpu_lock = db_path, receipts, gpu_lock
        self.queue = PipelineQueue(db_path, **({"clock": clock} if clock else {}))
        self.handlers, self.owner, self.budget = handlers, owner, budget
        self.reserve_bytes = reserve_bytes
        self.lease_seconds, self.retry_seconds = lease_seconds, retry_seconds
        self.max_attempts, self.clock = max_attempts, clock

    def close(self):
        self.queue.close()

    def _fail(self, job, reason, retry=None):
        try:
            self.queue.fail(job["id"], job["token"], reason, retry_seconds=retry)
        except LeaseLost:
            return "lease_lost"
        return "retry_wait" if retry is not None else "held"

    def _resources(self, stack, stage, key=None):
        if stage == "ready":
            stack.enter_context(file_lock(self.gpu_lock))
        if key is not None:
            stack.enter_context(file_lock(self.receipts.directory(key) / "resource.lock"))

    def tick(self, stage: str) -> str:
        if stage not in self.handlers:
            raise ValueError("unknown_stage")
        with ExitStack() as resources:
            try:
                # Obtain GPU ownership BEFORE claiming work; no busy-GPU leases.
                self._resources(resources, stage)
            except ResourceBusy:
                return "resource_busy"
            job = self.queue.claim(stage, self.owner, lease_seconds=self.lease_seconds)
            if job is None:
                return "idle"
            try:
                self._resources(resources, "job", job["id"])
            except ResourceBusy:
                # A stale stage can still own the files. Never run concurrently.
                self.queue.fail(job["id"], job["token"], "resource_owned")
                return "held"
            stopped, failed = threading.Event(), threading.Event()

            def heartbeat():
                queue = None
                try:
                    queue = PipelineQueue(self.db_path, **({"clock": self.clock} if self.clock else {}))
                    while not stopped.wait(self.lease_seconds / 3):
                        queue.heartbeat(job["id"], job["token"], lease_seconds=self.lease_seconds)
                except Exception:
                    failed.set()
                finally:
                    if queue is not None:
                        queue.close()

            def check():
                if failed.is_set():
                    raise LeaseLost("heartbeat_failed")
                self.queue._owned(job["id"], job["token"])
                if self.budget is not None:
                    directory = self.receipts.directory(job["id"])
                    used = sum(p.stat().st_size for scope in ("input", "result")
                               for p in (directory / scope).rglob("*") if p.is_file())
                    self.budget.check(job["id"], used)

            pulse = threading.Thread(target=heartbeat, name="pipeline-lease", daemon=True)
            pulse.start()
            try:
                old = self.receipts.read(job, stage)
                if old is not None and stage == "publish_pending":
                    verifier = getattr(self.handlers[stage], "verify_completed", None)
                    if verifier is None or verifier(job, old, check) is not True:
                        raise EvidenceError("delivery_reverification_required")
                if old is None:
                    previous = {"ready": "discovered", "publish_pending": "ready"}.get(stage)
                    if previous and self.receipts.read(job, previous) is None:
                        raise EvidenceError("missing_upstream_receipt")
                    if self.budget is not None:
                        if stage == "discovered":
                            self.budget.reserve(job["id"], self.reserve_bytes(json.loads(job["payload"])))
                    check()
                    # The ASR handler must not perform discovery or publication.
                    metadata = self.handlers[stage](job, self.receipts.directory(job["id"]), check)
                    check()
                    if not isinstance(metadata, dict):
                        raise HoldJob("invalid_stage_metadata")
                    self.receipts.write(job, stage, metadata)
                check()
                self.queue.finish(job["id"], job["token"])
                return "finished"
            except LeaseLost:
                # Keep token/receipt for reconciliation, never automatically steal.
                return "lease_lost"
            except (EvidenceError, HoldJob, ValueError) as exc:
                return self._fail(job, type(exc).__name__)
            except (DiskPressure, RetryJob, OSError, RuntimeError) as exc:
                retry = self.retry_seconds if job["attempts"] < self.max_attempts else None
                return self._fail(job, type(exc).__name__, retry)
            finally:
                stopped.set()
                pulse.join()

    def serve(self, stage: str, stop: threading.Event, *, poll_seconds: float = 1):
        if not 0 < poll_seconds <= 2:
            raise ValueError("poll_must_be_at_most_two_seconds")
        while not stop.is_set():
            outcome = self.tick(stage)
            if outcome != "finished":
                stop.wait(poll_seconds)

    def reconcile(self, key: str) -> str:
        """Manual receipt-only recovery. No evidence => no transition/re-execution."""
        job = self.queue.get(key)
        if job is None or job["resume_stage"] not in self.handlers:
            raise ValueError("not_an_inflight_job")
        stage = job["resume_stage"]
        with ExitStack() as resources:
            self._resources(resources, stage, key)
            def verify(current, current_stage):
                receipt = self.receipts.read(current, current_stage)
                if receipt is None:
                    return False
                if current_stage == "publish_pending":
                    verifier = getattr(self.handlers[current_stage], "verify_completed", None)
                    return verifier is not None and verifier(current, receipt, lambda: None) is True
                return True
            self.queue.reconcile_finished(
                key, job["token"], verify=verify)
        return "reconciled"
