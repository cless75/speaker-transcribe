"""Safety defaults for private zoomrs credentials and startup config."""

import os
import stat

import pytest
import yaml

from scripts.zoomrs_config import read_credentials, render_config, write_private


def test_generate_private_config_disabled_until_backlog_checked(tmp_path):
    env = tmp_path / "zoomrs.env"
    env.write_text('ZOOM_ACCOUNT_ID=account\nZOOM_CLIENT_ID=id\nZOOM_CLIENT_SECRET=secret:#value\n',
                   encoding="utf-8")
    os.chmod(env, 0o600)
    credentials = read_credentials(env)
    output = tmp_path / "zoomrs" / "config.yml"
    write_private(output, render_config(credentials))
    settings = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert settings["client"]["secret"] == "secret:#value"
    assert settings["storage"]["repository"] == "/data/recordings"
    assert settings["server"]["sync_job"] is False
    assert settings["server"]["download_job"] is False
    for key in ("trash_downloaded", "delete_downloaded", "delete_skipped"):
        assert settings["client"][key] is False
    assert settings["syncable"]["optional"] == ["audio_transcript", "chat_file"]
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_bad_credentials_never_get_written(tmp_path):
    env = tmp_path / "zoomrs.env"
    env.write_text("ZOOM_CLIENT_ID=id\n", encoding="utf-8")
    os.chmod(env, 0o600)
    with pytest.raises(ValueError, match="missing"):
        read_credentials(env)
    assert not (tmp_path / "config.yml").exists()


def test_enable_sync_is_separate_explicit_step():
    credentials = {"ZOOM_ACCOUNT_ID": "account", "ZOOM_CLIENT_ID": "id",
                   "ZOOM_CLIENT_SECRET": "secret"}
    config = yaml.safe_load(render_config(credentials, enable_sync=True))
    assert config["server"]["sync_job"] is True
    assert config["server"]["download_job"] is True
    assert config["client"]["trash_downloaded"] is False
