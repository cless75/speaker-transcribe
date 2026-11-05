"""Service management abstraction for cross-platform deployment."""

from __future__ import annotations

import os
import subprocess

from .profile import is_linux_current


def is_service_active(unit: str) -> bool:
    """Check if a systemd service is active (Linux-current only)."""
    if not is_linux_current():
        return False
    if os.name != "posix":
        return False
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False)
        return result.returncode == 0
    except (OSError, FileNotFoundError):
        return False


def is_service_enabled(unit: str) -> bool:
    """Check if a systemd service is enabled (Linux-current only)."""
    if not is_linux_current():
        return False
    if os.name != "posix":
        return False
    try:
        result = subprocess.run(["systemctl", "is-enabled", "--quiet", unit], check=False)
        return result.returncode == 0
    except (OSError, FileNotFoundError):
        return False


def get_service_status(unit: str) -> str:
    """Get systemd service status (Linux-current only)."""
    if not is_linux_current():
        return "not-applicable"
    if os.name != "posix":
        return "not-applicable"
    try:
        result = subprocess.run(["systemctl", "is-active", unit], 
                              capture_output=True, text=True, check=False)
        return result.stdout.strip() or "unknown"
    except (OSError, FileNotFoundError):
        return "unavailable"
