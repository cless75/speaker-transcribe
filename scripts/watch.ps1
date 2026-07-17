<#
.SYNOPSIS
  Run the Audio Inbox watcher: scan the configured sources, transcribe new files,
  write transcripts + state to the outputs declared in the node config.

.DESCRIPTION
  Thin wrapper over src/audio_inbox_watch.py. Use it on a timer (Task Scheduler /
  cron) for a headless node, or run it by hand for a one-off sweep. The watcher is
  config-driven: copy config/node.example.json -> config/node.local.json (and
  config/mapper.example.json -> config/mapper.local.json) and fill in your paths.
  ASCII-only output so the script parses under any console code page.

.EXAMPLE
  .\scripts\watch.ps1 -Once

.EXAMPLE
  .\scripts\watch.ps1 -Config config\node.local.json -MaxFiles 3 -TimeBudgetMinutes 120

.EXAMPLE
  .\scripts\watch.ps1 -CatchUpOnly        # only adopt existing transcripts; no ASR

.EXAMPLE
  .\scripts\watch.ps1 -Once -Pull         # git pull --ff-only (best-effort) before the sweep

.NOTE
  If "running scripts is disabled" -> run once:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
param(
  [string]$Config            = "config\node.local.json",        # node config (gitignored)
  [switch]$Once,                                                # single sweep (default action)
  [switch]$CatchUpOnly,                                         # adopt existing transcripts only
  [int]$MaxFiles             = 0,                               # 0 = no cap
  [int]$TimeBudgetMinutes    = 0,                               # 0 = no budget
  [switch]$ForceWindow,                                         # bypass process_window_local
  [switch]$Pull,                                                # best-effort git pull --ff-only first
  [string]$PythonBin         = "C:\work\venvs\asr\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"      # keep Cyrillic in the log readable

# PowerShell decodes a native command's stdout/stderr using [Console]::OutputEncoding.
# On a RU Windows the OEM default is CP866, so the watcher's UTF-8 log lines (Cyrillic
# filenames) arrive as mojibake when captured through the 2>&1 pipe below. Force UTF-8 so
# the capture decodes correctly. Guarded: a redirected/headless host may reject the setter.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$repo    = Split-Path $PSScriptRoot -Parent
$watcher = Join-Path $repo "src\audio_inbox_watch.py"

if (-not (Test-Path $PythonBin)) { throw "venv python not found: $PythonBin (create venv per docs/node-setup.html)" }
if (-not (Test-Path $watcher))   { throw "watcher not found: $watcher" }

$cfgPath = $Config
if (-not [System.IO.Path]::IsPathRooted($cfgPath)) { $cfgPath = Join-Path $repo $Config }
if (-not (Test-Path $cfgPath)) {
  throw "config not found: $cfgPath  (copy config\node.example.json -> config\node.local.json and fill in your paths)"
}

$watchArgs = @("--config", $cfgPath, "--once")
if ($CatchUpOnly)            { $watchArgs += "--catch-up-only" }
if ($MaxFiles -gt 0)         { $watchArgs += @("--max-files", "$MaxFiles") }
if ($TimeBudgetMinutes -gt 0){ $watchArgs += @("--time-budget-minutes", "$TimeBudgetMinutes") }
if ($ForceWindow)            { $watchArgs += "--force-window" }

# Rule off the start of every sweep so runs are easy to tell apart in the shared log
# (the scheduled task appends each sweep to the same file, back to back).
Write-Host ("=" * 78)
Write-Host ("[{0}] sweep start  (host={1})" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $env:COMPUTERNAME)
Write-Host "Watcher: $watcher" -ForegroundColor Yellow
Write-Host "Config : $cfgPath" -ForegroundColor Yellow

# Native commands (git, python) log progress to STDERR; under $ErrorActionPreference="Stop"
# PowerShell wraps the first stderr line as a terminating ErrorRecord and aborts the sweep
# (a scheduled run, whose output is redirected to a file, then never gets past this header).
# Drop to Continue for the rest of the sweep — the optional git pull AND the watcher call —
# and restore at the end.
$ErrorActionPreference = "Continue"

# Best-effort self-update: pull the latest engine before running so this sweep already uses
# it. --ff-only + gitignored node.local.json / untracked logs mean a clean fast-forward or a
# clean skip; a network/divergence failure is logged and never fails the sweep. Safe on a
# timer: MultipleInstances=IgnoreNew means a new instance (which pulls) can't start while a
# previous sweep is still running, so a pull never lands mid-ASR.
if ($Pull) {
  try {
    $before = (git -C $repo rev-parse --short HEAD 2>$null)
    git -C $repo pull --ff-only 2>&1 | ForEach-Object { $_.ToString() }
    $after = (git -C $repo rev-parse --short HEAD 2>$null)
    if ($before -ne $after) { Write-Host "git pull: $before -> $after" }
    else                    { Write-Host "git pull: up to date ($after)" }
  } catch { Write-Host "git pull skipped (non-fatal): $($_.Exception.Message)" }
}

# Merge stderr into stdout and stringify so lines land verbatim (no "python.exe :" prefix,
# no NativeCommandError noise). $LASTEXITCODE is still the python exit code.
& $PythonBin $watcher @watchArgs 2>&1 | ForEach-Object { $_.ToString() }
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($code -ne 0) { Write-Warning "watcher exit code $code" }
exit $code
