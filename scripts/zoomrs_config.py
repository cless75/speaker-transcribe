#!/usr/bin/env python3
"""Generate a private zoomrs YAML config from a node-local env file.

No credentials are passed on the command line, printed, or copied into the
public speaker-transcribe repository or the Docker build context.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import stat


KEYS = ("ZOOM_ACCOUNT_ID", "ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET")


def read_credentials(path: pathlib.Path) -> dict[str, str]:
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Zoom credentials file must be owner-only (chmod 600)")
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in KEYS or not value.strip():
            raise ValueError("Zoom credentials file must contain only the three nonempty ZOOM_* keys")
        if name in result:
            raise ValueError("duplicate Zoom credential key")
        result[name] = value.strip()
    if set(result) != set(KEYS):
        raise ValueError("Zoom credentials file is missing a required key")
    return result


def render_config(creds: dict[str, str]) -> str:
    q = lambda value: json.dumps(value, ensure_ascii=False)  # JSON strings are valid YAML
    return (f"server:\n  listen: ':8099'\n  domain: localhost\n"
            f"  access_key_salt: {q(secrets.token_hex(32))}\n"
            f"  jwt_secret: {q(secrets.token_hex(32))}\n"
            "  managers: []\n  sync_job: false\n"
            "  download_job: false\n"
            f"client:\n  account_id: {q(creds['ZOOM_ACCOUNT_ID'])}\n"
            f"  id: {q(creds['ZOOM_CLIENT_ID'])}\n"
            f"  secret: {q(creds['ZOOM_CLIENT_SECRET'])}\n"
            "  trash_downloaded: false\n  delete_downloaded: false\n"
            "  delete_skipped: false\n  cloud_capacity_hard_limit: 0\n"
            "  rate_limiting_delay:\n    light: 300\n    medium: 550\n    heavy: 1050\n"
            "storage:\n  type: sqlite\n"
            "  path: 'file:/data/state/zoomrs.db?mode=rwc&_journal_mode=WAL'\n"
            "  repository: /data/recordings\n  keep_free_space: 0\n"
            "syncable:\n  important: ['shared_screen_with_gallery_view']\n"
            "  alternative: ['shared_screen_with_speaker_view', 'active_speaker']\n"
            "  optional: ['audio_transcript', 'chat_file']\n  min_duration: 0\n"
            "commander:\n  instances: ['http://localhost:8099']\n")


def write_private(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError("Zoom configuration directory must be owner-only (chmod 700)")
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    write_private(args.output.expanduser(), render_config(read_credentials(args.env.expanduser())))
    print("Private zoomrs config written; cloud deletion disabled")


if __name__ == "__main__":
    main()
