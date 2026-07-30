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

# Гарантируем папку логов: если её нет, редирект задачи ( *>> logs\watch.log ) не создаёт
# родительский каталог и лог не появляется вовсе. Плюс — пишем ранние фатальные ошибки
# (venv/конфиг не найден, запуск под другим юзером = Error 103) ПРЯМО в watch.log, чтобы
# они не терялись, даже если редирект задачи не сработал.
$logDir  = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { try { New-Item -ItemType Directory -Force -Path $logDir | Out-Null } catch {} }
$selfLog = Join-Path $logDir "watch.log"
function Write-Fatal($msg) {
  $line = "[{0}] FATAL: {1}  (user={2}, PythonBin={3})" -f `
    (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg, $env:USERNAME, $PythonBin
  try { Add-Content -Path $selfLog -Value $line -Encoding UTF8 } catch {}
  Write-Host $line -ForegroundColor Red
}

# Engine version = the semver in ./VERSION plus the exact commit, e.g. "v1.0.0+g9cfbff3".
# The semver is bumped on releases; the commit hash always reflects the running code, so a
# self-update shows up even between version bumps. Guarded so a missing VERSION / no-git
# checkout never breaks a sweep.
function Get-EngineVersion {
  $ver = "0.0.0"
  try {
    $vf = Join-Path $repo "VERSION"
    if (Test-Path $vf) { $ver = ((Get-Content $vf -Raw) -replace '\s','') }
  } catch {}
  try {
    $hash = (git -C $repo rev-parse --short HEAD 2>$null)
    if ($hash) { return "v$ver+g$hash" }
  } catch {}
  return "v$ver"
}

# Пре-флайт: каждая ошибка пишется в watch.log (Write-Fatal) и даёт distinctive exit 103,
# чтобы «тихий» сбой задачи (напр. запуск под другим юзером без venv/PATH) был виден в логе.
if (-not (Test-Path $PythonBin)) {
  Write-Fatal "venv python не найден: $PythonBin — venv не создан или задача под другим юзером (create venv per docs/node-setup.html)"
  exit 103
}
if (-not (Test-Path $watcher)) { Write-Fatal "watcher не найден: $watcher (нужен git pull)"; exit 103 }

$cfgPath = $Config
if (-not [System.IO.Path]::IsPathRooted($cfgPath)) { $cfgPath = Join-Path $repo $Config }
if (-not (Test-Path $cfgPath)) {
  Write-Fatal "config не найден: $cfgPath (скопируй config\node.example.json -> config\node.local.json)"
  exit 103
}

$watchArgs = @("--config", $cfgPath, "--once")
if ($CatchUpOnly)            { $watchArgs += "--catch-up-only" }
if ($MaxFiles -gt 0)         { $watchArgs += @("--max-files", "$MaxFiles") }
if ($TimeBudgetMinutes -gt 0){ $watchArgs += @("--time-budget-minutes", "$TimeBudgetMinutes") }
if ($ForceWindow)            { $watchArgs += "--force-window" }

# Rule off the start of every sweep so runs are easy to tell apart in the shared log
# (the scheduled task appends each sweep to the same file, back to back).
Write-Host ("=" * 78)
Write-Host ("[{0}] sweep start  (host={1}, user={2}, engine {3})" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $env:COMPUTERNAME, $env:USERNAME, (Get-EngineVersion))
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
    $vBefore = Get-EngineVersion
    git -C $repo pull --ff-only 2>&1 | ForEach-Object { $_.ToString() }
    $vAfter = Get-EngineVersion
    if ($vBefore -ne $vAfter) { Write-Host "engine updated: $vBefore -> $vAfter" }
    else                      { Write-Host "engine up to date: $vAfter" }
  } catch { Write-Host "engine self-update skipped (non-fatal): $($_.Exception.Message)" }
}

# Merge stderr into stdout and stringify so lines land verbatim (no "python.exe :" prefix,
# no NativeCommandError noise). $LASTEXITCODE is still the python exit code.
& $PythonBin $watcher @watchArgs 2>&1 | ForEach-Object { $_.ToString() }
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($code -ne 0) { Write-Warning "watcher exit code $code" }
exit $code
