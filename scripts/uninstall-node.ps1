# uninstall-node.ps1 -- remove this node's footprint from the machine.
#
# What it can clean (each behind its own switch, nothing implicit):
#   scheduled tasks   every task whose NAME or ACTION points at this watcher
#   running processes a sweep in flight
#   autostart entries Startup-folder shortcuts / Run keys pointing at the watcher
#   venv              the interpreter tree from transcribe_python
#   cache             cache_root (voiceprint store, intermediates)
#   logs              <repo>\logs
#   config            config\node.local.json (+ .bak)
#   env vars          HF_TOKEN & co at User/Machine scope
#   path links        junctions/symlinks created from path_links
#
# NEVER touched: the Hub. Transcripts, registries and per-project data live there
# and are shared with other nodes -- deleting a node must not delete the work.
# The repository itself is not removed either (this script lives in it): delete
# the folder by hand once you are done.
#
# DRY-RUN BY DEFAULT: without -Force nothing is deleted, you only see the plan.
#
#   .\scripts\uninstall-node.ps1                    # plan for the default set (tasks + autostart + processes)
#   .\scripts\uninstall-node.ps1 -All               # plan for everything
#   .\scripts\uninstall-node.ps1 -All -Force        # actually remove everything
#   .\scripts\uninstall-node.ps1 -Tasks -Force      # only unregister the scheduled tasks
#
# Removing a task owned by ANOTHER user requires an elevated shell; the script
# says so instead of failing silently.
#
# ASCII-only on purpose: PS 5.1 mangles non-ASCII in .ps1 files without a BOM.

[CmdletBinding()]
param(
    [string]$Repo,
    [switch]$Tasks,        # scheduled tasks (default set)
    [switch]$Autostart,    # Startup folder + Run keys (default set)
    [switch]$Processes,    # kill a sweep in flight (default set)
    [switch]$Venv,         # delete the venv tree
    [switch]$Cache,        # delete cache_root
    [switch]$Logs,         # delete <repo>\logs
    [switch]$Config,       # delete config\node.local.json (+ .bak)
    [switch]$EnvVars,      # clear HF_TOKEN & co at User/Machine scope
    [switch]$Links,        # remove junctions/symlinks from path_links
    [switch]$All,          # everything above
    [switch]$Force         # actually do it (without this: dry-run)
)

$ErrorActionPreference = "Continue"
if (-not $Repo) { $Repo = Split-Path $PSScriptRoot -Parent }

if ($All) { $Tasks = $Autostart = $Processes = $Venv = $Cache = $Logs = $Config = $EnvVars = $Links = $true }
# Default set: the parts that keep the node ALIVE. Data-bearing paths (venv,
# cache, config) stay unless asked for explicitly.
if (-not ($Tasks -or $Autostart -or $Processes -or $Venv -or $Cache -or $Logs -or $Config -or $EnvVars -or $Links)) {
    $Tasks = $Autostart = $Processes = $true
}

function Head($t) { Write-Host ""; Write-Host ("=" * 64); Write-Host " $t"; Write-Host ("=" * 64) }
function Step($t) { Write-Host ""; Write-Host "-- $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   $t" -ForegroundColor Yellow }
function Plan($t) { Write-Host "   [dry-run] $t" -ForegroundColor DarkGray }

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Head "УДАЛЕНИЕ УЗЛА С МАШИНЫ"
Write-Host " репозиторий : $Repo"
Write-Host " режим       : $(if ($Force) { 'ВЫПОЛНЕНИЕ (удаляю)' } else { 'DRY-RUN (только план, ничего не трогаю)' })"
Write-Host " admin       : $(if ($isAdmin) { 'да' } else { 'нет — чужие задачи не удалить' })"

# --- конфиг: из него берём пути venv/cache/links --------------------------
$cfgPath = Join-Path $Repo "config\node.local.json"
$cfg = $null
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Warn "config\node.local.json не парсится — пути venv/cache возьму только из явных ключей" }
} else {
    Warn "config\node.local.json не найден — venv/cache/links чистить не по чему"
}

