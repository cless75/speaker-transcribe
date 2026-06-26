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
- Two usage modes: **from an AI agent** (MCP server) and **from the command line**
- Multi-node coordination via a shared hub (claim + merge-union, no fragile locks)

## Quickstart

Open **[`docs/node-setup.html`](docs/node-setup.html)** in a browser — a
self-contained, step-by-step setup guide (requirements → CUDA → Python env →
tokens → config → run). Both usage modes (agent / CLI) are covered there.

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
  merge_speaker_tracks.py    merge per-speaker tracks (Zoom multi-track)
  apply_speaker_identities.py post-ASR voiceprint binding
scripts/    run-media-transcribe-direct.ps1  (PowerShell wrapper)
config/     node.example.json  (copy to node.local.json)
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

## Status

Work in progress. Landed: setup docs, project scaffolding, **engine core**
(`src/`, CLI, PowerShell wrapper). Pending: watcher / hub orchestration layer,
MCP server, voiceprint-in-hub, English docs.

## License

[MIT](LICENSE) © 2026 Dmitry Bezuglyi
