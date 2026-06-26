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
  [string]$PythonBin         = "C:\work\venvs\asr\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

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

Write-Host "Watcher: $watcher" -ForegroundColor Yellow
Write-Host "Config : $cfgPath" -ForegroundColor Yellow
& $PythonBin $watcher @watchArgs
$code = $LASTEXITCODE
if ($code -ne 0) { Write-Warning "watcher exit code $code" }
exit $code
