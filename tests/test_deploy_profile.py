import pytest

import deploy_profile.profile as profile
from deploy_profile.lock import LockError, exclusive_lock
import hub_worker


@pytest.fixture(autouse=True)
def isolated_profile_cache():
    previous = profile._cached_profile
    profile._cached_profile = None
    yield
    profile._cached_profile = previous


def reset_profile(monkeypatch):
    monkeypatch.setattr(profile, "_cached_profile", None)


def test_environment_override_selects_deployment_profile(monkeypatch):
    monkeypatch.setenv("SPEAKER_DEPLOY_PROFILE", "windows-legacy")
    reset_profile(monkeypatch)
    assert profile.is_windows_legacy()
    monkeypatch.setenv("SPEAKER_DEPLOY_PROFILE", "linux-current")
    reset_profile(monkeypatch)
    assert profile.is_linux_current()


def test_lock_rejects_second_nonblocking_owner(tmp_path):
    lock = tmp_path / "worker.lock"
    with exclusive_lock(lock):
        with pytest.raises(LockError, match="another process"):
            with exclusive_lock(lock, nonblocking=True):
                pass


def test_windows_legacy_skips_systemd_legacy_timer_check(monkeypatch):
    monkeypatch.setattr(hub_worker, "is_windows_legacy", lambda: True)
    monkeypatch.setattr(hub_worker, "is_linux_current", lambda: False)
    hub_worker.legacy_inactive()


def test_linux_current_refuses_active_legacy_timer(monkeypatch):
    monkeypatch.setattr(hub_worker, "is_windows_legacy", lambda: False)
    monkeypatch.setattr(hub_worker, "is_linux_current", lambda: True)
    monkeypatch.setattr(hub_worker, "is_service_active", lambda _unit: True)
    with pytest.raises(RuntimeError, match="legacy consumer"):
        hub_worker.legacy_inactive()