function Remove-PathSafely($path, $label) {
    if (-not $path) { return }
    if (-not (Test-Path $path)) { Write-Host "   $label : нет ($path)"; return }
    if (-not $Force) { Plan "удалить $label : $path"; return }
    try {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
        Ok "удалено $label : $path"
    } catch {
        Warn "не удалось удалить $label ($path): $($_.Exception.Message)"
    }
}

# --- 1) процессы ----------------------------------------------------------
if ($Processes) {
    Step "Живые прогоны вотчера"
    $pat = 'watch\.ps1|audio_inbox_watch|media_transcribe'
    $selfPat = 'uninstall-node|node_diagnostics|collect-diag'
    $found = $false
    foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe' OR Name='python.exe'")) {
        $cmd = "$($p.CommandLine)"
        if ($p.ProcessId -eq $PID -or $cmd -match $selfPat -or $cmd -notmatch $pat) { continue }
        $found = $true
        if (-not $Force) { Plan "остановить pid $($p.ProcessId) ($($p.Name))"; continue }
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Ok "остановлен pid $($p.ProcessId)" }
        catch { Warn "не удалось остановить pid $($p.ProcessId): $($_.Exception.Message)" }
    }
    if (-not $found) { Write-Host "   не запущено" }
}

# --- 2) задачи планировщика ----------------------------------------------
if ($Tasks) {
    Step "Задачи планировщика"
    $pat = 'watch\.ps1|audio_inbox_watch|speaker-transcribe|MS-Audio-Inbox'
    $hits = @()
    foreach ($t in (Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        $act = ($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' '
        if (($t.TaskName -match $pat) -or ($act -match $pat)) { $hits += $t }
    }
    if (-not $hits) {
        Write-Host "   не найдено"
        if (-not $isAdmin) { Warn "список задач урезан правами — перепроверь из admin-шелла" }
    }
    foreach ($t in $hits) {
        $owner = $t.Principal.UserId
        $act = ($t.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' '
        Write-Host "   найдена: $($t.TaskPath)$($t.TaskName)  [$($t.State)]  владелец: $owner"
        Write-Host "     запускает: $act"
        $ownerShort = "$owner".Split("\")[-1]
        if (-not $isAdmin -and $ownerShort -and $ownerShort -ne $env:USERNAME) {
            Warn "чужая задача — нужен admin-шелл, пропуск"
            continue
        }
        if (-not $Force) { Plan "снять задачу $($t.TaskPath)$($t.TaskName)"; continue }
        try {
            Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false -ErrorAction Stop
            Ok "снята: $($t.TaskPath)$($t.TaskName)"
        } catch {
            Warn "не удалось снять $($t.TaskName): $($_.Exception.Message)"
        }
    }
}

# --- 3) автозапуск --------------------------------------------------------
if ($Autostart) {
    Step "Автозапуск (папка Startup и ключи Run)"
    $pat = 'watch\.ps1|audio_inbox_watch|speaker-transcribe'
    $any = $false
    $sh = New-Object -ComObject WScript.Shell
    foreach ($d in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('CommonStartup'))) {
        if (-not $d -or -not (Test-Path $d)) { continue }
        foreach ($f in (Get-ChildItem $d -File)) {
            $target = ''
            if ($f.Extension -eq '.lnk') {
                $lnk = $sh.CreateShortcut($f.FullName)
                $target = "$($lnk.TargetPath) $($lnk.Arguments)"
            }
            if (($f.Name -match $pat) -or ($target -match $pat)) {
                $any = $true
                Remove-PathSafely $f.FullName "ярлык автозапуска"
            }
        }
    }
    foreach ($hive in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
                        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run')) {
        $k = Get-ItemProperty -Path $hive -ErrorAction SilentlyContinue
        if (-not $k) { continue }
        foreach ($p in $k.PSObject.Properties) {
            if ($p.Name -like 'PS*' -or "$($p.Value)" -notmatch $pat) { continue }
            $any = $true
            if (-not $Force) { Plan "удалить ключ $hive\$($p.Name)"; continue }
            try { Remove-ItemProperty -Path $hive -Name $p.Name -ErrorAction Stop; Ok "удалён ключ $($p.Name)" }
            catch { Warn "не удалось удалить ключ $($p.Name): $($_.Exception.Message)" }
        }
    }
    if (-not $any) { Write-Host "   не найдено" }
}

# --- 4) линки путей -------------------------------------------------------
if ($Links -and $cfg) {
    Step "Линки путей (path_links)"
    # Имя переменной НЕ $links: PowerShell не различает регистр, и она затирала бы
    # параметр -Links (switch) значением массива.
    $pathLinks = @($cfg.path_links)
    if (-not $pathLinks -or $pathLinks.Count -eq 0) { Write-Host "   не заданы" }
    foreach ($l in $pathLinks) {
        if (-not $l.link) { continue }
        $item = Get-Item -LiteralPath $l.link -ErrorAction SilentlyContinue
        if (-not $item) { Write-Host "   нет: $($l.link)"; continue }
        # Удаляем ТОЛЬКО линк: если по пути лежит настоящий каталог с данными,
        # снос был бы удалением содержимого, а не отвязкой.
        if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Warn "$($l.link) — не линк, а реальный каталог: пропуск"
            continue
        }
        Remove-PathSafely $l.link "линк"
    }
}

# --- 5) venv / cache / logs / config --------------------------------------
if ($Venv) {
    Step "venv"
    $venvExe = if ($cfg) { $cfg.transcribe_python } else { $null }
    if (-not $venvExe) { Write-Host "   transcribe_python не задан — пропуск" }
    else {
        # <venv>\Scripts\python.exe -> <venv>
        $venvRoot = Split-Path (Split-Path $venvExe -Parent) -Parent
        Remove-PathSafely $venvRoot "venv"
    }
}
if ($Cache) {
    Step "Кэш узла (cache_root)"
    $cacheRoot = if ($cfg -and $cfg.node) { $cfg.node.cache_root } else { $null }
    if (-not $cacheRoot) { Write-Host "   cache_root не задан — пропуск" }
    else {
        Warn "в кэше лежит стор голосовых отпечатков (voiceprints.json) — восстановлению не подлежит"
        Remove-PathSafely $cacheRoot "cache_root"
    }
}
if ($Logs) {
    Step "Логи"
    Remove-PathSafely (Join-Path $Repo "logs") "logs"
}
if ($Config) {
    Step "Конфиг узла"
    Remove-PathSafely $cfgPath "node.local.json"
    Remove-PathSafely "$cfgPath.bak" "node.local.json.bak"
    Remove-PathSafely (Join-Path $Repo "config\.last-known-hub") ".last-known-hub"
}

# --- 6) переменные окружения ---------------------------------------------
if ($EnvVars) {
    Step "Переменные окружения"
    foreach ($name in @("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN")) {
        foreach ($scope in @("User", "Machine")) {
            $v = [Environment]::GetEnvironmentVariable($name, $scope)
            if (-not $v) { continue }
            if ($scope -eq "Machine" -and -not $isAdmin) {
                Warn "$name ($scope) — нужен admin-шелл, пропуск"
                continue
            }
            if (-not $Force) { Plan "очистить $name ($scope)"; continue }
            try { [Environment]::SetEnvironmentVariable($name, $null, $scope); Ok "очищено: $name ($scope)" }
            catch { Warn "не удалось очистить $name ($scope): $($_.Exception.Message)" }
        }
    }
}

Head "ИТОГ"
if (-not $Force) {
    Write-Host " Это был DRY-RUN. Ничего не удалено. Повтори с -Force, чтобы применить."
} else {
    Write-Host " Готово."
}
Write-Host ""
Write-Host " НЕ ТРОНУТО (и не будет):"
Write-Host "   * Hub — транскрипты, реестры голосов, конфиги узлов в _meta"
if ($cfg -and $cfg.hub_root) { Write-Host "     ($($cfg.hub_root))" }
Write-Host "   * сам репозиторий $Repo — удали папку вручную, когда закончишь"
Write-Host "   * кэш моделей HuggingFace / faster-whisper в профиле пользователя"
Write-Host "     (~\.cache\huggingface — общий для других проектов, чистить осознанно)"
