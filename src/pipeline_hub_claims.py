"""Long-lived Hub ownership between independent stages, with verified heartbeats.

Uses the existing .claim.json format plus a persisted pipeline token. Never
preempts another owner's expired claim. The enclosing supervisor owns a local
singleton lock; source claims remain held until verified dual publication.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import pathlib
import threading
import time
import uuid
from dataclasses import dataclass, field

import hub_worker as hub
from pipeline_runtime import HoldJob, RetryJob
from pipeline_storage import EvidenceError, atomic_json, no_links, sync_directory


@dataclass
class _Owned:
    item: object
    path: pathlib.Path
    token: str
    remote_id: str | None = None
    verified: float = 0
    failed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class HubClaims:
    def __init__(self, drive, root: pathlib.Path, *, host: str, lease_seconds=1800,
                  heartbeat_seconds=30, convergence_seconds=8, clock=time.monotonic,
                  staleness_seconds=None):
        if (not host or lease_seconds < 60 or not 0 < heartbeat_seconds < lease_seconds / 3
                or convergence_seconds < 0):
            raise ValueError("invalid_claim_policy")
        self.drive, self.root, self.host, self.clock = drive, no_links(root), host, clock
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        sync_directory(self.root.parent)
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self.convergence_seconds = convergence_seconds
        self.staleness_seconds = 120 if staleness_seconds is None else staleness_seconds
        if self.staleness_seconds <= 0:
            raise ValueError("invalid_staleness")
        self.active = {}
        self._active_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    @staticmethod
    def _relative(item):
        return item.path + ".claim.json"

    def _remote(self, item):
        path = pathlib.PurePosixPath(self._relative(item))
        parent = () if str(path.parent) == "." else path.parent.parts
        found = [e for e in self.drive.list(item.project, *parent)
                 if not e.get("IsDir") and e.get("Name") == path.name]
        if len(found) > 1:
            raise EvidenceError("ambiguous_hub_claim_path")
        if not found:
            return {}, None
        if not found[0].get("ID"):
            raise EvidenceError("hub_claim_id_missing")
        raw = self.drive.call("cat", self.drive.path(item.project, self._relative(item)))
        if len(raw) > 65536:
            raise EvidenceError("oversized_hub_claim")
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("claim"), dict):
            raise EvidenceError("invalid_hub_claim")
        return value, found[0]["ID"]

    def _write(self, owned, *, phase="pipeline"):
        now = dt.datetime.now(dt.timezone.utc)
        stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
        claim = {"claimed_by": self.host, "claimed_at": stamp(now),
                 "heartbeat_at": stamp(now), "lease_until": stamp(now + dt.timedelta(seconds=self.lease_seconds)),
                 "claim_phase": phase, "pipeline_token": owned.token,
                 "source_id": owned.item.file_id, "source_revision": owned.item.revision,
                 "preempt_count": 0}
        outbound = owned.path.with_name(owned.path.stem + "-outbound.json")
        atomic_json(outbound, {"claim": claim, "audio": pathlib.PurePosixPath(owned.item.path).name})
        self.drive.call("copyto", str(outbound),
                        self.drive.path(owned.item.project, self._relative(owned.item)))
        value, remote_id = {}, None
        for delay in (0, 1, 2):
            if delay:
                time.sleep(delay)
            value, remote_id = self._remote(owned.item)
            if value:
                break
        if not value:
            raise RetryJob("hub_claim_not_yet_visible")
        if (value.get("claim", {}).get("claimed_by") != self.host or
                value["claim"].get("pipeline_token") != owned.token or
                (owned.remote_id is not None and remote_id != owned.remote_id)):
            raise HoldJob("hub_claim_lost_or_id_changed")
        owned.remote_id = remote_id
        atomic_json(owned.path, {"schema": 1, "host": self.host, "token": owned.token,
                                 "source_id": owned.item.file_id, "revision": owned.item.revision,
                                 "remote_id": remote_id})
        owned.verified = self.clock()

    def ensure(self, job, item):
        key = job["id"]
        with self._active_lock:
            already = key in self.active
        if already:
            self.check(job)
            return
        path = no_links(self.root / f"{key}.json")
        saved = json.loads(path.read_bytes()) if path.exists() else None
        if saved and (saved.get("host"), saved.get("source_id"), saved.get("revision")) != (
                self.host, item.file_id, item.revision):
            raise EvidenceError("local_claim_identity_conflict")
        owned = _Owned(item, path, saved["token"] if saved else uuid.uuid4().hex,
                       saved.get("remote_id") if saved else None)
        value, remote_id = self._remote(item)
        existing = value.get("claim", {})
        if remote_id is not None:
            mine = (saved is not None and existing.get("claimed_by") == self.host and
                    existing.get("pipeline_token") == owned.token and
                    (owned.remote_id is None or remote_id == owned.remote_id) and
                    existing.get("source_id") == item.file_id and
                    existing.get("source_revision") == item.revision)
            if not mine and existing.get("claim_phase") != "done":
                # Expiry is not proof that another node's ASR has stopped.
                raise HoldJob("hub_claim_owned_or_requires_reconciliation")
            if not mine:
                owned.remote_id = remote_id
            elif owned.remote_id is None:
                owned.remote_id = remote_id
        elif saved and owned.remote_id:
            raise HoldJob("persisted_hub_claim_disappeared")
        # Persist token before first remote write so an ambiguous network failure
        # can be reconciled on restart without blindly replacing our own claim.
        atomic_json(path, {"schema": 1, "host": self.host, "token": owned.token,
                           "source_id": item.file_id, "revision": item.revision,
                           "remote_id": owned.remote_id})
        self._write(owned)
        if self.convergence_seconds:
            time.sleep(self.convergence_seconds)
        value, remote_id = self._remote(item)
        if value.get("claim", {}).get("pipeline_token") != owned.token or remote_id != owned.remote_id:
            raise HoldJob("hub_claim_convergence_lost")
        owned.verified = self.clock()
        with self._active_lock:
            self.active[key] = owned

    def check(self, job):
        with self._active_lock:
            owned = self.active.get(job["id"])
        if owned is None or owned.failed:
            raise HoldJob("hub_ownership_unverified")
        # No network calls in the ASR consumer's fast path. A stalled heartbeat
        # expires this local proof well before the remote lease expires.
        if self.clock() - owned.verified > self.staleness_seconds:
            raise RetryJob("hub_ownership_heartbeat_stale")

    def abandon(self, job):
        """Stop renewing a claim (e.g. after a held/retried stage)."""
        with self._active_lock:
            owned = self.active.pop(job["id"], None)
        if owned is not None:
            owned.failed = True

    def _refresh_one(self, owned):
        with owned.lock:
            if owned.failed:
                return
            try:
                value, remote_id = self._remote(owned.item)
                if (remote_id != owned.remote_id or value.get("claim", {}).get("claimed_by") != self.host or
                        value.get("claim", {}).get("pipeline_token") != owned.token):
                    owned.failed = True
                    return
                # If a claim has been stale for too long, stop renewing it so the
                # next attempt re-establishes ownership instead of spamming Drive.
                if self.clock() - owned.verified > 2 * self.staleness_seconds:
                    owned.failed = True
                    return
                self._write(owned)
            except (EvidenceError, HoldJob, ValueError):
                owned.failed = True
            except (OSError, RuntimeError):
                # Retain last verified time; check() becomes stale if this
                # persists. Never manufacture a fresh ownership proof.
                pass

    def refresh(self):
        with self._active_lock:
            owned_list = list(self.active.values())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self._refresh_one, o) for o in owned_list]
            for f in concurrent.futures.as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass
        # Drop abandoned claims from the active set.
        with self._active_lock:
            for key in [k for k, v in self.active.items() if v.failed]:
                del self.active[key]

    def start(self):
        if self.thread is not None:
            raise RuntimeError("claim_heartbeat_already_started")
        def loop():
            while not self.stop.wait(self.heartbeat_seconds):
                self.refresh()
        self.thread = threading.Thread(target=loop, name="hub-claim-heartbeats", daemon=True)
        self.thread.start()

    def retire(self, job):
        self.check(job)
        owned = self.active[job["id"]]
        with owned.lock:
            value, remote_id = self._remote(owned.item)
            if value.get("claim", {}).get("pipeline_token") != owned.token or remote_id != owned.remote_id:
                raise HoldJob("hub_claim_changed_before_retire")
            self._write(owned, phase="done")
            owned.failed = True  # a heartbeat snapshot must not renew retired work
            with self._active_lock:
                self.active.pop(job["id"], None)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)