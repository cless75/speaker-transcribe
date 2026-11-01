"""Explicit Hub/NAS pilot supervisor with independently running stage consumers.

Prepare and E2E are operator actions. Serving requires a same-identity E2E
attestation and old intake disabled; no auto-attest, deletion or GPU takeover.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time

from hub_intake import Drive
from hub_nas_intake import HubNasIntake
from pipeline_budget import DiskBudget
from pipeline_delivery import NasArea
from pipeline_hub_adapter import HubPipeline
from pipeline_hub_claims import HubClaims
from pipeline_process import gpu_idle
from pipeline_queue import PipelineQueue
from pipeline_runtime import StageRunner
from pipeline_storage import EvidenceError, ReceiptStore, atomic_json, canonical, digest, file_lock, no_links, required_destinations


def old_intake_stopped(*, serving=False):
    if os.name != "posix":
        raise RuntimeError("pilot_service_requires_linux_current")
    user = getpass.getuser()
    for unit in (f"unified-intake@{user}.timer", f"unified-intake@{user}.service",
                 f"hub-worker@{user}.timer", f"hub-worker@{user}.service",
                 f"zoom-local-worker@{user}.timer", f"zoom-local-worker@{user}.service",
                 f"zoom-dispatch@{user}.timer", f"zoom-dispatch@{user}.service",
                 "speaker-transcribe-watch.timer", "speaker-transcribe-watch.service"):
        result = subprocess.run(["systemctl", "show", "--value", "-p", "ActiveState", unit],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        if result.returncode or result.stdout.strip() not in {"inactive", "failed"}:
            raise RuntimeError("old_consumer_active_or_unverified")
        if serving and unit.endswith(".timer"):
            loaded = subprocess.run(["systemctl", "show", "--value", "-p", "LoadState", unit],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            if loaded.returncode == 0 and loaded.stdout.strip() == "not-found":
                continue
            enabled = subprocess.run(["systemctl", "is-enabled", unit], stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True, timeout=10)
            if enabled.stdout.strip() not in {"disabled", "masked", "not-found"}:
                raise RuntimeError("old_consumer_timer_not_disabled")


def implementation():
    root = pathlib.Path(__file__).resolve().parent
    files = {p.name: digest(p) for p in sorted(root.glob("*.py"))}
    for name in ("hub-pipeline@.service", "hub-pipeline@.timer"):
        unit = root.parent / "scripts/systemd" / name
        if unit.exists():
            files[name] = digest(unit)
    return hashlib.sha256(canonical(files)).hexdigest()


def normalized_node(config):
    original = pathlib.Path(config["node_config"]).resolve(strict=True)
    node = copy.deepcopy(json.loads(original.read_bytes()))
    node["_config_dir"] = str(original.parent)
    node["transcribe_cli"] = str(pathlib.Path(__file__).resolve().parent / "media_transcribe_cli.py")
    node["cleanup_intermediates"] = False
    node["on_asr_done"] = "leave"
    node.pop("session_artifact_dir", None)  # worker session artifacts stay in its explicit LOCAL output dir
    routing = node.get("routing") or {}
    if routing.get("mapper") and not pathlib.Path(routing["mapper"]).is_absolute():
        routing["mapper"] = str((original.parent / routing["mapper"]).resolve())
    return node


def prepare(config):
    root = no_links(pathlib.Path(config["state_root"]))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = normalized_node(config)
    path = root / "node-snapshot.json"
    if path.exists() and json.loads(path.read_bytes()) != node:
        raise EvidenceError("profile_snapshot_drift_requires_new_release")
    atomic_json(path, node)
    return root


class Pilot:
    def __init__(self, config):
        if config.get("schema") != 1:
            raise ValueError("unsupported_pilot_config")
        self.config = config
        self.root = no_links(pathlib.Path(config["state_root"]))
        node_path = self.root / "node-snapshot.json"
        self.node = json.loads(node_path.read_bytes())
        if self.node != normalized_node(config):
            raise EvidenceError("node_profile_drift")
        self.profile = hashlib.sha256(canonical(self.node)).hexdigest()
        python = self.node.get("transcribe_python") or sys.executable
        packages = subprocess.run([python, "-c", "import sys,json,importlib.metadata as m; "
                                   "print(json.dumps([sys.version,sorted((str(d.metadata.get('Name') or ''),d.version) "
                                   "for d in m.distributions())]))"], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=60, check=True).stdout
        self.engine = hashlib.sha256(canonical({"code": implementation(),
                                                "python": digest(pathlib.Path(python).resolve()),
                                                "packages": hashlib.sha256(packages).hexdigest()})).hexdigest()
        self.store = ReceiptStore(self.root)
        nas = config["nas"]
        self.nas = NasArea(pathlib.Path(nas["root"]), mountpoint=pathlib.Path(nas["mountpoint"]),
                           source=nas["source"], collection_id=nas["collection_id"], marker=nas["marker"],
                           minimum_free=config["nas_minimum_free"])
        def drive():
            mount = config.get("mount_path")
            if mount:
                from hub_intake import MountDrive
                return MountDrive(mount, timeout=config.get("drive_timeout", 60))
            return Drive(config["remote"], config.get("rclone_config"), timeout=config.get("drive_timeout", 60))
        self.claims = HubClaims(drive(), self.root / "claims",
                                host=str((self.node.get("node") or {}).get("host_label", "node")) + "-PIPELINE",
                                staleness_seconds=300)
        self.intake = HubNasIntake(drive(), self.nas, projects=list(config["source_paths"]),
                                   source_paths=config["source_paths"], node_config=self.node,
                                   project_delivery=config.get("project_delivery"),
                                   legacy_worker_root=pathlib.Path(config["legacy_worker_root"]),
                                   scan_cache=self.root / "scan-cache", profile_digest=self.profile,
                                   engine_digest=self.engine)
        self.pipeline = HubPipeline(self.intake, self.store, self.claims, node_config=self.node,
                                    config_path=node_path, maximum_job=config["maximum_job"])
        self.db = self.root / "queue.sqlite3"
        self.stop, self.failed = threading.Event(), []
        self.last_scan = None
        self.last_scan_error = None

    def identity(self):
        policy = {k: v for k, v in self.config.items() if k not in {"enabled", "e2e_source"}}
        return {"schema": 1, "queue_schema": 1, "engine": self.engine, "profile": self.profile,
                "policy_sha256": hashlib.sha256(canonical(policy)).hexdigest()}

    def budget(self):
        # Isolated supervisors must still reserve against the same local volume
        # ledger. DiskBudget validates its persisted limits/volume fail-closed.
        path = pathlib.Path(self.config.get("budget_path", self.root / "budget.sqlite3"))
        volume = pathlib.Path(self.config.get("budget_volume", self.root))
        return DiskBudget(path, capacity=self.config["buffer_capacity"],
                          minimum_free=self.config["local_minimum_free"], volume=volume,
                          maximum_job=self.config["maximum_job"])

    def private_error(self):
        import traceback
        fd = os.open(self.root / "private-supervisor.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            traceback.print_exc(file=out)

    def runner(self, stage, *, key=None):
        budget = self.budget()
        runner = StageRunner(self.db, self.store, gpu_lock=pathlib.Path(self.config["gpu_lock"]),
                             handlers={"discovered": self.pipeline.stage, "ready": self.pipeline.asr,
                                       "publish_pending": self.pipeline.delivery}, owner=f"pilot-{stage}",
                             budget=budget, reserve_bytes=lambda _: self.config["maximum_job"],
                             max_attempts=12, job_key=key, gpu_recovery_check=self.gpu_release_proof)
        return runner, budget

    def gpu_release_proof(self, job):
        path = self.store.directory(job["id"]) / "asr-process.json"
        if not path.exists():
            return False
        record = json.loads(path.read_bytes())
        if record.get("finished") is not True or type(record.get("pid")) is not int:
            return False
        try:
            os.kill(record["pid"], 0)
        except ProcessLookupError:
            gpu_idle()
            return True
        except PermissionError:
            return False
        return False

    def reconcile_completed(self, queue):
        for job in queue.expired():
            if self.store.read(job, job["resume_stage"]) is None:
                continue
            runner = budget = None
            try:
                runner, budget = self.runner(job["resume_stage"])
                runner.reconcile(job["id"])
            except (EvidenceError, ValueError, OSError, RuntimeError):
                # Remain visibly inflight/recovery-required; never retry a stage
                # merely because a receipt was absent or resource proof failed.
                pass
            finally:
                if runner:
                    runner.close()
                if budget:
                    budget.close()

    def status(self, queue):
        jobs = [dict(row) for row in queue.db.execute(
            "SELECT id,state,reason,attempts,eligible,expires FROM pipeline_jobs ORDER BY created")]
        counts = {}
        for job in jobs:
            counts[job["state"]] = counts.get(job["state"], 0) + 1
        return {"updated_at": time.time(), "status": "failed" if self.failed else
                "blocked" if counts.get("held") or queue.inflight("ready") and
                any(j["expires"] <= time.time() for j in queue.inflight("ready")) else "running",
                "jobs": jobs, "counts": counts, "last_scan": self.last_scan,
                "scan_error": self.last_scan_error, "component_errors": self.failed}

    def require_attestation(self):
        value = json.loads((self.root / "release-attestation.json").read_bytes())
        if value.get("identity") != self.identity():
            raise EvidenceError("component_identity_changed_repeat_e2e")
        queue = PipelineQueue(self.db)
        try:
            job = queue.get(value.get("job_id", ""))
            if job is None or job["state"] != "done":
                raise EvidenceError("attested_e2e_job_missing_or_unfinished")
            self.pipeline.source(job)
            if (self.store.read(job, "publish_pending") is None or
                    digest(self.store.path(job["id"], "publish_pending")) != value.get("delivery_receipt_sha256")):
                raise EvidenceError("attested_e2e_receipt_changed")
        finally:
            queue.close()
        return value

    def e2e_target(self):
        target = self.config.get("e2e_source")
        if (not isinstance(target, dict) or
                any(not isinstance(target.get(k), str) or not target[k] for k in ("project", "path"))):
            raise EvidenceError("fresh_e2e_source_required")
        return target

    def admit_e2e(self, queue):
        target = self.e2e_target()
        # A code/policy update must never turn a completed source into a new
        # E2E ASR. Keep its original receipts and select a fresh source instead.
        state = self.intake.drive.state(target["project"], target["path"])
        if state.get("status") == "asr-done":
            raise EvidenceError("completed_source_forbidden_for_e2e")
        key = self.intake.admit_exact(queue, target["project"], target["path"])
        _, selected = self.pipeline.source(queue.get(key))
        for field, actual in (("expected_id", selected.file_id), ("expected_md5", selected.md5),
                              ("expected_size", selected.size)):
            if field in target and target[field] != actual:
                raise EvidenceError("selected_e2e_source_changed")
        return key

    def attest(self, key):
        queue = PipelineQueue(self.db)
        try:
            job = queue.get(key)
            if job is None or job["state"] != "done":
                raise EvidenceError("completed_e2e_required")
            payload, item = self.pipeline.source(job)
            expected = self.e2e_target()
            if (item.project, item.path) != (expected["project"], expected["path"]):
                raise EvidenceError("wrong_e2e_source")
            receipt = self.store.read(job, "publish_pending")
            if receipt is None:
                raise EvidenceError("missing_e2e_delivery_receipt")
            for name in required_destinations(job):
                sink = self.pipeline.delivery.sinks[name]
                if sink.verify(job, receipt["metadata"]["deliveries"][name]["evidence"], lambda: None) is not True:
                    raise EvidenceError("e2e_delivery_changed")
            from hub_worker import source_unchanged
            source_unchanged(self.intake.drive, item)
            final = json.loads((self.store.directory(key) / "final-state.json").read_bytes())
            if self.pipeline._state(job) != final:
                raise EvidenceError("e2e_source_state_changed")
            value = {"identity": self.identity(), "job_id": key, "attested_at": time.time(),
                     "delivery_receipt_sha256": digest(self.store.path(key, "publish_pending"))}
            atomic_json(self.root / "release-attestation.json", value)
            return value
        finally:
            queue.close()

    def run(self, *, e2e=False):
        old_intake_stopped(serving=not e2e)
        local = subprocess.run(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(self.root)],
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True)
        if local.stdout.strip() not in {"ext4", "xfs", "btrfs", "zfs", "f2fs"}:
            raise EvidenceError("durable_local_state_filesystem_required")
        if self.root.is_relative_to(pathlib.Path(self.node["hub_root"]).resolve()) or self.root.is_relative_to(self.nas.root):
            raise EvidenceError("pipeline_state_overlaps_source_mount")
        # Each NAS stage guards its own mount. A NAS outage must not block
        # independent Hub-only jobs at supervisor startup.
        if not e2e:
            if self.config.get("enabled") is not True:
                raise EvidenceError("pilot_disabled")
            self.require_attestation()
        # The GPU must also be free of orphaned processes from the old scheduler.
        with file_lock(pathlib.Path(self.config["gpu_lock"])):
            gpu_idle()
        queue = PipelineQueue(self.db)
        initial_budget = self.budget()
        initial_budget.close()
        key = None
        threads = []
        try:
            if e2e:
                key = self.admit_e2e(queue)
            self.reconcile_completed(queue)
            # Restore only persisted ownership of already-ready stages. This does
            # not re-execute or automatically reclaim expired ASR attempts.
            for row in queue.db.execute("SELECT * FROM pipeline_jobs WHERE state IN ('ready','publish_pending','retry_wait')"):
                job = dict(row)
                if key is not None and job["id"] != key:
                    continue
                if (self.root / "claims" / (job["id"] + ".json")).exists():
                    _, item = self.pipeline.source(job)
                    self.claims.ensure(job, item)
            self.claims.start()

            def consume(stage):
                runner = budget = None
                try:
                    runner, budget = self.runner(stage, key=key)
                    runner.serve(stage, self.stop, poll_seconds=1)
                except Exception as exc:
                    self.private_error()
                    self.failed.append({"component": stage, "error": type(exc).__name__})
                    self.stop.set()
                finally:
                    if runner:
                        runner.close()
                    if budget:
                        budget.close()

            def scan():
                local = PipelineQueue(self.db)
                try:
                    while not self.stop.is_set():
                        try:
                            self.last_scan = {"finished_at": time.time(), **self.intake.discover(local)}
                            self.last_scan_error = None
                        except (OSError, RuntimeError, ValueError) as exc:
                            self.private_error()
                            self.last_scan_error = type(exc).__name__
                        self.stop.wait(self.config.get("scan_seconds", 300))
                finally:
                    local.close()

            for stage in ("discovered", "ready", "publish_pending"):
                threads.append(threading.Thread(target=consume, args=(stage,), name=stage))
            if not e2e:
                threads.append(threading.Thread(target=scan, name="scanner"))
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + self.config.get("e2e_timeout_seconds", 7200)
            while not self.stop.wait(2):
                atomic_json(self.root / "health.json", self.status(queue))
                if e2e:
                    state = queue.get(key)["state"]
                    if state in {"done", "held"}:
                        self.stop.set()
                    elif time.monotonic() >= deadline:
                        # Drain an owned active child; never kill someone else's
                        # GPU process just to satisfy an operator time limit.
                        self.failed.append({"component": "e2e", "error": "deadline"})
                        self.stop.set()
            for thread in threads:
                thread.join()
            report = self.status(queue)
            report["status"] = "failed" if self.failed else "stopped"
            atomic_json(self.root / "health.json", report)
            if self.failed or e2e and queue.get(key)["state"] != "done":
                raise RuntimeError("pilot_did_not_complete_see_private_health")
            return {"job_id": key, "state": queue.get(key)["state"]} if e2e else {"status": "stopped"}
        finally:
            self.stop.set()
            for thread in threads:
                if thread.is_alive():
                    thread.join()
            self.claims.close()
            queue.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--e2e", action="store_true")
    action.add_argument("--serve", action="store_true")
    action.add_argument("--attest", metavar="JOB_ID")
    args = parser.parse_args()
    config = json.loads(args.config.read_bytes())
    if args.prepare:
        prepare(config)
        print(json.dumps({"prepared": True}))
        return 0
    root = no_links(pathlib.Path(config["state_root"]))
    with file_lock(root / "supervisor.lock"):
        pilot = None
        try:
            pilot = Pilot(config)
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *args: pilot.stop.set())
            report = pilot.attest(args.attest) if args.attest else pilot.run(e2e=args.e2e)
            print(json.dumps(report))
            return 0
        except Exception as exc:
            if pilot:
                pilot.private_error()
            reason = str(exc) if re.fullmatch(r"[a-z][a-z0-9_]{0,99}", str(exc)) else type(exc).__name__
            atomic_json(root / "health.json", {"updated_at": time.time(), "status": "blocked",
                                               "error": type(exc).__name__, "reason": reason})
            print(json.dumps({"status": "blocked", "error": type(exc).__name__, "reason": reason}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
