<#
.SYNOPSIS
  Install (or remove) a Windows Scheduled Task that runs the watcher continuously.

.DESCRIPTION
  Registers a task that runs scripts\watch.ps1 -Once at logon and then repeats
  every N minutes indefinitely. Runs only while the user is logged on (so the
  Google Drive Hub is mounted). Overlap is prevented (IgnoreNew + the watcher's
  own host-lock); a failed run is retried. Output is appended to a log file.

  Paths are auto-detected from the script location, so the repo can live anywhere.
  ASCII-only so it parses under any console code page.

.PARAMETER TaskName
  Scheduled task name. Default: speaker-transcribe-watch

.PARAMETER IntervalMinutes
  Minutes between sweeps. Default: 10

.PARAMETER Config
  Path to the node config. Default: <repo>\config\node.local.json

.PARAMETER PythonBin
  Optional venv python forwarded to watch.ps1 (if omitted, watch.ps1 default is used).

.PARAMETER LogDir
  Directory for watch.log. Default: <repo>\logs

.PARAMETER Remove
  Remove the task instead of installing it.

.EXAMPLE
  .\scripts\install-watch-task.ps1

.EXAMPLE
  .\scripts\install-watch-task.ps1 -IntervalMinutes 5 -PythonBin C:\work\venvs\asr\Scripts\python.exe

.EXAMPLE
  .\scripts\install-watch-task.ps1 -Remove

.NOTE
  If "running scripts is disabled" -> run once:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  Or double-click install-watch-task.cmd (it sets Bypass for you).
#>
param(
  [string]$TaskName        = "speaker-transcribe-watch",
  [int]$IntervalMinutes    = 10,
  [string]$Config          = "",
  [string]$PythonBin       = "",
  [string]$LogDir          = "",
  [switch]$Remove
)

$ErrorActionPreference = "Stop"

$repo    = Split-Path $PSScriptRoot -Parent
$watcher = Join-Path $repo "scripts\watch.ps1"

if ($Remove) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Yellow
  } else {
    Write-Host "Task not found: $TaskName (nothing to remove)" -ForegroundColor DarkGray
  }
  return
}

if (-not (Test-Path $watcher)) { throw "watcher not found: $watcher" }

# Fail before doing any work: registering a task needs elevation, and the failure
# it produces otherwise is a non-terminating CIM error that reads like a success.
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  throw ("registering a scheduled task needs an elevated shell. Start PowerShell via " +
         "'Run as administrator' and re-run this script, or double-click scripts\install-watch-task.cmd " +
         "(it prompts for elevation). The watcher itself runs unprivileged.")
}

if (-not $Config)  { $Config  = Join-Path $repo "config\node.local.json" }
if (-not $LogDir)  { $LogDir  = Join-Path $repo "logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logFile = Join-Path $LogDir "watch.log"

if (-not (Test-Path $Config)) {
  Write-Warning "config not found yet: $Config"
  Write-Warning "Copy config\node.example.json -> config\node.local.json and fill it in before the first run."
}

# Build the watch.ps1 invocation (forward -Config and optional -PythonBin), append all streams to the log.
$inner = "& '$watcher' -Once -Config '$Config'"
if ($PythonBin) { $inner += " -PythonBin '$PythonBin'" }
$inner += " *>> '$logFile'"
$argument = "-NoProfile -ExecutionPolicy Bypass -Command `"$inner`""

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument

# At logon + repeat every N minutes indefinitely.
# NOTE: do NOT set -RepetitionDuration to [TimeSpan]::MaxValue -> Task Scheduler
# rejects it ("Duration out of range"). Omitting the duration = repeat forever.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)).Repetition

# Run only while the user is logged on (so the Google Drive Hub is mounted).
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -DontStopOnIdleEnd

# -ErrorAction Stop: Register-ScheduledTask reports failure (e.g. Access Denied) as a
# NON-terminating CIM error, which $ErrorActionPreference does not catch — without this
# the script printed "Installed" over a task that was never created.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings `
  -Description "Audio Inbox watcher node (every $IntervalMinutes min, logged-on session)" `
  -Force -ErrorAction Stop | Out-Null

# Trust the registry, not the return: confirm the task is really there before saying so.
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
  throw "task did not register: $TaskName (no error raised, but it is not in the scheduler)"
}

Write-Host "Installed scheduled task: $TaskName" -ForegroundColor Green
Write-Host "  repo     : $repo"
Write-Host "  config   : $Config"
Write-Host "  interval : every $IntervalMinutes min (at logon, then repeating)"
Write-Host "  log      : $logFile"
Write-Host ""
Write-Host "Run now:    Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Watch log:  Get-Content '$logFile' -Tail 30 -Wait"
Write-Host "Remove:     .\scripts\install-watch-task.ps1 -Remove"
