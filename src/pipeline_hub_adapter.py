"""Concrete Hub pilot stages; no implicit services, source cleanup or claim theft."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import os
import pathlib
import shutil
import subprocess
import tempfile

import hub_worker as hub
from hub_intake import DriveError
from pipeline_delivery import DualDelivery
from pipeline_process import AsrProcess, gpu_idle
from pipeline_queue import LeaseLost
from pipeline_runtime import HoldJob, RetryJob
from pipeline_storage import EvidenceError, atomic_json, digest, no_links, required_destinations


def duration_seconds(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_format",
         "-of", "json", str(path)], stdin=subprocess.DEVNULL,
        capture_output=True, timeout=30, check=True)
    value = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise HoldJob("invalid_media_duration")
    return value


class HubPipeline:
    def __init__(self, intake, store, claims, *, node_config: dict,
                 config_path: pathlib.Path, maximum_job: int, executor=None,
                 probe=duration_seconds, gpu_check=gpu_idle):
        self.intake, self.store, self.claims, self.cfg = intake, store, claims, node_config
        self.config_path, self.maximum_job = config_path, maximum_job
        self.executor, self.probe, self.gpu_check = executor, probe, gpu_check
        self.delivery = DualDelivery(store, hub=_HubSink(self), nas=_NasSink(self), finalize=self.finalize)
        self.intake.owned_state = self.owned_state

    def source(self, job):
        return self.intake._source(job)

    def worker_config(self, job, directory):
        return self.cfg, self.config_path

    def validate_engine_result(self, job, metadata, output):
        """Source adapters may enforce additional frozen execution policy."""

    def public_source(self, job):
        _, item = self.source(job)
        return pathlib.Path(self.cfg["hub_root"]) / item.project / pathlib.Path(item.path)

    def result_review(self, job, metadata):
        return "needs_review" if metadata["status"] == "degraded" else "not_reviewed"

    def route(self, job):
        data, item = self.source(job)
        started = hub.watcher.parse_started_from_name(pathlib.PurePosixPath(item.path).stem)
        if started is None:
            started = dt.datetime.fromisoformat(item.modified.replace("Z", "+00:00")).replace(tzinfo=None)
        sid = hub.watcher.generate_session_id(pathlib.Path(data["primary_name"]), started)
        import hashlib
        prefix = sid[:65].encode("utf-8")[:100].decode("utf-8", errors="ignore")
        sid = prefix + "-" + hashlib.sha256(f"{item.file_id}:{item.revision}".encode()).hexdigest()[:8] + "-p" + job["id"]
        return {"session_id": sid, "target": f"{item.project}/sessions/{started:%Y-%m}/{sid}"}

    def owned_state(self, job, state):
        self.claims.check(job)
        _, item = self.source(job)
        return (state.get("host") == self.claims.host and state.get("pipeline_job_id") == job["id"]
                and state.get("source_id") == item.file_id and state.get("source_revision") == item.revision
                and state.get("status") in {"copying", "in-progress", "awaiting-publication"})

    def _state(self, job):
        _, item = self.source(job)
        marker = pathlib.PurePosixPath(item.path + ".state.json")
        parent = () if str(marker.parent) == "." else marker.parent.parts
        matches = [e for e in self.intake.drive.list(item.project, *parent)
                   if not e.get("IsDir") and e.get("Name") == marker.name]
        if len(matches) > 1:
            raise EvidenceError("ambiguous_source_marker")
        directory = self.store.directory(job["id"])
        receipt = directory / "source-marker-id.json"
        saved = json.loads(receipt.read_bytes()) if receipt.exists() else None
        if not matches:
            if saved:
                raise HoldJob("source_marker_disappeared")
            return {}
        identity = {"id": matches[0].get("ID"), "source_id": item.file_id, "revision": item.revision}
        if not identity["id"] or (saved and saved != identity):
            raise HoldJob("source_marker_identity_changed")
        raw = self.intake.drive.call("cat", self.intake.drive.path(item.project, item.path + ".state.json"))
        if len(raw) > 1024 * 1024:
            raise EvidenceError("oversized_source_marker")
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise EvidenceError("invalid_source_marker")
        if state.get("source_revision") and state["source_revision"] != item.revision:
            raise HoldJob("source_marker_revision_changed")
        atomic_json(receipt, identity)
        return state

    def progress(self, job, status):
        _, item = self.source(job)
        self.claims.check(job)
        state = self._state(job)
        if state and state.get("status") != "queued" and not self.owned_state(job, state):
            raise HoldJob("source_not_owned_or_already_terminal")
        route = self.route(job)
        record = {"status": status, "source_id": item.file_id, "source_revision": item.revision,
                  "pipeline_job_id": job["id"], "host": self.claims.host,
                  "session_id": route["session_id"], "pid": item.project,
                  "worker_completed": status == "awaiting-publication"}
        started = self.store.directory(job["id"]) / "started-at.json"
        if not started.exists():
            atomic_json(started, {"at": dt.datetime.now(dt.timezone.utc).isoformat()})
        record["started_at"] = json.loads(started.read_bytes())["at"]
        path = self.store.directory(job["id"]) / "progress-state.json"
        atomic_json(path, record)
        self.intake.drive.call("copyto", str(path), self.intake.drive.path(item.project, item.path + ".state.json"))
        for delay in (0, 1, 2):
            if delay:
                import time
                time.sleep(delay)
            confirmed = self._state(job)
            if confirmed == record:
                return
            if confirmed and confirmed.get("status") != "queued" and not self.owned_state(job, confirmed):
                raise HoldJob("foreign_progress_marker")
        raise RetryJob("progress_state_not_yet_verified")

    def stage(self, job, directory, check):
        try:
            data, item = self.source(job)
            state = self._state(job)
            if state and state.get("status") != "queued":
                # Do not acquire or alter claims for a terminal foreign source.
                if (state.get("status") not in {"copying", "in-progress", "awaiting-publication"} or
                        state.get("host") != self.claims.host or state.get("pipeline_job_id") != job["id"]):
                    raise HoldJob("source_no_longer_queued")
                if job["id"] not in self.claims.active:
                    self.claims.ensure(job, item)
                if not self.owned_state(job, state):
                    raise HoldJob("source_no_longer_queued")
            self.claims.ensure(job, item)
            metadata = self.intake.stage(job, directory, check)
            duration = self.probe(directory / "input" / data["primary_name"])
            # Conservative admission for source+work copy, several PCM intermediates,
            # interval frames/OCR and retained output snapshot. Checks continue while
            # the owned child runs; this is not permission to expand without a bound.
            interval = max(1, float((self.cfg.get("video_frames") or {}).get("interval_sec", 60)))
            estimate = 2 * item.size + math.ceil(duration * 256000) + math.ceil(duration / interval) * 8 * 1024**2 + 1024**3
            if estimate > self.maximum_job:
                raise HoldJob("media_expansion_exceeds_job_budget")
            self.progress(job, "in-progress")
            return {**metadata, "duration_seconds": duration, "estimated_bytes": estimate, **self.route(job)}
        except BaseException:
            try:
                self.claims.abandon(job)
            except Exception:
                pass
            raise

    def asr(self, job, directory, check):
        try:
            data, item = self.source(job)
            self.claims.check(job)
            check()
            self.gpu_check()
            completion = directory / "engine-completion.json"
            if completion.exists():
                value = json.loads(completion.read_bytes())
                if value.get("binding") != self.store.binding(job) or value.get("files") != self.store.inventory(job["id"], "work/transcripts"):
                    raise EvidenceError("engine_completion_changed")
                return self.snapshot_result(job, directory, value, check)
            if (directory / "result").exists():
                raise HoldJob("unreceipted_result_requires_reconciliation")
        except BaseException:
            try:
                self.claims.abandon(job)
            except Exception:
                pass
            raise
        source = directory / "input" / data["primary_name"]
        work = no_links(directory / "work")
        local = work / "input" / data["primary_name"]
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists():
            if digest(local) != digest(source):
                raise HoldJob("work_input_conflict")
        else:
            try:
                os.link(source, local)
            except OSError:
                shutil.copyfile(source, local)
        output = work / "transcripts"
        output.mkdir(exist_ok=True)
        control = AsrProcess(directory, check=check, ownership_check=lambda: self.claims.check(job))
        worker_config, config_path = self.worker_config(job, directory)
        if self.executor is None:
            fd = os.open(directory / "private-asr.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as log, contextlib.redirect_stderr(log):
                ok, _, transcript, meta = hub.watcher.run_asr(
                    local, worker_config, item.project, local.parent, output,
                    config_path, process_control=control)
            if not ok:
                raise HoldJob("asr_did_not_complete")
        else:
            transcript, meta = self.executor(local, output, worker_config, config_path, item.project)
        if (not isinstance(meta, dict) or meta.get("status") not in {"ok", "degraded"} or
                transcript is None or not transcript.is_file() or transcript.stat().st_size == 0 or
                not transcript.is_relative_to(output)):
            raise HoldJob("incomplete_asr_result")
        quality = meta.get("quality") or {}
        if meta["status"] == "degraded" and quality.get("status") != "degraded":
            raise HoldJob("missing_degraded_quality")
        if not list(output.glob("*-run-meta.json")):
            raise HoldJob("missing_run_metadata")
        self.validate_engine_result(job, meta, output)
        artifacts = meta.get("outputs")
        if not isinstance(artifacts, dict) or not artifacts.get("md") or not artifacts.get("run_meta"):
            raise HoldJob("mandatory_output_manifest_missing")
        declared = [artifacts.get(k) for k in ("json", "txt", "md", "vtt", "segments_jsonl", "run_meta")]
        structured = artifacts.get("structured") or {}
        if not isinstance(structured, dict):
            raise HoldJob("invalid_structured_output_manifest")
        declared.extend(structured.values())
        for value in declared:
            if value is None:
                continue
            if not isinstance(value, str):
                raise HoldJob("invalid_declared_output_path")
            artifact = no_links(pathlib.Path(value))
            if not artifact.is_relative_to(output.absolute()) or not artifact.is_file() or artifact.stat().st_size == 0:
                raise HoldJob("mandatory_output_missing_or_outside_session")
        route = self.route(job)
        public = pathlib.Path(self.cfg["hub_root"])
        hub.rewrite_output_paths(output, local, str(public / route["target"] / "transcripts"),
                                 str(self.public_source(job)),
                                 related=[(str(local.parent), str(self.public_source(job).parent))])
        review = self.result_review(job, meta)
        atomic_json(output / "quality.json", {"status": meta["status"], "review": review, "details": quality})
        metadata = {"quality": meta["status"], "review": review, "transcript_quality": quality,
                    "transcript": "transcripts/" + transcript.name, **route}
        inventory = self.store.inventory(job["id"], "work/transcripts")
        for member in inventory:
            with (directory / member["path"]).open("r+b" if os.name == "nt" else "rb") as stream:
                os.fsync(stream.fileno())
        value = {"schema": 1, "binding": self.store.binding(job), "files": inventory, "metadata": metadata}
        atomic_json(completion, value)
        return self.snapshot_result(job, directory, value, check)

    def snapshot_result(self, job, directory, completion, check):
        expected = {m["path"].removeprefix("work/"): m for m in completion["files"]}
        result = no_links(directory / "result")
        for path in result.rglob("*"):
            no_links(path)
            if path.is_file() and path.relative_to(result).as_posix() not in expected:
                raise HoldJob("unknown_result_artifact")
        scratch = directory / "work" / "snapshot-tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        for relative, member in expected.items():
            check()
            self.claims.check(job)
            dest = no_links(result / relative)
            if dest.exists():
                if dest.stat().st_size != member["size"] or digest(dest) != member["sha256"]:
                    raise HoldJob("existing_result_conflict")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=scratch)
            temp = pathlib.Path(name)
            try:
                with os.fdopen(fd, "wb") as out, (directory / member["path"]).open("rb") as inp:
                    for block in iter(lambda: inp.read(1024 * 1024), b""):
                        check()
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                if temp.stat().st_size != member["size"] or digest(temp) != member["sha256"]:
                    raise EvidenceError("result_snapshot_changed")
                if dest.exists():
                    raise HoldJob("result_destination_appeared")
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
        metadata = completion["metadata"]
        # Durable local evidence precedes any network progress/publication.
        self.store.write(job, "ready", metadata)
        return metadata

    def finalize(self, job, deliveries, check):
        destinations = list(required_destinations(job))
        if set(deliveries) != set(destinations) or any(v.get("verified") is not True for v in deliveries.values()):
            raise EvidenceError("delivery_policy_mismatch")
        _, item = self.source(job)
        self.claims.ensure(job, item)
        self.claims.check(job)
        check()
        hub.source_unchanged(self.intake.drive, item)
        current = self._state(job)
        own_done = (current.get("status") == "asr-done" and
                    current.get("pipeline_job_id") == job["id"] and
                    current.get("publication_verified") is True)
        if not own_done and not self.owned_state(job, current):
            raise HoldJob("source_marker_changed_before_completion")
        receipt = self.store.read(job, "ready")
        meta, route = receipt["metadata"], self.route(job)
        path = self.store.directory(job["id"]) / "final-state.json"
        if path.exists():
            state = json.loads(path.read_bytes())
            if state.get("deliveries_verified") != destinations:
                raise EvidenceError("local_final_delivery_policy_mismatch")
            if (state.get("source_id"), state.get("source_revision"), state.get("pipeline_job_id"),
                    state.get("pid"), state.get("session_id"), state.get("status"), state.get("review")) != (
                    item.file_id, item.revision, job["id"], item.project, route["session_id"], "asr-done", meta["review"]):
                raise EvidenceError("local_final_state_identity_mismatch")
        else:
            state = {"status": "asr-done", "pid": item.project, "session_id": route["session_id"],
                     "source_id": item.file_id, "source_revision": item.revision,
                     "pipeline_job_id": job["id"], "host": self.claims.host,
                     "worker_completed": True, "publication_verified": True,
                     "deliveries_verified": destinations, "review": meta["review"],
                     "transcript_quality": {**meta["transcript_quality"], "status": meta["quality"]},
                     "transcript_url": "https://drive.google.com/file/d/" + deliveries["hub"]["evidence"]["transcript_id"] + "/view",
                     "transcript_path": str(pathlib.Path(self.cfg["hub_root"]) / route["target"] / meta["transcript"]),
                     "started_at": current.get("started_at"), "last_error": None,
                     "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            atomic_json(path, state)
        for target in (self.intake.drive.path(route["target"], "pipeline/state.json"),
                       self.intake.drive.path(item.project, item.path + ".state.json")):
            check()
            self.claims.check(job)
            self.intake.drive.call("copyto", str(path), target)
            if json.loads(self.intake.drive.call("cat", target)) != state:
                raise HoldJob("terminal_marker_not_verified")
        self._state(job)  # stable marker Drive ID after final publication
        self.claims.retire(job)


class _HubSink:
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
            raise HoldJob("hub_source_or_publication_unverified") from exc

    def _publish(self, job, result, check):
        _, item = self.p.source(job)
        self.p.claims.ensure(job, item)
        self.p.progress(job, "awaiting-publication")
        check()
        hub.source_unchanged(self.p.intake.drive, item)
        target = self.p.route(job)["target"]
        self.p.intake.drive.call("copy", str(result), self.p.intake.drive.path(target))
        published = hub.remote_verified(self.p.intake.drive, target, result)
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
        target = self.p.route(job)["target"]
        if evidence.get("target") != target or evidence.get("result_sha256") != digest(self.p.store.path(job["id"], "ready")):
            raise EvidenceError("hub_delivery_identity_mismatch")
        published = hub.remote_verified(self.p.intake.drive, target, self.p.store.directory(job["id"]) / "result")
        transcript = self.p.store.read(job, "ready")["metadata"]["transcript"]
        return (published.get(transcript) or {}).get("ID") == evidence["transcript_id"]


class _NasSink:
    def __init__(self, pipeline):
        self.p = pipeline

    def arguments(self, job):
        if "nas" not in required_destinations(job):
            raise EvidenceError("nas_forbidden_by_job_policy")
        source = self.p.store.read(job, "discovered")["metadata"]
        evidence = source["nas_source"]
        relative = evidence["relative"] + "/" + source["primary_name"] + ".asr/" + job["id"]
        files = [{**f, "path": f["path"].removeprefix("result/")}
                 for f in self.p.store.inventory(job["id"], "result")]
        binding = {**self.p.store.binding(job), "source_manifest_sha256": evidence["manifest_sha256"],
                   "result_receipt_sha256": digest(self.p.store.path(job["id"], "ready"))}
        return source, relative, files, binding

    def publish(self, job, result, check):
        source, relative, files, binding = self.arguments(job)
        self.p.intake.nas.verify_source(source["nas_source"], check)
        return self.p.intake.nas.publish(relative, result, files, binding, check)

    def verify(self, job, evidence, check):
        source, relative, files, binding = self.arguments(job)
        if evidence.get("relative") != relative:
            raise EvidenceError("nas_result_location_changed")
        self.p.intake.nas.verify_source(source["nas_source"], check)
        return self.p.intake.nas.verify_publication(evidence, files, binding, check)
