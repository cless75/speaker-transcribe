# Zoom transcript intake (optional, node-local)

`src/zoom_dispatch.py` reads **only** zoomrs' SQLite tables and its downloaded files.
It does not need Zoom/Google credentials and does not call Zoom APIs. It never
moves or deletes the downloaded MP4: zoomrs' repository must be on local disk.
The hub receives only VTT and (if available) a chat TXT file. This is a separate
process: no zoomrs/GPL code is copied into this repository.

## Prepare

1. Copy `config/zoom_dispatch.example.json` to `config/zoom_dispatch.local.json`
   (gitignored). Set the local paths (`database`, `repository`, `hub_root`,
    `state_file`), mapping rules, fallback project and worker folder. The fallback
    project must not start with `_`: the watcher skips such Hub directories.
    Do not paste credentials into this JSON.
    `approved_meetings` starts empty: no Zoom meeting is imported until its UUID
    is explicitly approved after checking whether it has already been processed
    by an earlier media import. Keep this allowlist local; do not log meeting
    identifiers or titles. If an approved meeting is already in the project index
    as `asr-done` under another filename, add a `legacy_processed` mapping from
    its UUID to `{ "pid": "...", "filename": "original index filename" }`.
    The dispatcher checks that exact completed input and records it as already
    delivered; a stale mapping or wrong project **blocks** delivery. Never infer
    identity from a matching start time alone. Unapproved meetings remain untouched.
    `worker_folder: "iso_week"` routes each meeting to `wN` using its start date's
    ISO week (for example, 2026-10-16 → `w42`); a fixed `wN` is still supported.
    The VTT importer fails closed on malformed cues or timestamps hidden in VTT
    comments/headers, rather than treating a partial transcript as `asr-done`.
    Per `org/zoom-vtt-ambiguous-note-fails-closed`, even a **valid** `NOTE`
    comment starting with a time or numeric ratio (such as `12:30 Meeting begins`
    or `16:9 aspect ratio`) is intentionally held for manual inspection. The
    operator checks the original VTT and corrects a copy before resubmitting;
    no partial transcript is published automatically.
   `container_repository` must match the zoomrs Docker mount path (`/data/recordings`);
   the dispatcher maps SQLite's container paths to the host `repository` and rejects
   anything resolving outside it.
2. Set `"zoom_vtt_only_input": true` in `config/node.local.json` on the **receiving**
   node. Other nodes keep their current scan behaviour. VTT beside a matching
   media source remains its sidecar; only standalone VTT creates a new run.
3. Ensure the rclone Hub mount is active. The dispatcher refuses to deliver if
   `hub_root` is absent or not a mount point (`require_hub_mount: true`). Keep
   this guard on in production; it prevents accidental writes under an unmounted
   `/home/.../hub` directory.
4. Keep `max_meetings_per_run` low (3 by default) during backlog intake. An
   incomplete zoomrs record, missing file, size mismatch or young file waits
   for a later tick. Multiple VTT/chat records per meeting are deliberately
   held for manual inspection rather than silently dropping one.

Check selection without writing files:

```sh
python3 src/zoom_dispatch.py --config config/zoom_dispatch.local.json --dry-run
```

After reviewing the routes/fallback, run without `--dry-run`. The local manifest
(`state_file`) tracks delivered Zoom meeting UUIDs. A chat file is copied first
and the VTT last, using a temporary name and atomic rename. The watcher's VTT
path produces `-transcript.md`, `state.json` (`asr-done`) and the usual
`_sessions-index.md`. A crash after copying but before saving the manifest
can be retried without overwriting a different file; an existing processed
entry in `_sessions-index.md` is also treated as delivered.

## Timer (operator / sudo)

`scripts/systemd/zoom-dispatch@.service` and `.timer` assume the repo lives at
`/home/<user>/speaker-transcribe`, Python at `/usr/bin/python3`, and the local
configuration is ready. The operator installs both units under
`/etc/systemd/system/`, runs `systemctl daemon-reload`, then enables and starts
`zoom-dispatch@<user>.timer`. Do not enable before inspecting paths and testing
one dry run. The operator (not the agent) performs the privileged step.

Zoom app creation, credentials and the cloud-backlog size check are **separate**.
Before any cloud cleanup, verify every copied recording and keep zoomrs
`trash_downloaded`, `delete_downloaded` and `delete_skipped` all `false`.

`scripts/zoomrs_config.py` turns the owner's private `zoomrs.env` (Account ID,
Client ID, Client Secret) into a 600-only `config.yml` under `~/.config/zoomrs/`.
The generated file **always** disables sync/download. It also sets
`keep_free_space: 0`, which disables zoomrs' local auto-eviction in our separate
patched GPL clone; this must not be used with an unpatched upstream image.
Only after measuring the backlog and agreeing a local-disk batch limit may an
operator enable controlled per-day downloads. It enables VTT/chat types and
keeps all three cloud deletion flags false. Do not
paste the credential values into a CLI argument, a log or this repository.
The video selection includes `active_speaker` as a fallback: some Zoom meetings
have only this MP4 type, not either of the `shared_screen_*` variants. When Zoom
did not record a `chat_file`, the dispatcher imports the VTT alone and does not
invent a chat transcript.
The optional `scripts/systemd/zoomrs@.service` binds its web API to localhost
and mounts this private YAML read-only. The operator must verify the hardcoded
image tag and paths and perform the `sudo` install; it is not enabled by default.
