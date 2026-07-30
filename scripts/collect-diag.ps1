# collect-diag.ps1 -- collect everything needed to debug a silent node into ONE file.
#
# Why: the console window closes (or scrolls away) before a human can read the
# error, and there is no shared clipboard between machines. This script captures
# every stream -- including stderr, where "No Python at '...'" shows up -- and
# writes the result to the Hub, so it can be read from any other machine.
#
# Nothing is changed on the node: read-only checks only. Secrets are reported as
# set/not set, never printed.
#
# Usage (from the repo root, or just double-click scripts\collect-diag.cmd):
#   .\scripts\collect-diag.ps1
#
# ASCII-only on purpose: PS 5.1 mangles non-ASCII in .ps1 files without a BOM.

[CmdletBinding()]
param(
    [string]$Repo,
    [int]$Keep = 10,          # how many stamped copies to keep per host
    [int]$WatchLogTail = 40   # lines of watch.log to include
)

$ErrorActionPreference = "Continue"   # a failing probe must not abort the collection
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not $Repo) { $Repo = Split-Path $PSScriptRoot -Parent }

$lines = New-Object System.Collections.Generic.List[string]
function Say($text) { $lines.Add([string]$text) | Out-Null; Write-Host $text }
function Section($title) { Say ""; Say ("--- " + $title + " ---") }
function Probe($title, [scriptblock]$block) {
    Section $title
    try {
        $out = & $block 2>&1 | Out-String
    } catch {
        $out = "EXCEPTION: " + $_.Exception.Message
    }
    foreach ($l in ($out -split "`r?`n")) { if ($l.Trim() -ne "") { Say ("  " + $l.TrimEnd()) } }
}

$stamp = Get-Date -Format "yyyy-MM-dd-HHmm"
Say ("=" * 62)
Say " 801 NODE DIAG COLLECT"
Say ("=" * 62)
Say ("  time     : " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Say ("  host     : " + $env:COMPUTERNAME)
Say ("  user     : " + $env:USERNAME + "   (the account running THIS collection)")
Say ("  repo     : " + $Repo)

# --- config: the node config drives every path below ----------------------
$cfgPath = Join-Path $Repo "config\node.local.json"
$cfg = $null
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Say ("  config   : INVALID JSON -- " + $_.Exception.Message) }
} else {
    Say ("  config   : NOT FOUND -- " + $cfgPath)
}
$venv      = if ($cfg) { $cfg.transcribe_python } else { "" }
$hubRoot   = if ($cfg) { $cfg.hub_root } else { "" }
$hostLabel = if ($cfg -and $cfg.node.host_label) { $cfg.node.host_label } else { $env:COMPUTERNAME }
Say ("  venv     : " + $venv)
Say ("  hub_root : " + $hubRoot)

Probe "git" { git -C $Repo log --oneline -1; git -C $Repo status -sb }

# The classic failure: a venv whose base interpreter path points at another
# user's profile (or carries a stray quote) -- python.exe exists but cannot run.
if ($venv) {
    $pyvenv = Join-Path (Split-Path (Split-Path $venv -Parent) -Parent) "pyvenv.cfg"
    Probe ("pyvenv.cfg (" + $pyvenv + ")") {
        if (Test-Path $pyvenv) { Get-Content $pyvenv } else { "NOT FOUND" }
    }
    Probe "venv python -V" {
        if (Test-Path $venv) { & $venv -V } else { "NOT FOUND: " + $venv }
    }
}

Probe "ffmpeg" { $f = (Get-Command ffmpeg -ErrorAction SilentlyContinue); if ($f) { $f.Source } else { "NOT on PATH" } }

# Which env vars matter is a property of the config ("env:HF_TOKEN" on one node,
# "env:HUGGINGFACE_TOKEN" on another) -- collect the names instead of guessing.
function Get-EnvRefs($node, $acc) {
    if ($null -eq $node) { return }
    if ($node -is [string]) {
        if ($node -like "env:*") { $acc.Add($node.Substring(4)) | Out-Null }
    } elseif ($node -is [System.Collections.IEnumerable] -and -not ($node -is [string])) {
        foreach ($item in $node) { Get-EnvRefs $item $acc }
    } elseif ($node -is [psobject]) {
        foreach ($p in $node.PSObject.Properties) { Get-EnvRefs $p.Value $acc }
    }
}
$envNames = New-Object System.Collections.Generic.List[string]
if ($cfg) { Get-EnvRefs $cfg $envNames }

