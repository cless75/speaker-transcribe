# Deploying a node

How to turn a machine into an unattended **speaker-transcribe** node: it watches
the sources you declare, runs ASR + diarization, and writes results to your hub —
continuously, surviving reboots.

This is the operational guide. For the one-time heavy install (CUDA / cuDNN /
ffmpeg / Python), see [`node-setup.html`](node-setup.html).

> **Code is public, data is private.** Everything below uses placeholders. Your
> real paths, tokens and hub live only in `*.local.json` (gitignored) and your
> machine's environment — never in the repo.

---

## 1. Prerequisites

- **Python 3.11** (the tested version — newer minors may lack wheels for some deps).
- **ffmpeg** on `PATH`.
- **GPU (optional):** NVIDIA + CUDA for fast ASR. Without it the engine runs on CPU.
- **A shared hub** mounted as a local path — e.g. Google Drive for Desktop, a
  network share, or just a local folder for a single-node setup.
- **A Hugging Face token** (gated pyannote models) — only needed for diarization.

## 2. Get the engine

```bash
git clone https://github.com/cless75/speaker-transcribe.git
cd speaker-transcribe

# create a venv ON THIS MACHINE (venvs are NOT portable — never copy one
# between machines; the base-interpreter path is hard-coded inside it)
py -3.11 -m venv C:/work/venvs/asr            # Windows
# python3.11 -m venv ~/venvs/asr              # macOS/Linux

C:/work/venvs/asr/Scripts/python.exe -m pip install --upgrade pip
C:/work/venvs/asr/Scripts/python.exe -m pip install -r requirements.txt
```

Verify the GPU is visible (optional):

```bash
C:/work/venvs/asr/Scripts/python.exe -c "import torch; print('cuda:', torch.cuda.is_available())"
```

`cuda: False` on a GPU machine → torch installed as a CPU build; reinstall it from
the CUDA index (see `node-setup.html`).

## 3. Configure

```bash
cp config/node.example.json   config/node.local.json
cp config/mapper.example.json config/mapper.local.json
```

Edit `config/node.local.json` — the fields that matter most:

| Field | Set to |
|---|---|
| `node.host_label` | a **unique** name per machine (used for claim attribution) |
| `node.cache_root` | a local path (NOT on the cloud drive) |
| `transcribe_python` | the venv python from step 2 |
| `runtime.device` / `compute_type` | `cuda`/`float16` with a GPU, else `cpu`/`int8` |
| `hub_root` | your mounted hub path (check the drive letter / mount point) |
| `sources[].root` + how to scan | see source modes below |
| `enable_multi_machine` + `sources[].claim` | `true` when several nodes share one hub |
| `secrets.hf_token` | `env:HF_TOKEN` (set the variable, see §6) |

**Source modes** — a source is either a plain folder or an auto-discovering hub:

- **Flat:** `{ "root": "{hub_root}/_inbox", "route": "mapper", "claim": true }` — scans
  one folder; `pid` comes from `mapper.local.json` (deepest prefix wins, then
  `_default`, then the folder name).
- **Project inboxes (hub layout):** `{ "root": "{hub_root}", "discover": "project-inboxes", "claim": true }`
  — auto-scans `{hub_root}/_inbox`, every `{hub_root}/<pid>/_<pid>_inbox/`, and
  top-level files in `{hub_root}/<pid>/`; `pid` is the `<pid>` folder name. New
  project folders are picked up automatically (no config edit). Outputs
  (`sessions/`) and meta dirs (`_meta`, `_voiceprints`, …) are skipped.

## 4. First run (smoke test)

Single file, no hub, no token — fastest way to confirm the engine works:

```bash
python src/media_transcribe_cli.py --input path/to/test.m4a --output-dir out \
    --model small --speaker-mode off --timestamps both
# add --device cuda --compute-type float16 on a GPU; cpu/int8 otherwise
```

Then one watcher sweep over the configured sources:

```bash
python src/audio_inbox_watch.py --config config/node.local.json --once
# Windows:  powershell -ExecutionPolicy Bypass -File scripts/watch.ps1 -Once
```

A processed file lands as `{hub_root}/{pid}/sessions/{YYYY-MM}/{sid}/transcripts/…`
with `pipeline/state.json` (`status: asr-done`) and sidecars next to the source.

## 5. Run continuously

The watcher does one sweep per invocation (drains the queue, exits). Run it on a
timer — overlap is safe (a host-local lock + per-file claim).

### Windows — Scheduled Task (one command)

