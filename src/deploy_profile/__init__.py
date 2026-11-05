"""Platform abstraction layer for dual deployment profiles."""

from .profile import (
    DeploymentProfile,
    detect_profile,
    get_profile,
    is_linux_current,
    is_windows_legacy,
    require_linux,
)

__all__ = [
    "DeploymentProfile",
    "detect_profile",
    "get_profile",
    "is_linux_current",
    "is_windows_legacy",
    "require_linux",
]
