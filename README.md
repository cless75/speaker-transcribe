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
docs/       node-setup.html    (full setup guide)
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

## License

[MIT](LICENSE) © 2026 Dmitry Bezuglyi
