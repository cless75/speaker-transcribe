"""Private process receipts and bounded resource checks for an owned ASR child."""
from __future__ import annotations

import os
import pathlib
import subprocess

from pipeline_runtime import HoldJob
from pipeline_storage import atomic_json, no_links


def gpu_idle():
    result = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True)
    if result.stdout.strip():
        raise HoldJob("gpu_compute_process_requires_reconciliation")


class AsrProcess:
    def __init__(self, directory: pathlib.Path, *, check, ownership_check):
        self.directory, self.check_stage, self.check_owner = no_links(directory), check, ownership_check
        self.record = self.directory / "asr-process.json"
        self.pid = None

    def started(self, pid: int):
        self.pid = pid
        boot = pathlib.Path("/proc/sys/kernel/random/boot_id")
        atomic_json(self.record, {"schema": 1, "pid": pid, "parent_pid": os.getpid(),
                                 "boot_id": boot.read_text().strip() if boot.exists() else None,
                                 "finished": False})
        self.check()

    def check(self):
        self.check_stage()
        self.check_owner()

    def finished(self, returncode: int):
        atomic_json(self.record, {"schema": 1, "pid": self.pid,
                                 "parent_pid": os.getpid(), "finished": True,
                                 "returncode": returncode})
