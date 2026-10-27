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
from pipeline_storage import EvidenceError, digest, no_links, safe_path, sync_directory


class HubNasIntake:
    def __init__(self, drive, nas, *, projects: list[str], node_config: dict,
                 legacy_worker_root: pathlib.Path, scan_cache: pathlib.Path,
                 profile_digest: str, engine_digest: str):
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

    def discover(self, queue) -> dict:
        report = {"candidates": 0, "held": 0}
        for project in self.projects:
            items, held = hub.pending_inputs(self.drive, project, self.cfg,
                                             self.scan_cache / project,
                                             worker_root=self.legacy_worker_root)
            report["held"] += len(held)
            for item, peers in items:
                # I1 does not silently inherit the old size-based multi-video
                # heuristic. I2 must provide alignment/coverage evidence first.
                if peers or pathlib.PurePosixPath(item.path).suffix.lower() not in MEDIA:
                    report["held"] += 1
                    continue
                name = pathlib.PurePosixPath(item.path).name
                safe_path(name)
                # marker presence is volatile, not part of frozen source identity.
                source = asdict(item)
                source.pop("marker")
                submission = f"drive:{project}:{item.file_id}"
                relative = "sources/" + hashlib.sha256(
                    f"{submission}\0{item.revision}".encode()).hexdigest()
                queue.enqueue(submission, item.revision, self.profile_digest, self.engine_digest,
                              {"adapter": "hub-nas-v1", "source": source,
                               "primary_name": name, "nas_relative": relative})
                report["candidates"] += 1
        return report

    def _source(self, job):
        data = json.loads(job["payload"])
        source = data.get("source")
        if not isinstance(source, dict) or data.get("adapter") != "hub-nas-v1":
            raise EvidenceError("invalid_hub_submission")
        item = MediaFile(**source, marker=False)
        if item.project not in self.projects:
            raise EvidenceError("project_outside_pilot_scope")
        expected = [f"drive:{item.project}:{item.file_id}", item.revision,
                    self.profile_digest, self.engine_digest]
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
        self.nas.guard()
        hub.source_unchanged(self.drive, item)
        # Recheck terminal/owner markers at copy time, not only during discovery.
        state = self.drive.state(item.project, item.path)
        if state.get("status") not in {"unknown", "queued"}:
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
        evidence = self.nas.publish_source(data["nas_relative"], local_dir, files,
                                           {"submission": json.loads(job["identity"])[0],
                                            "revision": item.revision, "primary_name": primary,
                                            "drive_id": item.file_id, "drive_md5": item.md5.lower()}, check)
        self.nas.verify_source(evidence, check)
        return {"source_verified": True, "nas_source": evidence, "primary_name": primary}
