<#
.SYNOPSIS
  Health check for a node: is the watcher alive, is the hub reachable, is the queue moving?

.DESCRIPTION
  One command that answers "is this node actually working?" instead of the
  scattered checks in docs/deployment.md. Reports a table of checks and exits
  with a code you can alert on:

    0 = OK      every check passed
    1 = WARN    degraded (e.g. log went stale, a claim expired)
    2 = FAIL    not working (task missing, hub unreachable)

  Checks: scheduled task registered + last result; watch.log freshness (a live
  node appends every sweep, even outside process_window_local); a sweep running
  right now; hub_root reachable; HF_TOKEN present when diarization is on; queue
  status from the *.state.json sidecars + expired claims.

  Paths are auto-detected from the script location, so the repo can live
  anywhere. ASCII-only so it parses under any console code page.

.PARAMETER TaskName
  Scheduled task name. Default: speaker-transcribe-watch

.PARAMETER Config
  Path to the node config. Default: <repo>\config\node.local.json

.PARAMETER LogDir
  Directory holding watch.log. Default: <repo>\logs

.PARAMETER MaxLogAgeMinutes
  Stale-log threshold. Default 0 = derive from the task interval (3 sweeps,
  never less than 30 min).

.PARAMETER Json
  Emit the checks as JSON (for a monitor / scheduled alert) instead of a table.

.EXAMPLE
  .\scripts\watch-health.ps1

.EXAMPLE
  .\scripts\watch-health.ps1 -Json            # machine-readable, exit code = worst status

.NOTE
  If "running scripts is disabled" -> run once:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
param(
  [string]$TaskName         = "speaker-transcribe-watch",
  [string]$Config           = "",
  [string]$LogDir           = "",
  [int]$MaxLogAgeMinutes    = 0,
  [switch]$Json
)

$ErrorActionPreference = "Stop"

$repo = Split-Path $PSScriptRoot -Parent
if (-not $Config) { $Config = Join-Path $repo "config\node.local.json" }
if (-not $LogDir) { $LogDir = Join-Path $repo "logs" }
$logFile = Join-Path $LogDir "watch.log"

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$Name, [string]$Status, [string]$Detail) {
  $checks.Add([pscustomobject]@{ Check = $Name; Status = $Status; Detail = $Detail })
}

# --- config -----------------------------------------------------------------
$cfg = $null
if (Test-Path $Config) {
  try {
    $cfg = Get-Content $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    Add-Check "config" "OK" $Config
  } catch {
    Add-Check "config" "FAIL" "unparseable: $Config ($($_.Exception.Message))"
  }
} else {
  Add-Check "config" "FAIL" "not found: $Config (copy node.example.json -> node.local.json)"
}

# --- scheduled task ---------------------------------------------------------
# Task Scheduler codes: 0 = last sweep ok, 267009 = running now, 267011 = never run.
$intervalMin = 0
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
  Add-Check "task" "FAIL" "not registered: $TaskName (run scripts\install-watch-task.ps1)"
} else {
  $info = Get-ScheduledTaskInfo -TaskName $TaskName
  $user = $task.Principal.UserId
  $rep  = $task.Triggers | ForEach-Object { $_.Repetition.Interval } | Where-Object { $_ } | Select-Object -First 1
  if ($rep) {
    try { $intervalMin = [int][System.Xml.XmlConvert]::ToTimeSpan($rep).TotalMinutes } catch { $intervalMin = 0 }
  }
  $last = if ($info.LastRunTime) { $info.LastRunTime.ToString("yyyy-MM-dd HH:mm") } else { "never" }
  $next = if ($info.NextRunTime) { $info.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "none" }
  $detail = "state=$($task.State) user=$user interval=${intervalMin}m last=$last next=$next result=$($info.LastTaskResult)"

  if ($task.State -eq "Disabled") {
    Add-Check "task" "FAIL" "disabled -- $detail"
  } elseif ($info.LastTaskResult -eq 267011) {
    Add-Check "task" "WARN" "never ran yet -- $detail"
  } elseif ($info.LastTaskResult -eq 0 -or $info.LastTaskResult -eq 267009) {
    Add-Check "task" "OK" $detail
  } else {
    # The watcher itself exits 0 (ok) or 2 (no config) — any other code came from
    # watch.ps1 throwing (venv python / watcher / config not found). See the log tail.
    Add-Check "task" "WARN" "last run failed -- $detail"
  }

  # A registered task is not a scheduled one: the trigger is AtLogOn, so its repetition
  # only starts at the next logon. Registered mid-session (or started by hand) it sits
  # there with no next run and the node stays silent — which looks identical to healthy.
  if ($task.State -ne "Disabled" -and -not $info.NextRunTime) {
    Add-Check "task-schedule" "WARN" ("no next run scheduled -- the AtLogOn trigger arms at the next logon. " +
                                      "Sign out and back in (or reboot) to start the ${intervalMin}-min repetition; " +
                                      "Start-ScheduledTask only fires a one-off sweep.")
  }

  if ($user -and $env:USERNAME -and ($user -notlike "*$env:USERNAME")) {
    Add-Check "task-owner" "WARN" "task runs as '$user', you are '$env:USERNAME' -- the hub mount and HF_TOKEN are per-user (see docs/deployment.md section 6)"
  }
}

