"""Transactional local byte reservations. No eviction, retention or source removal."""
from __future__ import annotations

import pathlib
import shutil
import sqlite3

from pipeline_storage import no_links


class DiskPressure(RuntimeError):
    pass


class DiskBudget:
    def __init__(self, path: pathlib.Path, *, capacity: int, minimum_free: int,
                 volume: pathlib.Path, free_bytes=None):
        if (type(capacity) is not int or capacity <= 0 or type(minimum_free) is not int
                or minimum_free < 0):
            raise ValueError("explicit_byte_budget_required")
        self.volume = no_links(volume)
        if not self.volume.is_dir():
            raise ValueError("budget_volume_missing")
        self.capacity, self.minimum_free = capacity, minimum_free
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(self.volume).free)
        no_links(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.stat().st_dev != self.volume.stat().st_dev:
            raise ValueError("budget_database_must_be_on_reserved_volume")
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {"disk_budget_config", "disk_reservations"}:
                raise ValueError("not_an_isolated_budget_database")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("CREATE TABLE IF NOT EXISTS disk_budget_config "
                            "(capacity INTEGER NOT NULL, minimum_free INTEGER NOT NULL, volume TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS disk_reservations "
                            "(job_id TEXT PRIMARY KEY, bytes INTEGER NOT NULL CHECK(bytes>0))")
            config = (capacity, minimum_free, str(self.volume))
            old = self.db.execute("SELECT * FROM disk_budget_config").fetchall()
            if old and old != [config]:
                raise ValueError("budget_config_changed_requires_reconciliation")
            if not old:
                self.db.execute("INSERT INTO disk_budget_config VALUES (?,?,?)", config)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def reserve(self, key: str, size: int):
        if not key or type(size) is not int or size <= 0:
            raise ValueError("positive_byte_reservation_required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT bytes FROM disk_reservations WHERE job_id=?", (key,)).fetchone()
            if old and old[0] != size:
                raise ValueError("reservation_size_changed")
            total = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM disk_reservations").fetchone()[0]
            required = total if old else total + size
            # Conservative: reservations include bytes already materialised. This
            # can pause early, never admits two jobs against the same free bytes.
            if required > self.capacity or self.free_bytes() - required < self.minimum_free:
                raise DiskPressure("local_disk_pressure")
            if not old:
                self.db.execute("INSERT INTO disk_reservations VALUES (?,?)", (key, size))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def check(self, key: str, used: int = 0):
        row = self.db.execute("SELECT bytes FROM disk_reservations WHERE job_id=?", (key,)).fetchone()
        if not row or used > row[0]:
            raise DiskPressure("reservation_missing_or_exceeded")
        if self.free_bytes() < self.minimum_free:
            raise DiskPressure("local_disk_pressure")

    def release(self, key: str):
        """Explicit operator/retention action after ownership+disk accounting; no files removed."""
        self.db.execute("DELETE FROM disk_reservations WHERE job_id=?", (key,))
