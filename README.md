# speaker-transcribe

Local-first **speech recognition (ASR) + speaker diarization** with per-project
voiceprint. Runs as a headless node: reads sources, transcribes on the GPU,
identifies speakers, and writes results to a shared hub.

Built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2)
and [pyannote.audio](https://github.com/pyannote/pyannote-audio).

## Features

- GPU ASR (CUDA) with chunking and timestamps
- Speaker diarization (pyannote)
- Per-project voiceprint: a global registry of voices, projected per project,
  cached locally on each node
- Three usage modes: **AI agent** (MCP), **command line**, and **local folder** (manual drop + batch)
- Multi-node coordination via a shared hub (claim + merge-union, no fragile locks)

## Quickstart

The full setup guide lives in **[`docs/node-setup.html`](docs/node-setup.html)** — a
self-contained, step-by-step walkthrough (requirements → CUDA → Python env → tokens →
config → run), covering all three usage modes (agent / CLI / local folder).

GitHub shows the `.html` file as source, not a rendered page. To read it as a page:

- **Rendered (quick):** <https://raw.githack.com/cless75/speaker-transcribe/main/docs/node-setup.html>
- **GitHub Pages:** <https://cless75.github.io/speaker-transcribe/docs/node-setup.html> (once Pages is enabled)
- **Local:** clone the repo and open `docs/node-setup.html` in a browser

> The setup guide is currently in Russian; an English version is planned.

## Install with an AI agent (Claude Code / Codex)

Open the repo in a coding agent (Claude Code, Codex, or Cursor) and paste the prompt
below. The agent uses **[`docs/deployment.md`](docs/deployment.md)** as the source of
truth and sets the node up interactively — it **asks you** for the machine-specific
values (hub path, node name, Hugging Face token) instead of guessing, and verifies the
GPU before finishing.

```text
You are setting up a speaker-transcribe ASR + speaker-diarization node on THIS machine.
Treat docs/deployment.md as the source of truth; read it first. Never copy a venv from
another machine (the base-interpreter path is baked in). Work step by step and STOP to
ask me before anything destructive.

1. Check prerequisites and report gaps: OS, NVIDIA GPU + CUDA 12.8, Python 3.11, ffmpeg,
   RAM. Do not proceed past a missing hard requirement without telling me.
2. Create a LOCAL venv and install requirements.txt, with torch + torchaudio from the
   CUDA index (cu128). On Windows you may use: scripts/update-node.ps1 -CudaTorch.
3. Copy config/node.example.json -> config/node.local.json and
   config/mapper.example.json -> config/mapper.local.json. ASK me for: hub_root (the
   shared Hub path), host_label (a unique node name), cache_root, and the Hugging Face
   token (gated pyannote; exported as HF_TOKEN). Set runtime.device=cuda,
   runtime.diarization_device=cuda, voiceprint_mode=match_enroll.
4. Run diagnostics (scripts/node_diagnostics.py, or update-node.ps1) and show me:
   torch.cuda True + GPU name, faster-whisper CUDA OK, speechbrain + torchaudio present,
   ffmpeg found, config valid, git commit.
5. Smoke-test one short audio file (docs/deployment.md section 4) and show the transcript
   with speaker labels.
6. Register the watcher to run continuously (Windows: scripts/install-watch-task.ps1 from
   an ELEVATED shell; macOS: launchd; Linux: cron — see deployment.md section 5). Confirm
   it is registered and alive.

Finish with a short pass/fail checklist of every step.
```

**Codex / Cursor:** the same prompt applies — approve the venv creation, dependency
install, and diagnostics commands when the agent asks. On Codex, run it from the repo
root so it can read `docs/deployment.md` and the `scripts/` and `config/` folders.

## Requirements (short)

- Windows 10/11 (Linux/macOS paths differ), NVIDIA GPU with CUDA 12.8, 16 GB+ RAM
- Python 3.11
- `torch` installed from the CUDA index (see `requirements.txt`), cuDNN 9, ffmpeg
- A Hugging Face token (gated pyannote models) — see the setup guide

## Privacy

**Code is public, data is private.** The engine does not collect or transmit your
data. Voiceprints are biometric personal data and are **never** committed — they
live only in your private hub. See **[PRIVACY.md](PRIVACY.md)**.

## Repository layout

```
src/        engine — ASR + diarization + voiceprint
  media_transcribe.py        core worker (faster-whisper + pyannote + ECAPA-TDNN)
  media_transcribe_cli.py    CLI for a single file
  audio_inbox_watch.py       watcher — scan sources, state machine, dispatch ASR
  merge_speaker_tracks.py    merge per-speaker tracks (Zoom multi-track)
  apply_speaker_identities.py post-ASR voiceprint binding
scripts/    transcribe.ps1   (single file / folder)   watch.ps1  (run the watcher)
            run-media-transcribe-direct.ps1  (low-level PowerShell wrapper)
config/     node.example.json   mapper.example.json   (copy to *.local.json)
docs/       node-setup.html  (install)   deployment.md  (operate)   speaker-recognition.md  (diarization + voiceprints)
            slide-frames.md  (screenshots + local OCR of slide text)
```

## Engine quickstart (CLI)

```bash
python src/media_transcribe_cli.py \
    --input  C:/work/recordings/meeting.m4a \
    --output-dir C:/work/output/my-project \
    --model medium --speaker-mode on --timestamps both
```

Requires `HF_TOKEN` for diarization (gated pyannote models) — see the setup guide.

## Watcher (headless node)

The watcher turns a machine into an unattended ASR node: it scans the **sources**
you declare, runs ASR on each new recording, and writes transcripts + state to the
**outputs** templates — resolving `{hub_root}` / `{pid}` / `{sid}` / `{YYYY-MM}` at
runtime, so the repo carries no personal absolute paths.

```bash
cp config/node.example.json   config/node.local.json     # fill in your paths
cp config/mapper.example.json config/mapper.local.json   # folder -> project id

python src/audio_inbox_watch.py --config config/node.local.json --once
# Windows:  .\scripts\watch.ps1 -Once
```

Run it on a timer (Task Scheduler / launchd / cron) for continuous pickup. On
Windows, `scripts\install-watch-task.ps1` (or double-click
`scripts\install-watch-task.cmd`) registers a scheduled task that sweeps every N
minutes while you are logged on — `-Remove` uninstalls it. Key behaviour, all
config-driven:

- **State machine** per file via a sidecar `*.state.json`: `queued -> in-progress
  -> asr-done` (with retry / `_failed/` on repeated errors).
- **CPU-aware**: defers ASR while the machine is busy (`respect_cpu_load`).
- **Waits for the hub to mount** (`hub_wait_seconds`, `hub_wait_poll_sec`): the task
  fires at logon, a cloud drive appears seconds-to-minutes later — a sweep in that
  window would otherwise find no source root and do nothing.
- **Multi-node** (opt-in `enable_multi_machine`): per-file claim-and-verify with a
  lease + heartbeat — several nodes share one hub without fragile locks.
- **Zoom bundles**: a multi-file meeting export is transcribed once (primary), its
  siblings are tracked but skipped.
- **Obsidian session card** is an *optional* output adapter
  (`outputs.session_card.adapter`, default `none`) — the core is vault-agnostic.

## Status

Work in progress. Landed: setup docs, project scaffolding, **engine core**
(`src/`, CLI, PowerShell wrapper), **watcher / orchestration layer**
(`src/audio_inbox_watch.py`, `scripts/watch.ps1`). Pending: MCP server,
voiceprint-in-hub, English docs.

### Drive-first intake (opt-in foundation)

`src/hub_intake.py` lists every project directly through rclone's Drive backend,
including completed projects and the shared `Hub/_inbox`. It does **not** depend on recursively walking the
FUSE mount. It can produce a local `index.html` for each project and a
`hub-hygiene.json` report, or download one selected media revision to local disk
with a verified size and Drive MD5. A failed listing is reported as **incomplete**,
never as an empty queue. Legacy `asr-done` markers are labeled "publication not
verified" until their output is independently checked. Marker reads are batched
into a node-local cache, checked against Drive MD5; the report is saved after
**each** project so a long whole-Hub run can resume using `--after PROJECT` or
`--through PROJECT`. Nothing is deleted from the Hub.

```bash
python src/hub_intake.py --remote gdrive:Hub \
  --rclone-config /home/clessd/.config/rclone/rclone.conf \
  --output-dir /home/clessd/hub-intake-status
# Optional explicit, single-file delivery (does NOT start ASR):
python src/hub_intake.py --remote gdrive:Hub \
  --rclone-config /home/clessd/.config/rclone/rclone.conf \
  --output-dir /home/clessd/hub-intake-status \
  --stage 'Example-Course/Lessons/new-recording.m4a'
```

Use `--publish` only after reviewing the result: it writes generated `index.html`
into each project root, refusing to overwrite unrelated HTML. HTML in Drive is
**not** a web host; it inherits the folder's Drive permissions and may need to be
downloaded to open in a browser. A separate sign-in-only website still needs
an approved authentication/hosting endpoint.

`src/hub_worker.py` is an **explicit one-file E2E canary**, not a replacement
timer. It stages an immutable Drive revision locally, creates its transcript
locally (ASR or opt-in Zoom VTT-only), uploads the complete session, verifies
every published output by size and MD5, then publishes `asr-done` LAST. It
groups timestamped Zoom media with video/VTT peers and uses the watcher's
shared claim and heartbeat (when `enable_multi_machine` is on). Progress
markers and the affected project's HTML follow copying → ASR → publication.
A local journal resumes an interrupted copy or publication without re-running
successful ASR. It refuses to run while the
old systemd watcher is active. The single exception is `--isolated-vtt-canary`:
only a `.vtt` in a `_test*` folder, and only after verifying the **actual** old
watcher config cannot discover VTT. Old media in that folder is *not* isolated.
Standalone VTTs without markers have no age cutoff: a separately approved
historical backlog can be imported after cutover, with the risk of re-importing
material previously processed outside this watcher.

```bash
# No writes: inspect one candidate, or inspect the whole Hub queue.
python3 src/hub_worker.py --project PROJECT --file recording.m4a \
  --config config/node.local.json --rclone-config /path/to/rclone.conf \
  --local-root /local/asr-work
python3 src/hub_worker.py --all --config config/node.local.json \
  --rclone-config /path/to/rclone.conf --local-root /local/asr-work

# Only after the old consumer is off, use --run for an explicit file.
# For an isolated VTT test use --isolated-vtt-canary and --legacy-config.

# Report locally eligible copies without deleting them. --run deletes ONLY
# local inputs/outputs older than 30 days after verified publication.
python3 src/hub_worker.py --cleanup-local --config config/node.local.json \
  --rclone-config /path/to/rclone.conf --local-root /local/asr-work
```

**Whole-Hub `--all --run` is gated, not currently enabled.** Before production cutover,
verify peer watchers are off, test a real media bundle as well as VTT-only,
coordinate the single writer, deploy the code through the reviewed `release`
branch, and switch the old timer under an operator-controlled rollback window.
The current old watcher remains the live consumer; no new worker timer is installed.
The optional whole-Hub HTML refresh timer should not run alongside a future
worker's event-driven status writes without a single-writer arbitration rule.

`scripts/systemd/hub-worker@.*` is an **operator-only, disabled template** for
that future window. It runs at most three verified sources per sweep and refuses
to execute until a real-media E2E receipt from the *same worker code and node*
is less than 24 hours old. It also refuses an active legacy timer or missing
shared Hub mount/claim. Do not install it during a VTT-only pilot.

Optional six-hour refresh units live in `scripts/systemd/hub-intake-status@.*`.
They are **not installed automatically**: a node operator must review paths,
publish the tested script through the repo, install the units and enable the
timer. The unit does not require a FUSE mount for discovery and does not run ASR.

## License

[MIT](LICENSE) © 2026 Dmitry Bezuglyi
