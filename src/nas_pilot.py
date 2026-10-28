"""Bounded NAS admission-package pilot. Never enables services or scans a corpus.

The immutable owner package is the sole discovery input; only its single include
may enter this isolated queue. Shares the live node's GPU lock and byte ledger.
No claim theft, automatic ASR retry, VTT import, enrollment or source cleanup.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import traceback
import uuid
from types import SimpleNamespace

import hub_worker as hub
from hub_intake import Drive, DriveError
from hub_pipeline import implementation, old_intake_stopped
from pipeline_budget import DiskBudget
from pipeline_delivery import DualDelivery, NasArea, verify_mount
from pipeline_hub_adapter import HubPipeline, duration_seconds
from pipeline_process import gpu_idle
from pipeline_queue import LeaseLost, PipelineQueue
from pipeline_runtime import HoldJob, RetryJob, StageRunner
from pipeline_storage import (EvidenceError, ReceiptStore, atomic_json, canonical,
                              digest, file_lock, no_links, safe_path, sync_directory)


POLICY = {"speakers": "unknown-or-recording-local-labels-only",
          "required_effective_voiceprint_mode": "off", "enrollment": False,
          "active_matching": False, "global_promotion": False,
          "external_vtt": False, "proposed_glossary": False,
          "named_speaker_attribution_approved": False}


def read_json(path, limit=4 * 1024 * 1024):
    path = no_links(path)
    if path.stat().st_size > limit:
        raise EvidenceError("oversized_pilot_document")
    return json.loads(path.read_bytes())


class Admission:
    def __init__(self, config, *, mount_check=verify_mount):
        self.config, self.mount_check = config, mount_check
        self.root = no_links(pathlib.Path(config["corpus_root"]))
        self.mount = no_links(pathlib.Path(config["mountpoint"]))
        if not self.root.is_relative_to(self.mount) or self.root == self.mount:
            raise EvidenceError("dedicated_corpus_root_required")
        self.guard()
        self.package = no_links(self.root / str(safe_path(config["package_relative"])))
        manifest = self.package / "manifest.json"
        self.manifest_hash = digest(manifest, strict_snapshot=False)
        if self.manifest_hash != config["manifest_sha256"]:
            raise EvidenceError("admission_manifest_changed")
        data = read_json(manifest)
        if data.get("version") != "0.0.1" or str(data.get("project")) != str(config["project"]):
            raise EvidenceError("unsupported_admission_manifest")
        names = set()
        for row in data["files"]:
            name = str(safe_path(row["path"]))
            if name in names:
                raise EvidenceError("duplicate_admission_member")
            names.add(name)
            path = no_links(self.package / name)
            if path.stat().st_size != row["bytes"] or digest(path, strict_snapshot=False) != row["sha256"]:
                raise EvidenceError("admission_member_changed")
        if not {"whitelist.json", "speaker-policy.json", "source-validation.json"} <= names:
            raise EvidenceError("missing_admission_members")
        whitelist = read_json(self.package / "whitelist.json")
        speaker = read_json(self.package / "speaker-policy.json")
        if (str(whitelist.get("project")) != str(config["project"]) or
                whitelist.get("recursive_scan_allowed") is not False or
                len(whitelist.get("include", [])) != 1 or
                whitelist.get("speaker_policy") != POLICY or speaker.get("policy") != POLICY or
                speaker.get("enrollment_admitted") is not False or
                whitelist.get("transcripts_admitted") != [] or whitelist.get("offsets_applied") != []):
            raise EvidenceError("single_unknown_pilot_policy_required")
        self.entry = whitelist["include"][0]
        e = self.entry
        if (str(e.get("project")) != str(config["project"]) or
                e.get("admission") != "admitted" or
                e.get("admission_scope") != "single-initial-pilot-only" or
                e.get("speaker_policy") != POLICY or
                not isinstance(e.get("source_sha256"), str) or
                not re.fullmatch(r"[a-f0-9]{64}", e["source_sha256"]) or
                type(e.get("bytes")) is not int or e["bytes"] <= 0 or
                not isinstance(e.get("duration_sec"), (int, float)) or
                not math.isfinite(e["duration_sec"]) or e["duration_sec"] <= 0):
            raise EvidenceError("invalid_pilot_source_admission")
        safe_path(e["source_id"])
        if "/" in e["source_id"]:
            raise EvidenceError("invalid_pilot_source_id")
        self.source = no_links(self.root / str(safe_path(e["source_path"])))
        if self.source.suffix.lower() not in {".mp4", ".webm", ".m4a", ".wav", ".mp3"}:
            raise EvidenceError("unsupported_pilot_media")
        if any(e["source_path"] == x.get("path") for x in whitelist.get("exclude", [])):
            raise EvidenceError("included_source_excluded")
        timeline = e.get("timeline", {})
        if (timeline.get("basis") != "native-source-media" or timeline.get("trim") is not False or
                timeline.get("speed_change") is not False or timeline.get("offset_applied_sec") != 0 or
                timeline.get("external_transcript_bindings") != []):
            raise EvidenceError("native_timeline_required")
        validation = read_json(self.package / "source-validation.json")
        if (validation.get("source_sha256") != e["source_sha256"] or
                validation.get("source") != e["source_path"] or
                validation.get("source_matches_archive_hash") is not True or
                validation.get("full_audio_video_decode_exit_code") != 0 or
                validation.get("status") != "passed-technical-source-validation"):
            raise EvidenceError("source_validation_required")
        self.whitelist = whitelist

    def guard(self):
        self.mount_check(self.mount, self.config["mount_source"])

    def verify(self, check=lambda: None):
        self.guard()
        check()
        if digest(self.package / "manifest.json", strict_snapshot=False) != self.manifest_hash:
            raise EvidenceError("admission_manifest_changed")
        if (self.source.stat().st_size != self.entry["bytes"] or
                digest(self.source, strict_snapshot=False) != self.entry["source_sha256"]):
            raise EvidenceError("admitted_source_changed")
        check()
        self.guard()

    def report(self):
        self.verify()
        return {"manifest_sha256": self.manifest_hash, "project": self.config["project"],
                "include_count": 1, "exclude_count": len(self.whitelist.get("exclude", [])),
                "source_id": self.entry["source_id"], "source_sha256": self.entry["source_sha256"],
                "speaker_policy": POLICY, "input_ack": "acknowledged", "execution_started": False}


def pilot_profile(node):
    cfg = copy.deepcopy(node)
    # Full config is passed to the worker: explicit off must survive CLI defaults.
    cfg.update(voiceprint_mode="off", speaker_mode="diarize", execution_mode="full",
               generate_speaker_clips=False, zoom_vtt_autodetect=False,
               ktalk_sidecar_patterns=[], glossary_override="off", cleanup_intermediates=False,
               on_asr_done="leave", speaker_map={}, _project_settings_cache={})
    for key in ("zoom_vtt_path", "ktalk_txt_path", "speaker_map_path", "session_artifact_dir",
                "project_speaker_registry_path", "profile_store_path", "global_registry_dir",
                "machine_local_voiceprint_store_path", "initial_prompt", "hotwords", "glossary_path"):
        cfg.pop(key, None)
    return cfg


class SourceClaim:
    """Persistent NAS token, never expired/stolen. Local token precedes remote write."""
    def __init__(self, admission, store, area):
        self.a, self.store, self.area = admission, store, area
        self.local = store.root / "nas-source-ownership.json"
        self.remote = area.root / "source-claim.json"

    def ensure(self):
        self.area.guard()
        if self.local.exists():
            value = read_json(self.local)
        else:
            value = {"source_id": self.a.entry["source_id"],
                     "source_sha256": self.a.entry["source_sha256"],
                     "token": uuid.uuid4().hex, "host": socket.gethostname(),
                     "state_root": str(self.store.root)}
            atomic_json(self.local, value)
        if (value.get("source_id"), value.get("source_sha256")) != (
                self.a.entry["source_id"], self.a.entry["source_sha256"]):
            raise EvidenceError("local_source_claim_changed")
        try:
            with self.remote.open("xb") as stream:
                stream.write(canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(self.area.root)
        except FileExistsError:
            pass
        self.check(None)

    def check(self, job):
        self.area.guard()
        if not self.local.exists() or read_json(self.remote) != read_json(self.local):
            raise HoldJob("nas_source_owned_by_other_or_changed")


class NasPipeline(HubPipeline):
    """Uses the same receipted engine/snapshot path, not a second ASR implementation."""
    def __init__(self, admission, store, area, drive, *, node_config, config_path,
                 profile_digest, engine_digest, executor=None, probe=duration_seconds,
                 gpu_check=gpu_idle):
        self.a, self.store, self.area, self.drive = admission, store, area, drive
        self.cfg, self.config_path = pilot_profile(node_config), config_path
        self.profile_digest, self.engine_digest = profile_digest, engine_digest
        self.executor, self.probe, self.gpu_check = executor, probe, gpu_check
        self.claims = SourceClaim(admission, store, area)
        self.maximum_job = admission.config["maximum_job"]
        self.delivery = DualDelivery(store, hub=HubSink(self), nas=NasSink(self), finalize=self.finalize)

    def payload(self):
        return {"adapter": "nas-admitted-pilot-v1", "project": str(self.a.config["project"]),
                "source": self.a.entry, "manifest_sha256": self.a.manifest_hash,
                "primary_name": self.a.source.name}

    def discover(self, queue):
        self.a.verify()
        state_path = self.area.root / "source-state.json"
        state = read_json(state_path) if state_path.exists() else {}
        if state.get("status") == "asr-done":
            if state.get("source_sha256") != self.a.entry["source_sha256"]:
                raise HoldJob("source_has_other_completed_revision")
            return {"candidates": 0, "reused_completed": 1, "job_id": state["pipeline_job_id"]}
        expected_identity = ["nas:" + str(self.a.config["project"]) + ":" + self.a.entry["source_id"],
                             self.a.entry["source_sha256"], self.profile_digest, self.engine_digest]
        for row in queue.db.execute("SELECT identity,payload FROM pipeline_jobs"):
            previous = json.loads(row["payload"])
            if (previous.get("source", {}).get("source_id") == self.a.entry["source_id"] and
                    json.loads(row["identity"]) != expected_identity):
                raise HoldJob("source_has_other_processing_identity_requires_reconciliation")
        # The entire include is the admitted single source. Never recursive scanning.
        key = queue.enqueue("nas:" + str(self.a.config["project"]) + ":" + self.a.entry["source_id"],
                            self.a.entry["source_sha256"], self.profile_digest, self.engine_digest, self.payload())
        return {"candidates": 1, "reused_completed": 0, "job_id": key}

    def source(self, job):
        if json.loads(job["payload"]) != self.payload() or json.loads(job["identity"]) != [
                "nas:" + str(self.a.config["project"]) + ":" + self.a.entry["source_id"],
                self.a.entry["source_sha256"], self.profile_digest, self.engine_digest]:
            raise EvidenceError("nas_pilot_processing_identity_mismatch")
        return self.payload(), SimpleNamespace(project=str(self.a.config["project"]),
                                               path=self.a.entry["source_path"],
                                               size=self.a.entry["bytes"])

    def route(self, job):
        self.source(job)
        sid = "NAS-" + job["id"]
        return {"session_id": sid, "target": str(self.a.config["project"]) + "/sessions/nas/" + sid}

    def public_source(self, job):
        self.source(job)
        return pathlib.Path(self.a.entry["source_unc"].replace("\\", "/"))

    def result_review(self, job, metadata):
        return "needs_review"  # all initial pilot outputs require shared acceptance

    def worker_config(self, job, directory):
        self.source(job)
        cfg = pilot_profile(self.cfg)
        cfg["work_root"] = str(directory / "work" / "engine")
        cfg["session_artifact_dir"] = str(directory / "work" / "transcripts")
        path = directory / "worker-snapshot.json"
        if path.exists() and read_json(path) != cfg:
            raise EvidenceError("pilot_worker_snapshot_changed")
        atomic_json(path, cfg)
        return cfg, path

    def validate_engine_result(self, job, metadata, output):
        vp = metadata.get("voiceprint") or {}
        diar = metadata.get("diarization") or {}
        if (vp.get("mode") != "off" or vp.get("enabled") is not False or
                metadata.get("profile_updates") or
                diar.get("source") in {"zoom_vtt", "ktalk_txt"}):
            raise HoldJob("unknown_no_enrollment_result_not_proven")
        for path in output.glob("*-run-meta.json"):
            record = read_json(path)
            actual = record.get("voiceprint") or {}
            if actual.get("mode") != "off" or actual.get("enabled") is not False:
                raise HoldJob("run_meta_no_enrollment_not_proven")

    def stage(self, job, directory, check):
        self.source(job)
        self.a.verify(check)
        self.claims.ensure()
        local = no_links(directory / "input" / self.a.source.name)
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists():
            if local.stat().st_size != self.a.entry["bytes"] or digest(local) != self.a.entry["source_sha256"]:
                raise HoldJob("local_nas_source_conflict")
        else:
            temp = local.parent / (".copy-" + uuid.uuid4().hex)
            try:
                with self.a.source.open("rb") as inp, temp.open("xb") as out:
                    for block in iter(lambda: inp.read(1024 * 1024), b""):
                        check()
                        self.claims.check(job)
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                if digest(temp) != self.a.entry["source_sha256"]:
                    raise EvidenceError("nas_local_copy_changed")
                os.replace(temp, local)
                sync_directory(local.parent)
            finally:
                temp.unlink(missing_ok=True)
        duration = self.probe(local)
        if abs(duration - self.a.entry["duration_sec"]) > 1:
            raise HoldJob("admitted_duration_changed")
        estimate = 2 * self.a.entry["bytes"] + math.ceil(duration * 256000) + math.ceil(duration / 60) * 8 * 1024**2 + 1024**3
        if estimate > self.maximum_job:
            raise HoldJob("media_expansion_exceeds_job_budget")
        return {"source_verified": True, "source_sha256": self.a.entry["source_sha256"],
                "manifest_sha256": self.a.manifest_hash, "duration_seconds": duration,
                "estimated_bytes": estimate, "speaker_policy": POLICY, **self.route(job)}

    def asr(self, job, directory, check):
        self.a.verify(check)
        self.claims.check(job)
        # A previous unknown invocation is never retried by mere file presence.
        if (directory / "asr-process.json").exists() and not (directory / "engine-completion.json").exists():
            raise HoldJob("prior_asr_attempt_requires_reconciliation")
        try:
            return super().asr(job, directory, check)
        except OSError as exc:
            raise HoldJob("nas_pilot_asr_attempt_requires_review") from exc

    def finalize(self, job, deliveries, check):
        if set(deliveries) != {"hub", "nas"} or any(v.get("verified") is not True for v in deliveries.values()):
            raise EvidenceError("both_verified_destinations_required")
        self.claims.check(job)
        self.a.verify(check)
        state_path = self.store.directory(job["id"]) / "final-state.json"
        ready = self.store.read(job, "ready")["metadata"]
        state = {"status": "asr-done", "project": str(self.a.config["project"]),
                 "pipeline_job_id": job["id"], "source_id": self.a.entry["source_id"],
                 "source_sha256": self.a.entry["source_sha256"],
                 "manifest_sha256": self.a.manifest_hash, "deliveries_verified": ["hub", "nas"],
                 "speaker_policy": POLICY, "quality": ready["quality"], "review": "needs_review",
                 "transcript_url": "https://drive.google.com/file/d/" + deliveries["hub"]["evidence"]["transcript_id"] + "/view"}
        if state_path.exists() and read_json(state_path) != state:
            raise EvidenceError("nas_final_state_changed")
        atomic_json(state_path, state)
        remote = self.drive.path(self.route(job)["target"], "pipeline/state.json")
        self.drive.call("copyto", str(state_path), remote)
        if json.loads(self.drive.call("cat", remote)) != state:
            raise HoldJob("hub_terminal_state_not_verified")
        check()
        self.claims.check(job)
        # Source-side terminal marker LAST; never changes the source/owner package.
        atomic_json(self.area.root / "source-state.json", state)
        if read_json(self.area.root / "source-state.json") != state:
            raise HoldJob("nas_terminal_state_not_verified")


class HubSink:
    def __init__(self, pipeline):
        self.p = pipeline

    def publish(self, job, result, check):
        try:
            return self._publish(job, result, check)
        except DriveError as exc:
            raise RetryJob("hub_delivery_unavailable") from exc
        except (RetryJob, LeaseLost):
            raise
        except RuntimeError as exc:
            raise HoldJob("hub_publication_unverified") from exc

    def _publish(self, job, result, check):
        check()
        self.p.claims.check(job)
        target = self.p.route(job)["target"]
        self.p.drive.call("copy", str(result), self.p.drive.path(target))
        published = hub.remote_verified(self.p.drive, target, result)
        transcript = self.p.store.read(job, "ready")["metadata"]["transcript"]
        identity = (published.get(transcript) or {}).get("ID")
        if not identity:
            raise HoldJob("published_transcript_id_missing")
        return {"target": target, "transcript_id": identity,
                "result_sha256": digest(self.p.store.path(job["id"], "ready"))}

    def verify(self, job, evidence, check):
        try:
            return self._verify(job, evidence, check)
        except DriveError as exc:
            raise RetryJob("hub_verification_unavailable") from exc
        except (RetryJob, LeaseLost):
            raise
        except RuntimeError as exc:
            raise HoldJob("hub_publication_integrity_unverified") from exc

    def _verify(self, job, evidence, check):
        check()
        if (evidence.get("target") != self.p.route(job)["target"] or
                evidence.get("result_sha256") != digest(self.p.store.path(job["id"], "ready"))):
            raise EvidenceError("hub_pilot_delivery_identity_changed")
        published = hub.remote_verified(self.p.drive, evidence["target"], self.p.store.directory(job["id"]) / "result")
        transcript = self.p.store.read(job, "ready")["metadata"]["transcript"]
        return (published.get(transcript) or {}).get("ID") == evidence["transcript_id"]


class NasSink:
    def __init__(self, pipeline):
        self.p = pipeline

    def arguments(self, job):
        files = [{**f, "path": f["path"].removeprefix("result/")}
                 for f in self.p.store.inventory(job["id"], "result")]
        binding = {**self.p.store.binding(job), "source_sha256": self.p.a.entry["source_sha256"],
                   "manifest_sha256": self.p.a.manifest_hash,
                   "result_receipt_sha256": digest(self.p.store.path(job["id"], "ready"))}
        return files, binding

    def publish(self, job, result, check):
        self.p.a.verify(check)
        self.p.claims.check(job)
        files, binding = self.arguments(job)
        return self.p.area.publish(job["id"], result, files, binding, check)

    def verify(self, job, evidence, check):
        if evidence.get("relative") != job["id"]:
            raise EvidenceError("nas_pilot_output_location_changed")
        self.p.a.verify(check)
        files, binding = self.arguments(job)
        return self.p.area.verify_publication(evidence, files, binding, check)


def make_area(admission, minimum_free):
    return NasArea(pathlib.Path(str(admission.source) + ".asr"),
                   mountpoint=admission.mount, source=admission.config["mount_source"],
                   collection_id="nas-pilot:" + admission.entry["source_id"] + ":" + admission.entry["source_sha256"],
                   marker=".speaker-transcribe-nas-pilot", minimum_free=minimum_free,
                   mount_check=admission.mount_check)


def prepare_area(area):
    area.mount_check(area.mountpoint, area.source)
    marker = area.root / area.marker
    if marker.exists():
        area.guard()
        return
    if area.root.exists() and any(area.root.iterdir()):
        raise HoldJob("existing_unowned_asr_directory_requires_reconciliation")
    area.root.mkdir(parents=True, exist_ok=True)
    atomic_json(marker, {"schema": 1, "collection_id": area.collection_id, "mount_source": area.source})
    area.guard()


def preflight(config, *, mount_check=verify_mount):
    admission = Admission(config, mount_check=mount_check)
    report = admission.report()
    # Do not misrepresent local free space as a NAS-reservation check.
    report["local_free"] = shutil.disk_usage(pathlib.Path(config["state_root"]).parent).free
    report["nas_free"] = shutil.disk_usage(admission.root).free
    if report["local_free"] < config["local_minimum_free"] + config["maximum_job"]:
        raise HoldJob("local_disk_pressure")
    if report["nas_free"] < config["nas_minimum_free"] + config["maximum_job"]:
        raise HoldJob("nas_disk_pressure")
    area = make_area(admission, config["nas_minimum_free"])
    state = area.root / "source-state.json"
    if state.exists():
        value = read_json(state)
        if value.get("status") == "asr-done":
            raise HoldJob("completed_nas_source_requires_reuse_not_asr")
    # Caller must pin a separate, inspected duplicate-check report. It is input
    # evidence, not authority for a broader corpus or an automatic new run.
    evidence_path = pathlib.Path(config["completion_check_path"])
    if digest(evidence_path) != config["completion_check_sha256"]:
        raise EvidenceError("completion_check_changed")
    evidence = read_json(evidence_path)
    if (evidence.get("source_sha256") != admission.entry["source_sha256"] or
            evidence.get("completed_asr_found") is not False or
            evidence.get("source_id") != admission.entry["source_id"]):
        raise HoldJob("prior_completion_not_resolved")
    report["completion_check_sha256"] = config["completion_check_sha256"]
    report["runtime_ready"] = False
    return admission, area, report


def run_pilot(config, stop=None):
    """Explicit bounded E2E, not --serve and not an attestation bypass."""
    if config.get("scope") != "single-approved-nas-pilot":
        raise EvidenceError("explicit_single_nas_pilot_required")
    old_intake_stopped()
    admission, area, report = preflight(config)
    root = no_links(pathlib.Path(config["state_root"]))
    if root.is_relative_to(admission.mount):
        raise EvidenceError("durable_local_state_required")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with file_lock(root / "supervisor.lock"):
        store = ReceiptStore(root)
        node = pilot_profile(read_json(pathlib.Path(config["node_config"])))
        node["transcribe_cli"] = str(pathlib.Path(__file__).resolve().parent / "media_transcribe_cli.py")
        node["_config_dir"] = str(pathlib.Path(config["node_config"]).parent)
        snapshot = root / "node-snapshot.json"
        if snapshot.exists() and read_json(snapshot) != node:
            raise EvidenceError("pilot_snapshot_drift")
        atomic_json(snapshot, node)
        profile = hashlib.sha256(canonical(node)).hexdigest()
        python = node.get("transcribe_python") or os.sys.executable
        packages = subprocess.run(
            [python, "-c", "import sys,json,importlib.metadata as m; print(json.dumps([sys.version,sorted((str(d.metadata.get('Name') or ''),d.version) for d in m.distributions())]))"],
            capture_output=True, timeout=60, check=True).stdout
        engine = hashlib.sha256(canonical({"implementation": implementation(),
                    "python": digest(pathlib.Path(python).resolve()),
                    "packages": hashlib.sha256(packages).hexdigest(),
                    "policy": config})).hexdigest()
        drive = Drive(config["remote"], config.get("rclone_config"), timeout=60)
        drive.list()  # Hub credentials/root must work before source admission.
        # Never broaden the live Hub source_paths to fake a NAS input.
        prepare_area(area)
        pipeline = NasPipeline(admission, store, area, drive, node_config=node,
                               config_path=snapshot, profile_digest=profile, engine_digest=engine)
        pipeline.claims.ensure()
        report.update(engine=engine, profile=profile, runtime_ready=True,
                      speaker_policy=POLICY, destinations=["hub", "nas"])
        atomic_json(root / "input-ack-and-preflight.json", report)
        db = root / "queue.sqlite3"
        queue = PipelineQueue(db)
        stop, failures = stop or threading.Event(), []
        threads = []
        try:
            selected = pipeline.discover(queue)
            key = selected["job_id"]
            if selected["reused_completed"]:
                return selected

            def consume(stage):
                budget = runner = None
                try:
                    # Same existing ledger/volume/config as live Hub consumers.
                    budget = DiskBudget(pathlib.Path(config["budget_path"]),
                        capacity=config["buffer_capacity"], minimum_free=config["local_minimum_free"],
                        maximum_job=config["maximum_job"], volume=pathlib.Path(config["budget_volume"]))
                    runner = StageRunner(db, store, gpu_lock=pathlib.Path(config["gpu_lock"]),
                        handlers={"discovered": pipeline.stage, "ready": pipeline.asr,
                                  "publish_pending": pipeline.delivery}, owner="nas-pilot-" + stage,
                        budget=budget, reserve_bytes=lambda _: config["maximum_job"], job_key=key,
                        max_attempts=12)
                    runner.serve(stage, stop, poll_seconds=1)
                except Exception as exc:
                    fd = os.open(root / "private-supervisor.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8") as log:
                        traceback.print_exc(file=log)
                    failures.append(type(exc).__name__)
                    stop.set()
                finally:
                    if runner:
                        runner.close()
                    if budget:
                        budget.close()

            for stage in ("discovered", "ready", "publish_pending"):
                threads.append(threading.Thread(target=consume, args=(stage,), name="nas-" + stage))
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + config.get("timeout_seconds", 7200)
            while not stop.wait(2):
                job = queue.get(key)
                atomic_json(root / "health.json", {"updated_at": time.time(), "job_id": key,
                            "state": job["state"], "reason": job["reason"], "errors": failures})
                if job["state"] in {"done", "held"} or time.monotonic() >= deadline:
                    stop.set()
            for thread in threads:
                thread.join()
            job = queue.get(key)
            if failures or job["state"] != "done":
                raise HoldJob("nas_pilot_not_completed_see_private_health")
            receipt = store.read(job, "publish_pending")
            pipeline.delivery.verify_completed(job, receipt, lambda: None)
            # Repeated automatic discovery must reuse, never enqueue another ASR.
            observations = [pipeline.discover(queue), pipeline.discover(queue)]
            if any(x.get("reused_completed") != 1 or x["job_id"] != key for x in observations):
                raise EvidenceError("completed_pilot_reclaimed")
            final = read_json(area.root / "source-state.json")
            result = {"job_id": key, "state": "done", "manifest_sha256": admission.manifest_hash,
                      "transcript_url": final["transcript_url"], "quality": final["quality"],
                      "review": "needs_review", "discovery_observations": observations,
                      "engine": engine, "profile": profile, "destinations_reverified": ["hub", "nas"]}
            atomic_json(root / "pilot-result.json", result)
            atomic_json(root / "health.json", {"updated_at": time.time(), "job_id": key,
                                               "state": "done", "errors": []})
            return result
        finally:
            stop.set()
            for thread in threads:
                if thread.is_alive():
                    thread.join()
            queue.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run-pilot", action="store_true")
    args = parser.parse_args()
    config = read_json(args.config)
    if args.preflight:
        _, _, report = preflight(config)
        print(json.dumps(report))
    else:
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *args: stop.set())
        print(json.dumps(run_pilot(config, stop=stop)))


if __name__ == "__main__":
    main()