# --- log freshness ----------------------------------------------------------
# A live node appends to watch.log every sweep -- including ticks it skips
# (outside process_window_local it still logs "skipping tick"). So a stale log
# means the timer is not firing, not merely that there was nothing to do.
$threshold = $MaxLogAgeMinutes
if ($threshold -le 0) {
  $threshold = if ($intervalMin -gt 0) { [Math]::Max(30, $intervalMin * 3) } else { 30 }
}
if (-not (Test-Path $logFile)) {
  Add-Check "log" "WARN" "no log yet: $logFile (task has not swept, or it logs elsewhere)"
} else {
  $ageMin = [int]((Get-Date) - (Get-Item $logFile).LastWriteTime).TotalMinutes
  $detail = "last write ${ageMin}m ago (threshold ${threshold}m) -- $logFile"
  if ($ageMin -gt $threshold) { Add-Check "log" "WARN" "stale -- $detail" }
  else                        { Add-Check "log" "OK" $detail }

  # Errors in the recent tail: surface them, they do not by themselves mean the node is down.
  # Covers both sides of the run: the python worker (Traceback / ok=False / OOM) AND the
  # PowerShell wrapper, whose throws ("venv python not found: …") land in this same log and
  # are what a non-zero task result usually means.
  $tail = Get-Content $logFile -Tail 200 -ErrorAction SilentlyContinue
  $errs = $tail | Select-String -Pattern "Traceback|ok=False|ERROR|CUDA out of memory|not found:|FullyQualifiedErrorId|watcher exit code"
  if ($errs) {
    Add-Check "log-errors" "WARN" "$($errs.Count) error line(s) in the last 200 -- newest: $(($errs | Select-Object -Last 1).Line.Trim())"
  } else {
    Add-Check "log-errors" "OK" "no error lines in the last 200"
  }
}

# --- sweep running right now ------------------------------------------------
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'audio_inbox_watch|media_transcribe' }
if ($procs) {
  $oldest = ($procs | Sort-Object CreationDate | Select-Object -First 1).CreationDate
  Add-Check "sweep" "OK" "$($procs.Count) process(es) running, oldest started $($oldest.ToString('HH:mm'))"
} else {
  Add-Check "sweep" "OK" "idle (no sweep in flight)"
}