```powershell
# registering a task needs an elevated shell (admin); the watcher itself runs unprivileged
.\scripts\install-watch-task.ps1                 # every 10 min, at logon, logged-on session
.\scripts\install-watch-task.ps1 -IntervalMinutes 5 -PythonBin C:\work\venvs\asr\Scripts\python.exe
.\scripts\install-watch-task.ps1 -Remove
```

Or double-click `scripts\install-watch-task.cmd` (it sets ExecutionPolicy Bypass
and prompts for elevation). Logs go to `<repo>\logs\watch.log`.

### macOS — launchd

`~/Library/LaunchAgents/com.speaker-transcribe.watch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.speaker-transcribe.watch</string>
  <key>ProgramArguments</key><array>
    <string>/Users/you/venvs/asr/bin/python</string>
    <string>/path/to/speaker-transcribe/src/audio_inbox_watch.py</string>
    <string>--config</string><string>/path/to/speaker-transcribe/config/node.local.json</string>
    <string>--once</string>
  </array>
  <key>StartInterval</key><integer>600</integer>
  <key>StandardErrorPath</key><string>/path/to/logs/watch.log</string>
  <key>StandardOutPath</key><string>/path/to/logs/watch.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.speaker-transcribe.watch.plist
```

> **macOS gotcha:** a launchd process runs in its own TCC sandbox. To read a
> Google Drive `CloudStorage` path it needs **Full Disk Access** granted to the
> python binary (System Settings → Privacy & Security → Full Disk Access).
> Without it the scan silently sees nothing — the watcher prints a hint about this.

### Linux — cron

```cron
*/10 * * * * /home/you/venvs/asr/bin/python /path/speaker-transcribe/src/audio_inbox_watch.py --config /path/config/node.local.json --once >> /path/logs/watch.log 2>&1
```

## 6. Survive reboots

So the node comes back **by itself** after a restart, three things must be stable
across reboots and independent of which user logs in:

1. **Secrets at machine scope, not per-user.** A user-scope env var disappears if a
   different account logs in. Set it machine-wide (admin shell):
   ```powershell
   [Environment]::SetEnvironmentVariable("HF_TOKEN", "<token>", "Machine")   # Windows
   ```
   On macOS/Linux put `HF_TOKEN` in a system profile / the launchd plist
   `EnvironmentVariables`, not just your shell rc.
2. **Auto-login the node's user.** The scheduled task and the cloud-drive mount are
   per-user; pin auto-login to the account that owns them (Windows: Sysinternals
   **Autologon** — LSA-encrypted — or `netplwiz`).
3. **Cloud drive on startup.** Enable "launch on system startup" so the hub is
   mounted before the first sweep, and confirm the mount path matches `hub_root`.
4. **Task owned by that user** (the installer registers it for the current user at
   logon). Verify with `Get-ScheduledTask`.

> Auto-login means physical access = session access — fine for a dedicated node,
> but a conscious trade-off.

## 7. Multiple nodes

Point several machines at the same hub and set `enable_multi_machine: true` with
`claim: true` on the source. Each node:

- needs a **unique** `node.host_label`;
- writes a per-file `<file>.claim.json` (lease + heartbeat) so exactly one node
  processes each file — resilient to cloud-drive sync lag (no fragile locks);
- can be capability-gated: set `required_capabilities` and list the node's
  `capabilities` so, e.g., only GPU nodes pick up heavy jobs.

`processed_by_host` in the state records which node did the work.

## 8. Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `No Python at '…'` / exit 103 | venv copied from another machine → **recreate the venv locally** (§2) |
| `running scripts is disabled` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or run via `-ExecutionPolicy Bypass` |
| `Register-ScheduledTask: Access Denied (0x80070005)` | run the installer from an **elevated** shell / "Run as administrator" |
| `Duration … P99999999…` registering a task | don't use `[TimeSpan]::MaxValue` for repetition — omit the duration (the installer already does) |
| `no sources configured` | `node.local.json` has no non-empty `sources` array (or it's the wrong file) |
| `config not found` | copy `node.example.json` → `node.local.json` |
| Scan finds nothing despite files | wrong `hub_root` / drive letter; or (macOS) missing Full Disk Access |
| `cuda: False` on a GPU box | torch is a CPU build → reinstall from the CUDA index |
| Env vars gone after reboot | they were per-user and another account logged in → set machine-scope + auto-login (§6) |

---

See also: [`README.md`](../README.md) · [`node-setup.html`](node-setup.html) ·
`config/node.example.json` · `scripts/watch.ps1` · `scripts/install-watch-task.ps1`
