"""Deployment profile detection for windows-legacy and linux-current setups."""

from __future__ import annotations

import os
import pathlib
from enum import Enum


class DeploymentProfile(Enum):
    WINDOWS_LEGACY = "windows-legacy"
    LINUX_CURRENT = "linux-current"
    UNKNOWN = "unknown"


_cached_profile: DeploymentProfile | None = None


def detect_profile() -> DeploymentProfile:
    """Detect deployment profile from environment or system indicators."""
    global _cached_profile
    if _cached_profile is not None:
        return _cached_profile

    # Explicit override via environment
    env_profile = os.environ.get("SPEAKER_DEPLOY_PROFILE", "").strip().lower()
    if env_profile in ("windows-legacy", "windows", "legacy"):
        _cached_profile = DeploymentProfile.WINDOWS_LEGACY
        return _cached_profile
    if env_profile in ("linux-current", "linux", "current", "lenovo"):
        _cached_profile = DeploymentProfile.LINUX_CURRENT
        return _cached_profile

    # Auto-detect based on system indicators
    if os.name == "nt":
        # Check if running under WSL
        if os.environ.get("WSL_DISTRO_NAME") or os.path.exists("/proc/version"):
            try:
                with open("/proc/version", "r", encoding="utf-8") as f:
                    if "microsoft" in f.read().lower():
                        _cached_profile = DeploymentProfile.LINUX_CURRENT
                        return _cached_profile
            except OSError:
                pass
        _cached_profile = DeploymentProfile.WINDOWS_LEGACY
        return _cached_profile

    if os.name == "posix":
        # Check for systemd (indicates linux-current setup)
        if os.path.exists("/run/systemd/system") or os.environ.get("INVOCATION_ID"):
            _cached_profile = DeploymentProfile.LINUX_CURRENT
            return _cached_profile
        # Check if we're in a typical Linux setup without systemd
        if pathlib.Path("/etc/os-release").exists():
            try:
                content = pathlib.Path("/etc/os-release").read_text(encoding="utf-8")
                if "ubuntu" in content.lower() or "debian" in content.lower():
                    # Could be either, prefer linux-current if unified-intake config exists
                    if pathlib.Path("/home/clessd/speaker-transcribe").exists():
                        _cached_profile = DeploymentProfile.LINUX_CURRENT
                        return _cached_profile
            except OSError:
                pass
        # Default to linux-current for POSIX if not explicitly Windows
        _cached_profile = DeploymentProfile.LINUX_CURRENT
        return _cached_profile

    _cached_profile = DeploymentProfile.UNKNOWN
    return _cached_profile


def get_profile() -> DeploymentProfile:
    """Get cached deployment profile."""
    return detect_profile()


def is_windows_legacy() -> bool:
    """Check if running in Windows-legacy deployment profile."""
    return get_profile() == DeploymentProfile.WINDOWS_LEGACY


def is_linux_current() -> bool:
    """Check if running in Linux-current deployment profile."""
    return get_profile() == DeploymentProfile.LINUX_CURRENT


def require_linux(feature: str) -> None:
    """Raise error if feature requires Linux but running on non-Linux profile."""
    if not is_linux_current():
        raise RuntimeError(f"{feature} requires linux-current deployment profile")