# Секрет может лежать файлом (обычно в Hub) — тогда переменной окружения на узле нет
# и быть не должно. Проверяем читаемость источника, не показывая содержимое.
function Get-FileSecrets($node, $acc) {
    if ($null -eq $node) { return }
    if ($node -is [string]) {
        if ($node -like "file:*") { $acc.Add($node.Substring(5)) | Out-Null }
    } elseif ($node -is [System.Collections.IEnumerable] -and -not ($node -is [string])) {
        foreach ($item in $node) { Get-FileSecrets $item $acc }
    } elseif ($node -is [psobject]) {
        foreach ($p in $node.PSObject.Properties) { Get-FileSecrets $p.Value $acc }
    }
}
$fileSecrets = New-Object System.Collections.Generic.List[string]
if ($cfg) { Get-FileSecrets $cfg $fileSecrets }
if ($fileSecrets.Count -gt 0) {
    Probe "secrets from files (content never printed)" {
        foreach ($spec in ($fileSecrets | Select-Object -Unique)) {
            $p = $spec.Replace("{hub_root}", [string]$hubRoot)
            if (-not (Test-Path $p)) { "{0} : NOT FOUND" -f $p; continue }
            try {
                $len = (Get-Content $p -Raw -ErrorAction Stop).Trim().Length
                "{0} : {1}" -f $p, $(if ($len -gt 0) { "ok ($len chars)" } else { "EMPTY" })
            } catch {
                "{0} : UNREADABLE ({1})" -f $p, $_.Exception.Message
            }
        }
    }
}
Probe "env vars referenced by the config (values never printed)" {
    if ($envNames.Count -eq 0) { "config references no env:VAR" }
    foreach ($name in ($envNames | Select-Object -Unique)) {
        foreach ($scope in "Process", "User", "Machine") {
            $v = [Environment]::GetEnvironmentVariable($name, $scope)
            "{0,-22} {1,-8}: {2}" -f $name, $scope, $(if ($v) { "set" } else { "not set" })
        }
    }
}

Probe "scheduled task" {
    $found = $false
    foreach ($n in "speaker-transcribe-watch", "MS-Audio-Inbox-Watch") {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($t) {
            $found = $true
            $i = Get-ScheduledTaskInfo -TaskName $n -ErrorAction SilentlyContinue
            "name      : " + $n
            "state     : " + $t.State
            "owner     : " + $t.Principal.UserId + "  (logon: " + $t.Principal.LogonType + ")"
            "last run  : " + $i.LastRunTime + "  result: " + $i.LastTaskResult
            "next run  : " + $i.NextRunTime
        }
    }
    if (-not $found) { "NOT REGISTERED (neither speaker-transcribe-watch nor MS-Audio-Inbox-Watch)" }
}

if ($hubRoot) {
    Probe "hub mount" {
        if (Test-Path $hubRoot) { "mounted: " + $hubRoot } else { "NOT MOUNTED under this user: " + $hubRoot }
    }
}

# The diagnostics script writes its own report; here we only need its output
# captured, so a crash inside it lands in this file too.
$diag = Join-Path $Repo "scripts\node_diagnostics.py"
Probe "node_diagnostics.py --auto" {
    if (-not (Test-Path $diag)) { "NOT FOUND: " + $diag + " (git pull needed)" }
    elseif (-not ($venv -and (Test-Path $venv))) { "skipped: venv python unavailable" }
    else { & $venv $diag --repo $Repo --auto }
}

$watchLog = Join-Path $Repo "logs\watch.log"
Probe ("watch.log tail " + $WatchLogTail) {
    if (Test-Path $watchLog) {
        $st = Get-Item $watchLog
        ("size: {0} bytes, modified {1} ({2:N0} min ago)" -f $st.Length, $st.LastWriteTime,
            ((Get-Date) - $st.LastWriteTime).TotalMinutes)
        Get-Content $watchLog -Tail $WatchLogTail
    } else { "NOT FOUND: " + $watchLog + " (the task never wrote a log)" }
}

Say ""
Say ("=" * 62)

# --- write out: repo\logs always, Hub when it is reachable ----------------
$safe = ($hostLabel -replace '[^\w\.-]', '_')
$body = ($lines -join "`r`n") + "`r`n"

$logDir = Join-Path $Repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$localPath = Join-Path $logDir ("console-" + $safe + "-" + $stamp + ".txt")
Set-Content -Path $localPath -Value $body -Encoding UTF8
Get-ChildItem $logDir -Filter ("console-" + $safe + "-*.txt") | Sort-Object Name |
    Select-Object -SkipLast $Keep | Remove-Item -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host ("local copy : " + $localPath) -ForegroundColor Green

if ($hubRoot -and (Test-Path $hubRoot)) {
    $meta = Join-Path $hubRoot "_meta"
    try {
        New-Item -ItemType Directory -Force -Path $meta | Out-Null
        $hubLatest = Join-Path $meta ("801-console-" + $safe + ".txt")
        Set-Content -Path $hubLatest -Value $body -Encoding UTF8
        Set-Content -Path (Join-Path $meta ("801-console-" + $safe + "-" + $stamp + ".txt")) -Value $body -Encoding UTF8
        Get-ChildItem $meta -Filter ("801-console-" + $safe + "-*.txt") | Sort-Object Name |
            Select-Object -SkipLast $Keep | Remove-Item -Force -ErrorAction SilentlyContinue
        Write-Host ("hub copy   : " + $hubLatest) -ForegroundColor Green
        Write-Host "             (readable from any other machine -- no clipboard needed)"
    } catch {
        Write-Host ("hub copy FAILED: " + $_.Exception.Message) -ForegroundColor Yellow
    }
} else {
    Write-Host "hub copy   : skipped (hub not mounted under this user)" -ForegroundColor Yellow
}