# --- hub + token + queue (config-dependent) ---------------------------------
if ($cfg) {
  $hub = $cfg.hub_root
  if (-not $hub) {
    Add-Check "hub" "FAIL" "hub_root not set in $Config"
  } elseif (Test-Path $hub) {
    Add-Check "hub" "OK" $hub
  } else {
    Add-Check "hub" "FAIL" "unreachable: $hub (cloud drive not mounted, or wrong drive letter)"
  }

  $needsToken = ($cfg.speaker_mode -eq "diarize") -or ($cfg.voiceprint_mode -and $cfg.voiceprint_mode -ne "off")
  if ($needsToken) {
    # The worker reads the token from the environment, trying these names in order
    # (media_transcribe.py: load_diarization_pipeline). Any of them works.
    $names = @("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN")
    $machineName = $names | Where-Object { [Environment]::GetEnvironmentVariable($_, "Machine") } | Select-Object -First 1
    $anyName     = $names | Where-Object { [Environment]::GetEnvironmentVariable($_, "Machine") -or
                                           [Environment]::GetEnvironmentVariable($_, "User") -or
                                           [Environment]::GetEnvironmentVariable($_, "Process") } | Select-Object -First 1
    if (-not $anyName) {
      Add-Check "hf-token" "FAIL" "none of $($names -join '/') is set but speaker_mode=$($cfg.speaker_mode) -- diarization will fail"
    } elseif (-not $machineName) {
      Add-Check "hf-token" "WARN" "$anyName set for this profile only -- set it at Machine scope so it survives a login as another user (docs/deployment.md section 6)"
    } else {
      Add-Check "hf-token" "OK" "$machineName set at Machine scope"
    }
  }

  # Queue: read the intake sidecars the watcher writes next to each source file.
  # Scanned shallowly (inbox locations only, never sessions/) to stay fast on a cloud drive.
  if ($hub -and (Test-Path $hub)) {
    $sidecars = @()
    foreach ($glob in @("_inbox\*.state.json", "*\_*_inbox\*.state.json", "*\*.state.json")) {
      $sidecars += Get-ChildItem -Path (Join-Path $hub $glob) -ErrorAction SilentlyContinue
    }
    $sidecars = $sidecars | Where-Object { $_.Directory.Name -ne "_meta" } | Sort-Object FullName -Unique

    if (-not $sidecars) {
      Add-Check "queue" "OK" "no pending sidecars found under $hub"
    } else {
      $byStatus = @{}
      $stale = New-Object System.Collections.Generic.List[string]
      $leaseMin = if ($cfg.claim_lease_minutes) { [int]$cfg.claim_lease_minutes } else { 30 }

      foreach ($sf in $sidecars) {
        try { $st = Get-Content $sf.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } catch { continue }
        $status = if ($st.status) { $st.status } else { "unknown" }
        $byStatus[$status] = 1 + [int]$byStatus[$status]

        # in-progress is only healthy while its owner keeps refreshing the claim lease.
        if ($status -eq "in-progress") {
          $claimFile = $sf.FullName -replace '\.state\.json$', '.claim.json'
          $expired = $true ; $owner = "no claim"
          if (Test-Path $claimFile) {
            try {
              $cl = (Get-Content $claimFile -Raw -Encoding UTF8 | ConvertFrom-Json).claim
              $owner = $cl.claimed_by
              $until = if ($cl.lease_until) { [datetime]$cl.lease_until } else { ([datetime]$cl.claimed_at).AddMinutes($leaseMin) }
              $expired = ((Get-Date).ToUniversalTime() -ge $until.ToUniversalTime())
            } catch { $expired = $true ; $owner = "garbled claim" }
          }
          if ($expired) { $stale.Add("$($sf.Name -replace '\.state\.json$','') [$owner]") }
        }
      }

      $summary = ($byStatus.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join " "
      if ($stale.Count -gt 0) {
        Add-Check "queue" "WARN" "$summary -- $($stale.Count) orphaned in-progress (expired lease, another node will re-queue): $($stale -join ', ')"
      } else {
        Add-Check "queue" "OK" $summary
      }
    }
  }
}

# --- report -----------------------------------------------------------------
$worst = 0
foreach ($c in $checks) {
  if ($c.Status -eq "FAIL") { $worst = 2 }
  elseif ($c.Status -eq "WARN" -and $worst -lt 1) { $worst = 1 }
}
$verdict = @("OK", "WARN", "FAIL")[$worst]

if ($Json) {
  [pscustomobject]@{
    node     = if ($cfg) { $cfg.node.host_label } else { $env:COMPUTERNAME }
    verdict  = $verdict
    checked  = (Get-Date).ToString("s")
    checks   = $checks
  } | ConvertTo-Json -Depth 5
} else {
  Write-Host ""
  Write-Host "Node   : $(if ($cfg) { $cfg.node.host_label } else { $env:COMPUTERNAME })"
  Write-Host "Verdict: $verdict" -ForegroundColor @("Green", "Yellow", "Red")[$worst]
  Write-Host ""
  $checks | Format-Table -AutoSize -Wrap
  if ($worst -gt 0) {
    Write-Host "Fixes: docs/deployment.md section 5 (run continuously), 6 (survive reboots), 8 (troubleshooting)" -ForegroundColor DarkGray
  }
}

exit $worst
