"""Isolated v1 stage queue; no production migration, NAS I/O or GPU execution.

Scheduling leases are NOT filesystem/GPU locks. Expired work must be reconciled
by an operator/adapter after proving resource ownership, never auto-stolen.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import sqlite3
import time
import uuid
from contextlib import contextmanager


TRANSITIONS = {
    "discovered": ("staging", "ready"),
    "ready": ("transcribing", "publish_pending"),
    "publish_pending": ("publishing", "done"),
}


class LeaseLost(RuntimeError):
    pass


class PipelineQueue:
    """One connection per process/thread, DB on local storage only."""

    def __init__(self, path: pathlib.Path, *, clock=time.time):
        self.clock = clock
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            # Refuse existing unrelated databases; never migrate live queue implicitly.
            tables = {r[0] for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "pipeline_schema" not in tables:
                raise ValueError("not an isolated pipeline database")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=10000")
            with self._transaction():
                self.db.execute("CREATE TABLE IF NOT EXISTS pipeline_schema (version INTEGER NOT NULL)")
                versions = [r[0] for r in self.db.execute("SELECT version FROM pipeline_schema")]
                if not versions:
                    self.db.execute("INSERT INTO pipeline_schema VALUES (1)")
                elif versions != [1]:
                    raise ValueError("unsupported pipeline schema")
                self.db.execute("""CREATE TABLE IF NOT EXISTS pipeline_jobs (
                    id TEXT PRIMARY KEY, identity TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, resume_stage TEXT, priority INTEGER NOT NULL,
                    created REAL NOT NULL, eligible REAL NOT NULL,
                    owner TEXT, token TEXT, expires REAL, attempts INTEGER NOT NULL DEFAULT 0,
                    reason TEXT)""")
                self.db.execute("CREATE INDEX IF NOT EXISTS pipeline_eligible "
                                "ON pipeline_jobs(state, eligible, priority, created)")
        except Exception:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()

    def enqueue(self, submission: str, revision: str, profile: str, engine: str,
                payload: dict, *, priority: int = 100) -> str:
        if not all(isinstance(x, str) and x for x in (submission, revision, profile, engine)):
            raise ValueError("complete processing identity required")
        identity = json.dumps([submission, revision, profile, engine], ensure_ascii=False,
                              separators=(",", ":"))
        key = hashlib.sha256(identity.encode()).hexdigest()
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        when = self.clock()
        with self._transaction():
            old = self.db.execute("SELECT identity,payload FROM pipeline_jobs WHERE id=?", (key,)).fetchone()
            if old and (old["identity"], old["payload"]) != (identity, raw):
                raise ValueError("same identity with different payload")
            if not old:
                self.db.execute("INSERT INTO pipeline_jobs "
                                "(id,identity,payload,state,priority,created,eligible) "
                                "VALUES (?,?,?,'discovered',?,?,?)",
                                (key, identity, raw, priority, when, when))
        return key

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM pipeline_jobs WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _duration(value: float):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("duration must be finite and positive")

    def claim(self, stage: str, owner: str, *, lease_seconds: float = 300) -> dict | None:
        if stage not in TRANSITIONS or not owner:
            raise ValueError("valid stage and owner required")
        self._duration(lease_seconds)
        when = self.clock()
        with self._transaction():
            row = self.db.execute("SELECT id FROM pipeline_jobs WHERE eligible<=? AND "
                                  "(state=? OR (state='retry_wait' AND resume_stage=?)) "
                                  "ORDER BY priority,created,id LIMIT 1",
                                  (when, stage, stage)).fetchone()
            if row is None:
                return None
            key, token = row["id"], uuid.uuid4().hex
            self.db.execute("UPDATE pipeline_jobs SET state=?,resume_stage=?,owner=?,token=?,"
                            "expires=?,attempts=attempts+1,reason=NULL WHERE id=?",
                            (TRANSITIONS[stage][0], stage, owner, token, when + lease_seconds, key))
            return self.get(key)

    def _owned(self, key: str, token: str) -> dict:
        job = self.get(key)
        if (not job or not token or job["token"] != token or
                job["expires"] is None or job["expires"] <= self.clock()):
            raise LeaseLost("lease expired or fenced")
        return job

    def heartbeat(self, key: str, token: str, *, lease_seconds: float = 300):
        self._duration(lease_seconds)
        with self._transaction():
            self._owned(key, token)
            self.db.execute("UPDATE pipeline_jobs SET expires=? WHERE id=?",
                            (self.clock() + lease_seconds, key))

    def finish(self, key: str, token: str):
        """Caller must verify durable stage receipt before advancing."""
        with self._transaction():
            job = self._owned(key, token)
            target = TRANSITIONS[job["resume_stage"]][1]
            self.db.execute("UPDATE pipeline_jobs SET state=?,resume_stage=NULL,owner=NULL,"
                            "token=NULL,expires=NULL WHERE id=?", (target, key))

    def fail(self, key: str, token: str, reason: str, *, retry_seconds: float | None = None):
        if not reason:
            raise ValueError("reason required")
        if retry_seconds is not None:
            self._duration(retry_seconds)
        with self._transaction():
            self._owned(key, token)
            self.db.execute("UPDATE pipeline_jobs SET state=?,eligible=?,reason=?,owner=NULL,"
                            "token=NULL,expires=NULL WHERE id=?",
                            ("held" if retry_seconds is None else "retry_wait",
                             self.clock() + (retry_seconds or 0), reason, key))

    def expired(self) -> list[dict]:
        """Report only. No automatic lease takeover or side-effect re-execution."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM pipeline_jobs WHERE expires IS NOT NULL AND expires<=?",
            (self.clock(),))]
