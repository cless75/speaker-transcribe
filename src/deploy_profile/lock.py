"""File locking abstraction for cross-platform deployment."""

from __future__ import annotations

import os
import pathlib
from contextlib import contextmanager
from typing import Iterator

from .profile import is_linux_current, is_windows_legacy


class LockError(RuntimeError):
    pass


@contextmanager
def exclusive_lock(lock_path: pathlib.Path, *, nonblocking: bool = False) -> Iterator[None]:
    """Acquire exclusive lock with profile-appropriate implementation."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    
    if is_linux_current() or os.name == "posix":
        try:
            import fcntl
            with lock_path.open("w") as stream:
                try:
                    flags = fcntl.LOCK_EX
                    if nonblocking:
                        flags |= fcntl.LOCK_NB
                    fcntl.flock(stream, flags)
                    try:
                        yield
                    finally:
                        fcntl.flock(stream, fcntl.LOCK_UN)
                except BlockingIOError as exc:
                    raise LockError("another process holds the lock") from exc
            return
        except ImportError:
            pass
    
    if is_windows_legacy() or os.name == "nt":
        try:
            import msvcrt
        except ImportError as exc:
            raise LockError("Windows lock support is unavailable") from exc
        # msvcrt locks byte ranges; establish the first byte before opening r+b.
        with lock_path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
                msvcrt.locking(stream.fileno(), mode, 1)
            except OSError as exc:
                raise LockError("another process holds the lock") from exc
            try:
                yield
            finally:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        return

    raise LockError(f"no locking mechanism available for {os.name}")
