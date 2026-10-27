"""Mount-guarded NAS publication and independently resumable Hub+NAS delivery.

No Drive implementation or implicit mount/identity provisioning. Production Hub
adapter must verify ID/revision/claims and publish terminal source markers LAST.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unicodedata
import re

from pipeline_runtime import RetryJob
from pipeline_storage import (EvidenceError, atomic_json, canonical, digest, no_links,
                              safe_path, sync_directory)


def verify_mount(mountpoint: pathlib.Path, source: str):
    no_links(mountpoint)
    if not os.path.ismount(mountpoint):
        raise OSError("nas_mount_missing")
    if os.name == "posix":
        result = subprocess.run(
            ["findmnt", "--json", "--mountpoint", str(mountpoint), "--output", "TARGET,SOURCE,FSTYPE"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=True)
        mounts = json.loads(result.stdout).get("filesystems", [])
        if len(mounts) != 1 or mounts[0] != {
                "target": str(mountpoint), "source": source, "fstype": "cifs"}:
            raise OSError("nas_mount_identity_mismatch")
    elif os.name == "nt":
        # UNC share roots only. Drive letters cannot establish a remote identity.
        if (not source.startswith("\\\\") or
                str(mountpoint).rstrip("\\/").casefold() != source.rstrip("\\/").casefold()):
            raise OSError("nas_unc_identity_mismatch")
    else:
        raise OSError("unsupported_mount_platform")


class NasArea:
    def __init__(self, root: pathlib.Path, *, mountpoint: pathlib.Path, source: str,
                 collection_id: str, marker: str, minimum_free: int, mount_check=verify_mount):
        self.root, self.mountpoint = no_links(root), no_links(mountpoint)
        if not self.root.is_relative_to(self.mountpoint) or self.root == self.mountpoint:
            raise ValueError("dedicated_nas_subdirectory_required")
        if len(safe_path(marker).parts) != 1 or not collection_id or not source:
            raise ValueError("explicit_nas_identity_required")
        if type(minimum_free) is not int or minimum_free < 0:
            raise ValueError("explicit_nas_reserve_required")
        self.source, self.collection_id = source, collection_id
        self.marker, self.minimum_free, self.mount_check = marker, minimum_free, mount_check

    def guard(self):
        self.mount_check(self.mountpoint, self.source)
        no_links(self.root)
        marker = no_links(self.root / self.marker)
        if not self.root.is_dir() or not marker.is_file() or marker.stat().st_size > 4096:
            raise OSError("nas_collection_identity_missing")
        identity = json.loads(marker.read_bytes())
        if identity != {"schema": 1, "collection_id": self.collection_id, "mount_source": self.source}:
            raise EvidenceError("nas_collection_identity_mismatch")

    def _target(self, relative: str):
        safe_path(relative)
        self.guard()
        return no_links(self.root / relative)

    def _verify(self, target: pathlib.Path, manifest: dict, check, *, receipt=True):
        self.guard()
        if receipt:
            marker = no_links(target / "receipt.json")
            if marker.stat().st_size > 4 * 1024 * 1024:
                raise EvidenceError("nas_receipt_too_large")
            found = json.loads(marker.read_bytes())
            if found != manifest:
                raise EvidenceError("nas_version_conflict")
        for member in manifest["files"]:
            check()
            self.guard()
            path = no_links(target / str(safe_path(member["path"])))
            if path.stat().st_size != member["size"] or digest(path) != member["sha256"]:
                raise EvidenceError("nas_readback_mismatch")
        # Results are immutable versions: do not accept extra, unhashed files.
        actual = set()
        source = manifest["binding"].get("kind") == "hub-input"
        generated = manifest["binding"].get("primary_name", "") + ".asr"
        for directory, dirs, files in os.walk(target, followlinks=False):
            for name in [*dirs, *files]:
                path = no_links(pathlib.Path(directory) / name)
                if source and pathlib.Path(directory) == target and name == generated and name in dirs:
                    dirs.remove(name)
                    continue
                if source and pathlib.Path(directory) == target and name == "READY":
                    if not path.is_file() or path.stat().st_size != 0:
                        raise EvidenceError("invalid_source_ready")
                    continue
                if path.is_file():
                    actual.add(path.relative_to(target).as_posix())
        if actual != {m["path"] for m in manifest["files"]} | ({"receipt.json"} if receipt else set()):
            raise EvidenceError("nas_unlisted_artifacts")

    def publish(self, relative: str, local: pathlib.Path, files: list[dict], binding: dict, check):
        """Read-back verified immutable version. Caller owns destination resource lock.

        files paths are relative to local; receipt is written last. A conflicting
        existing final is never overwritten, even if its receipt is absent.
        """
        if not isinstance(files, list) or not files or len(files) > 10000:
            raise EvidenceError("invalid_delivery_inventory")
        names = set()
        for member in files:
            if not isinstance(member, dict) or set(member) != {"path", "size", "sha256"}:
                raise EvidenceError("invalid_delivery_inventory")
            path = str(safe_path(member["path"]))
            normal = unicodedata.normalize("NFC", path).casefold()
            if (normal in names or normal in {"receipt.json", "ready"} or
                    type(member["size"]) is not int or member["size"] < 0 or
                    not isinstance(member["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", member["sha256"])):
                raise EvidenceError("invalid_delivery_inventory")
            names.add(normal)
        manifest = {"schema": 1, "binding": binding, "files": files}
        target = self._target(relative)
        if binding.get("kind") == "hub-input" and (target / "READY").exists() and not (target / "receipt.json").exists():
            raise EvidenceError("ready_without_source_receipt")
        if (target / "receipt.json").exists():
            self._verify(target, manifest, check)
            return {"relative": relative, "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest()}
        self.guard()
        target.mkdir(parents=True, exist_ok=True)
        for member in files:
            check()
            self.guard()
            rel = str(safe_path(member["path"]))
            source, dest = no_links(local / rel), no_links(target / rel)
            if source.stat().st_size != member["size"] or digest(source) != member["sha256"]:
                raise EvidenceError("local_delivery_artifact_changed")
            if dest.exists():
                if dest.stat().st_size == member["size"] and digest(dest) == member["sha256"]:
                    continue  # known complete member of an interrupted attempt
                raise EvidenceError("nas_existing_member_conflict")
            if shutil.disk_usage(self.root).free - member["size"] < self.minimum_free:
                raise OSError("nas_disk_pressure")
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".partial-", dir=dest.parent)
            tmp = pathlib.Path(name)
            try:
                with source.open("rb") as inp, os.fdopen(fd, "wb") as out:
                    for block in iter(lambda: inp.read(1024 * 1024), b""):
                        check()
                        self.guard()
                        if shutil.disk_usage(self.root).free - len(block) < self.minimum_free:
                            raise OSError("nas_disk_pressure")
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                if tmp.stat().st_size != member["size"] or digest(tmp) != member["sha256"]:
                    raise EvidenceError("nas_copy_mismatch")
                self.guard()
                if dest.exists():
                    raise EvidenceError("nas_destination_appeared")
                os.replace(tmp, dest)
                sync_directory(dest.parent)
            finally:
                # Do not touch a replacement local mountpoint after disconnect.
                if tmp.exists():
                    self.guard()
                    tmp.unlink()
        check()
        self.guard()
        self._verify(target, manifest, check, receipt=False)
        atomic_json(target / "receipt.json", manifest)
        self._verify(target, manifest, check)
        return {"relative": relative, "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest()}

    def publish_source(self, relative: str, local: pathlib.Path, files: list[dict],
                       binding: dict, check):
        primary = str(safe_path(binding.get("primary_name", "")))
        if "/" in primary or primary not in {m["path"] for m in files}:
            raise EvidenceError("primary_missing_from_source_inventory")
        binding = {**binding, "kind": "hub-input"}
        evidence = self.publish(relative, local, files, binding, check)
        target = self._target(relative)
        check()
        self.guard()
        ready = no_links(target / "READY")
        if ready.exists():
            if not ready.is_file() or ready.stat().st_size:
                raise EvidenceError("invalid_source_ready")
        else:
            # Last-written visibility marker, after source manifest and read-back.
            fd, name = tempfile.mkstemp(prefix=".ready-", dir=target)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                self.guard()
                os.replace(name, ready)
                sync_directory(target)
            finally:
                if pathlib.Path(name).exists():
                    self.guard()
                    pathlib.Path(name).unlink()
        return evidence

    def verify_source(self, evidence: dict, check):
        target = self._target(evidence["relative"])
        path = no_links(target / "receipt.json")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise EvidenceError("nas_receipt_too_large")
        manifest = json.loads(path.read_bytes())
        if (manifest.get("binding", {}).get("kind") != "hub-input" or
                hashlib.sha256(canonical(manifest)).hexdigest() != evidence["manifest_sha256"]):
            raise EvidenceError("nas_source_identity_mismatch")
        self._verify(target, manifest, check)
        marker = no_links(target / "READY")
        if not marker.is_file() or marker.stat().st_size:
            raise EvidenceError("missing_or_invalid_source_ready")
        return manifest


class DualDelivery:
    """Each sink implements publish(job, result_dir, check) and verify(job, evidence, check).

    publish must NOT mark the Hub source as asr-done. finalize is called only
    after BOTH deliveries verify; it must be idempotent and read-back checked.
    """
    def __init__(self, store, *, hub, nas, finalize):
        self.store, self.sinks, self.finalize = store, {"hub": hub, "nas": nas}, finalize

    def verify_completed(self, job, receipt, check):
        deliveries = receipt["metadata"]["deliveries"]
        for name, sink in self.sinks.items():
            check()
            if sink.verify(job, deliveries[name]["evidence"], check) is not True:
                raise EvidenceError("destination_verification_failed")
        # Must itself read back final source-side markers; safe after interruption.
        self.finalize(job, deliveries, check)
        return True

    def __call__(self, job, directory, check):
        result = self.store.read(job, "ready")
        if result is None:
            raise EvidenceError("missing_asr_receipt")
        binding = {**self.store.binding(job),
                   "result_receipt_sha256": digest(self.store.path(job["id"], "ready"))}
        completed, transient = {}, []
        for name, sink in self.sinks.items():
            path = no_links(directory / f"delivery-{name}.json")
            try:
                check()
                if path.exists():
                    saved = json.loads(path.read_bytes())
                    if saved.get("binding") != binding or saved.get("destination") != name:
                        raise EvidenceError("delivery_receipt_identity_mismatch")
                    evidence = saved["evidence"]
                else:
                    evidence = sink.publish(job, directory / "result", check)
                if sink.verify(job, evidence, check) is not True:
                    raise EvidenceError("destination_verification_failed")
                atomic_json(path, {"binding": binding, "destination": name, "evidence": evidence})
                completed[name] = {"verified": True, "evidence": evidence}
            except (OSError, RetryJob) as exc:
                transient.append(type(exc).__name__)
        if transient:
            raise RetryJob("dual_delivery_incomplete")
        check()
        self.finalize(job, completed, check)
        return {"deliveries": completed}
