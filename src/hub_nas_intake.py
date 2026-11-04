"""Explicit-project, read/copy-only Hub source adapter for isolated stage queues.

This adapter never changes Drive markers/claims or enables ASR. Before execution
adapters are attached, production requires route exclusion, claim ownership,
journal migration and component attestations. Terminal history stays authoritative.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import tempfile
from dataclasses import asdict

import hub_worker as hub
from hub_intake import MediaFile, MEDIA
from pipeline_runtime import HoldJob
from pipeline_storage import EvidenceError, digest, no_links, safe_path, sync_directory, required_destinations


class HubNasIntake:
    def __init__(self, drive, nas, *, projects: list[str], node_config: dict,
                 legacy_worker_root: pathlib.Path, scan_cache: pathlib.Path,
                 profile_digest: str, engine_digest: str, source_paths: dict | None = None,
                 owned_state=None, project_delivery: dict | None = None):
        if not projects or len(set(projects)) != len(projects):
            raise ValueError("explicit_unique_projects_required")
        for project in projects:
            if len(safe_path(project).parts) != 1 or project == "_inbox":
                raise ValueError("pilot_requires_explicit_project_route")
        if not all(re.fullmatch(r"[a-f0-9]{64}", d) for d in (profile_digest, engine_digest)):
            raise ValueError("profile_and_engine_content_digests_required")
        self.drive, self.nas, self.projects, self.cfg = drive, nas, tuple(projects), node_config
        self.legacy_worker_root, self.scan_cache = legacy_worker_root, scan_cache
        self.profile_digest, self.engine_digest = profile_digest, engine_digest
        if project_delivery is None:
            project_delivery = {}
        if (not isinstance(project_delivery, dict) or set(project_delivery) - set(projects) or
                any(v not in ("hub-only", "hub-nas") for v in project_delivery.values())):
            raise ValueError("invalid_project_delivery_policy")
        self.project_delivery = dict(project_delivery)
        if source_paths is not None:
            if set(source_paths) != set(projects):
                raise ValueError("scope_required_for_every_project")
            for paths in source_paths.values():
                if paths is not None and (not isinstance(paths, list) or not paths):
                    raise ValueError("nonempty_explicit_source_paths_required")
                for value in paths or []:
                    safe_path(value)
        self.source_paths, self.owned_state = source_paths, owned_state

    def discover(self, queue) -> dict:
        report = {"candidates": 0, "held": 0}
        for project in self.projects:
            allowed = self.source_paths.get(project) if self.source_paths is not None else None
            if allowed is not None:
                for path in allowed:
                    try:
                        self.admit_exact(queue, project, path)
                        report["candidates"] += 1
                    except (EvidenceError, HoldJob, ValueError, StopIteration):
                        report["held"] += 1
                continue
            items, held = hub.pending_inputs(self.drive, project, self.cfg,
                                             self.scan_cache / project,
                                             worker_root=self.legacy_worker_root)
            report["held"] += len(held)
            for item, peers in items:
                allowed = self.source_paths.get(project) if self.source_paths is not None else None
                if allowed is not None and item.path not in allowed:
                    continue
                # I1 does not silently inherit the old size-based multi-video
                # heuristic. I2 must provide alignment/coverage evidence first.
                if peers or pathlib.PurePosixPath(item.path).suffix.lower() not in MEDIA:
                    report["held"] += 1
                    continue
                journal = self.legacy_worker_root / "intake" / item.project / item.file_id / item.revision / "job.json"
                if journal.exists():
                    report["held"] += 1
                    continue  # migration is explicit, not another ASR of old work
                name = pathlib.PurePosixPath(item.path).name
                safe_path(name)
                # marker presence is volatile, not part of frozen source identity.
                source = asdict(item)
                source.pop("marker")
                submission = f"drive:{project}:{item.file_id}"
                relative = "sources/" + hashlib.sha256(
                    f"{submission}\0{item.revision}".encode()).hexdigest()
                self._enqueue(queue, submission, item.revision, source, name, relative)
                report["candidates"] += 1
        return report

    def admit_exact(self, queue, project: str, path: str):
        if project not in self.projects:
            raise EvidenceError("project_outside_pilot_scope")
        safe_path(path)
        allowed = self.source_paths.get(project) if self.source_paths is not None else None
        if allowed is not None and path not in allowed:
            raise EvidenceError("source_outside_pilot_scope")
        item, peers = hub.choose_group(self.drive, project, path, allow_vtt=False)
        if item.path != path or peers or pathlib.PurePosixPath(path).suffix.lower() not in MEDIA:
            raise HoldJob("i1_requires_single_explicit_media")
        journal = self.legacy_worker_root / "intake" / item.project / item.file_id / item.revision / "job.json"
        if journal.exists():
            raise HoldJob("legacy_journal_requires_migration")
        source = asdict(item)
        source.pop("marker")
        name = pathlib.PurePosixPath(path).name
        safe_path(name)
        submission = f"drive:{project}:{item.file_id}"
        relative = "sources/" + hashlib.sha256(f"{submission}\0{item.revision}".encode()).hexdigest()
        return self._enqueue(queue, submission, item.revision, source, name, relative)

    def _policy_engine(self, policy):
        if policy == "hub-nas":
            return self.engine_digest
        return hashlib.sha256(f"{self.engine_digest}\0delivery:{policy}".encode()).hexdigest()

    def _enqueue(self, queue, submission, revision, source, name, relative):
        policy = self.project_delivery.get(source["project"], "hub-nas")
        payload = {"adapter": "hub-nas-v1", "source": source,
                   "primary_name": name, "nas_relative": relative}
        # Keep legacy dual payloads/identities byte-compatible.
        if policy != "hub-nas":
            payload["delivery_policy"] = policy
        return queue.enqueue(submission, revision, self.profile_digest, self._policy_engine(policy), payload)

    def _source(self, job):
        data = json.loads(job["payload"])
        source = data.get("source")
        if not isinstance(source, dict) or data.get("adapter") != "hub-nas-v1":
            raise EvidenceError("invalid_hub_submission")
        required_destinations(job)  # reject malformed frozen policies before any I/O
        item = MediaFile(**source, marker=False)
        if item.project not in self.projects:
            raise EvidenceError("project_outside_pilot_scope")
        # A stricter privacy policy is a hard fence, not a relabelling of an
        # old job: it must never publish or stage an old dual-destination job.
        if (self.project_delivery.get(item.project) == "hub-only" and
                data.get("delivery_policy", "hub-nas") != "hub-only"):
            raise HoldJob("hub_only_policy_conflicts_with_frozen_job")
        allowed = self.source_paths.get(item.project) if self.source_paths is not None else None
        if allowed is not None and item.path not in allowed:
            raise EvidenceError("source_outside_pilot_scope")
        expected = [f"drive:{item.project}:{item.file_id}", item.revision,
                    self.profile_digest, self._policy_engine(data.get("delivery_policy", "hub-nas"))]
        if json.loads(job["identity"]) != expected:
            raise EvidenceError("hub_processing_identity_mismatch")
        name = str(safe_path(data["primary_name"]))
        expected_relative = "sources/" + hashlib.sha256(
            f"{expected[0]}\0{item.revision}".encode()).hexdigest()
        if ("/" in name or name != pathlib.PurePosixPath(item.path).name or
                data["nas_relative"] != expected_relative):
            raise EvidenceError("hub_source_locator_mismatch")
        if not isinstance(item.md5, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", item.md5):
            raise HoldJob("hub_source_checksum_unavailable")
        if type(item.size) is not int or item.size <= 0:
            raise HoldJob("hub_source_size_invalid")
        return data, item

    def stage(self, job, directory, check):
        data, item = self._source(job)
        check()
        # Do not create even a local partial if the destination mount is absent.
        use_nas = "nas" in required_destinations(job)
        if use_nas:
            self.nas.guard()
        hub.source_unchanged(self.drive, item)
        # Recheck terminal/owner markers at copy time, not only during discovery.
        state = self.drive.state(item.project, item.path)
        if (state.get("status") not in {"unknown", "queued"} and
                (self.owned_state is None or self.owned_state(job, state) is not True)):
            raise HoldJob("hub_state_changed_since_discovery")
        primary = data["primary_name"]
        local_dir = no_links(directory / "input")
        local_dir.mkdir(parents=True, exist_ok=True)
        target = no_links(local_dir / primary)
        if target.exists():
            if target.stat().st_size != item.size or hub.file_md5(target) != item.md5.lower():
                raise HoldJob("existing_local_source_conflict")
        else:
            fd, name = tempfile.mkstemp(prefix=".download-", dir=local_dir)
            import os
            os.close(fd)
            tmp = pathlib.Path(name)
            try:
                self.drive.call("copyto", self.drive.path(item.project, item.path), str(tmp))
                check()
                if tmp.stat().st_size != item.size or hub.file_md5(tmp) != item.md5.lower():
                    raise HoldJob("hub_copy_checksum_mismatch")
                with tmp.open("r+b") as stream:
                    os.fsync(stream.fileno())
                if target.exists():
                    raise HoldJob("local_source_appeared")
                os.replace(tmp, target)
                sync_directory(local_dir)
            finally:
                tmp.unlink(missing_ok=True)
        check()
        hub.source_unchanged(self.drive, item)
        files = [{"path": primary, "size": item.size, "sha256": digest(target)}]
        if not use_nas:
            return {"source_verified": True, "primary_name": primary}
        evidence = self.nas.publish_source(data["nas_relative"], local_dir, files,
                                           {"submission": json.loads(job["identity"])[0],
                                            "revision": item.revision, "primary_name": primary,
                                            "drive_id": item.file_id, "drive_md5": item.md5.lower()}, check)
        self.nas.verify_source(evidence, check)
        return {"source_verified": True, "nas_source": evidence, "primary_name": primary}
