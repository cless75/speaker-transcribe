"""Local-only ownership and durable receipts for the isolated pipeline.

Never put this state root on NAS. These locks supplement, not replace, leases.
The GPU lock path must be shared with the existing consumer at deployment.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
from contextlib import contextmanager


class EvidenceError(ValueError):
    pass


class ResourceBusy(RuntimeError):
    pass


def safe_path(value: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise EvidenceError("unsafe_relative_path")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if any(p in {"", ".", ".."} or p.rstrip(". ") != p or
           p.split(".")[0].upper() in reserved or
           any(ord(c) < 32 or c in '<>"|?*' for c in p) for p in parts):
        raise EvidenceError("unsafe_relative_path")
    return pathlib.PurePosixPath(value)


def no_links(path: pathlib.Path) -> pathlib.Path:
    path = path.absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise EvidenceError("linked_state_path")
    return path


def digest(path: pathlib.Path, *, strict_snapshot: bool = True) -> str:
    no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise EvidenceError("non_regular_artifact")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    after = path.stat()
    if before.st_size != after.st_size or (strict_snapshot and
            (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) !=
            (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
        raise EvidenceError("artifact_changed")
    return h.hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def sync_directory(path: pathlib.Path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path: pathlib.Path, data: dict):
    no_links(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(data))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


@contextmanager
def file_lock(path: pathlib.Path):
    """Nonblocking local lock. Persistent inode; never unlink a lock on release."""
    no_links(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b", buffering=0) as stream:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ResourceBusy("resource_owned") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt
            # msvcrt permits locking beyond EOF; no write to a locked byte.
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ResourceBusy("resource_owned") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            raise RuntimeError("unsupported_lock_platform")


STAGES = ("discovered", "ready", "publish_pending")


def required_destinations(job):
    """Frozen job policy; missing field preserves the original dual contract."""
    payload = json.loads(job["payload"])
    policy = payload.get("delivery_policy", "hub-nas")
    if policy == "nas-only" and payload.get("adapter") == "nas-inbox-v1":
        return ("nas",)
    if policy not in ("hub-only", "hub-nas"):
        raise EvidenceError("invalid_delivery_policy")
    if policy == "hub-only" and payload.get("adapter") != "hub-nas-v1":
        raise EvidenceError("hub_only_requires_hub_adapter")
    return ("hub",) if policy == "hub-only" else ("hub", "nas")


class ReceiptStore:
    def __init__(self, root: pathlib.Path):
        self.root = no_links(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        sync_directory(self.root)
        sync_directory(self.root.parent)

    def directory(self, key: str) -> pathlib.Path:
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise EvidenceError("invalid_job_key")
        return no_links(self.root / "jobs" / key)

    def path(self, key: str, stage: str):
        if stage not in STAGES:
            raise EvidenceError("invalid_stage")
        return self.directory(key) / f"receipt-{stage}.json"

    def inventory(self, key: str, scope: str) -> list[dict]:
        if scope not in {"input", "result", "work/transcripts"}:
            raise EvidenceError("invalid_artifact_scope")
        root = self.directory(key) / scope
        no_links(root)
        if not root.is_dir():
            raise EvidenceError("missing_artifact_scope")
        found = []
        names = set()
        import unicodedata
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in [*dirs, *files]:
                path = no_links(pathlib.Path(directory) / name)
                relative = path.relative_to(self.directory(key)).as_posix()
                safe_path(relative)
                norm = unicodedata.normalize("NFC", relative).casefold()
                if norm in names:
                    raise EvidenceError("ambiguous_artifact_path")
                names.add(norm)
                if path.is_dir():
                    continue
                found.append({"path": relative, "size": path.stat().st_size,
                              "sha256": digest(path)})
        if not found:
            raise EvidenceError("empty_artifact_scope")
        return sorted(found, key=lambda f: f["path"])

    @staticmethod
    def binding(job: dict) -> dict:
        return {"job_id": job["id"], "identity": job["identity"],
                "payload_sha256": hashlib.sha256(job["payload"].encode()).hexdigest()}

    @staticmethod
    def _metadata(stage, metadata, job):
        if not isinstance(metadata, dict):
            raise EvidenceError("invalid_receipt_metadata")
        if stage == "discovered" and metadata.get("source_verified") is not True:
            raise EvidenceError("source_verification_required")
        if stage == "ready":
            if not isinstance(metadata.get("quality"), str) or metadata["quality"] not in {"ok", "degraded"}:
                raise EvidenceError("explicit_quality_required")
            if metadata["quality"] == "degraded" and metadata.get("review") != "needs_review":
                raise EvidenceError("degraded_requires_review")
        if stage == "publish_pending":
            deliveries = metadata.get("deliveries")
            if (not isinstance(deliveries, dict) or set(deliveries) != set(required_destinations(job)) or
                    any(not isinstance(v, dict) or v.get("verified") is not True
                        for v in deliveries.values())):
                raise EvidenceError("both_verified_destinations_required")

    def read(self, job: dict, stage: str) -> dict | None:
        path = self.path(job["id"], stage)
        no_links(path)
        if not path.exists():
            return None
        if path.stat().st_size > 4 * 1024 * 1024:
            raise EvidenceError("oversized_receipt")
        try:
            receipt = json.loads(path.read_bytes())
            if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or
                    receipt.get("stage") != stage or receipt.get("binding") != self.binding(job)):
                raise EvidenceError("receipt_identity_mismatch")
            self._metadata(stage, receipt.get("metadata"), job)
            previous = STAGES.index(stage) - 1
            upstream = None
            if previous >= 0:
                upstream_stage = STAGES[previous]
                if self.read(job, upstream_stage) is None:
                    raise EvidenceError("missing_upstream_receipt")
                upstream = digest(self.path(job["id"], upstream_stage))
            if receipt.get("upstream_sha256") != upstream:
                raise EvidenceError("upstream_receipt_changed")
            scope = {"discovered": "input", "ready": "result"}.get(stage)
            if scope and receipt.get("artifacts") != self.inventory(job["id"], scope):
                raise EvidenceError("receipt_artifacts_changed")
            if stage == "publish_pending":
                deliveries = receipt.get("metadata", {}).get("deliveries")
                if not isinstance(deliveries, dict) or set(deliveries) != set(required_destinations(job)):
                    raise EvidenceError("both_destinations_required")
                if any(not isinstance(v, dict) or v.get("verified") is not True
                       for v in deliveries.values()):
                    raise EvidenceError("unverified_delivery")
            return receipt
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise EvidenceError("invalid_receipt") from exc

    def write(self, job: dict, stage: str, metadata: dict) -> dict:
        old = self.read(job, stage)
        if old is not None:
            return old
        self._metadata(stage, metadata, job)
        previous = STAGES.index(stage) - 1
        upstream = None
        if previous >= 0:
            upstream_stage = STAGES[previous]
            if self.read(job, upstream_stage) is None:
                raise EvidenceError("missing_upstream_receipt")
            upstream = digest(self.path(job["id"], upstream_stage))
        scope = {"discovered": "input", "ready": "result"}.get(stage)
        receipt = {"schema": 1, "stage": stage, "binding": self.binding(job),
                   "upstream_sha256": upstream,
                   "artifacts": self.inventory(job["id"], scope) if scope else [],
                   "metadata": metadata}
        # Files must reach local storage before their receipt becomes durable.
        directories = {self.directory(job["id"])}
        for item in receipt["artifacts"]:
            with (self.directory(job["id"]) / item["path"]).open("r+b" if os.name == "nt" else "rb") as stream:
                os.fsync(stream.fileno())
            parent = (self.directory(job["id"]) / item["path"]).parent
            while parent.is_relative_to(self.root):
                directories.add(parent)
                if parent == self.root:
                    break
                parent = parent.parent
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            sync_directory(directory)
        atomic_json(self.path(job["id"], stage), receipt)
        return self.read(job, stage)
