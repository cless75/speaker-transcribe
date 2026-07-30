# update-node.ps1 — привести ASR-узел в актуальное состояние.
#
# Безопасно по умолчанию: обновляет код (git pull), доустанавливает недостающие
# пакеты, прогоняет диагностику. Рискованное — за явными флагами:
#   -CudaTorch    переустановить torch+torchaudio со сборкой CUDA (для GPU-диаризации)
#   -SyncConfig   заменить config/node.local.json копией из Hub _meta (с бэкапом)
#   -PushConfig   наоборот: выложить рабочий локальный конфиг в Hub _meta
#                 (после локальных правок — иначе следующий -SyncConfig их затрёт)
#   -FixVenv      починить venv, чей базовый Python пропал ("No Python at '...'"):
#                 перенаправляет pyvenv.cfg на живой интерпретатор той же версии,
#                 пакеты остаются на месте (не надо качать torch заново)
# -DryRun печатает план и ничего не выполняет.
#
# Примеры (Windows PowerShell на узле):
#   .\scripts\update-node.ps1                       # код + зависимости + диагностика
#   .\scripts\update-node.ps1 -SyncConfig           # + подтянуть конфиг из Hub
#   .\scripts\update-node.ps1 -CudaTorch            # + torch/torchaudio с CUDA (cu128)
#   .\scripts\update-node.ps1 -DryRun -CudaTorch    # только показать, что будет сделано

[CmdletBinding()]
param(
    [string]$Repo,
    [switch]$CudaTorch,
    [string]$CudaVersion = "cu128",   # соответствует requirements.txt; драйвер 590+ тянет
    [switch]$SyncConfig,
    [switch]$PushConfig,  # обратное направление: выложить локальный конфиг узла в Hub
    [switch]$FixVenv,     # починить сломанный venv (перенаправить pyvenv.cfg на живой базовый Python)
    [switch]$NoDiag,
    [switch]$Auto,        # автоматический режим: не ждать клавишу в конце (scheduled/agent)
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

function Head($t) { Write-Host ""; Write-Host ("=" * 64); Write-Host " $t"; Write-Host ("=" * 64) }
function Step($t) { Write-Host ""; Write-Host "-- $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   $t" -ForegroundColor Yellow }
function Do-Run($label, [scriptblock]$block) {
    if ($DryRun) { Write-Host "   [dry-run] $label" -ForegroundColor DarkGray; return }
    & $block
}

function Repair-Venv([string]$venvExe) {
    <#
      Чинит venv, чей базовый интерпретатор пропал (создавался под другим юзером
      или Python переставили): в pyvenv.cfg лежат home/executable/command с этим
      путём. Пакеты в Lib\site-packages при этом целы — их и сохраняем, поэтому
      перенаправляем конфиг на живой Python ТОЙ ЖЕ минорной версии, а не сносим
      venv (torch с CUDA — это гигабайты загрузки заново).
      Возвращает $true, если после правки venv запускается.
    #>
    $cfgFile = Join-Path (Split-Path (Split-Path $venvExe -Parent) -Parent) "pyvenv.cfg"
    if (-not (Test-Path $cfgFile)) { Warn "pyvenv.cfg не найден: $cfgFile — чинить нечего"; return $false }

    $text = Get-Content $cfgFile -Raw
    $ver = if ($text -match '(?m)^\s*version\s*=\s*"?(\d+)\.(\d+)') { "$($Matches[1]).$($Matches[2])" } else { "" }
    if (-not $ver) { Warn "в pyvenv.cfg нет version — не могу подобрать интерпретатор"; return $false }
    Write-Host "   venv собран на Python $ver — ищу такой же живой интерпретатор"

    # Кандидаты: py -X.Y (лаунчер), затем python из PATH — годится только точное
    # совпадение минорной версии, иначе site-packages несовместимы по ABI.
    $found = ""
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $out = (& py "-$ver" -c "import sys; print(sys.executable)" 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out)) { $cands += $out }
    }
    foreach ($name in "python", "python3") {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { $cands += $c.Source }
    }
    foreach ($cand in $cands) {
        $v = (& $cand -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1 | Out-String).Trim()
        if ($v -eq $ver) { $found = $cand; break }
    }
    if (-not $found) {
        Warn "живого Python $ver не нашлось (проверял: $($cands -join ', '))"
        Warn "  поставь Python $ver и повтори, либо пересоздай venv:"
        Warn "    py -$ver -m venv <путь>   +   pip install -r requirements.txt"
        return $false
    }
    $newHome = Split-Path $found -Parent
    Write-Host "   базовый Python: $found"

    Do-Run "перенаправить pyvenv.cfg на $newHome" {
        Copy-Item $cfgFile "$cfgFile.bak" -Force
        # Кавычки в путях — вторая половина той же граблы: venv-лаунчер ищет
        # интерпретатор вместе с кавычкой и пишет No Python at '"C:\...'.
        $fixed = ($text -replace '"', '') -split "`r?`n" | ForEach-Object {
            if ($_ -match '^\s*home\s*=')            { "home = $newHome" }
            elseif ($_ -match '^\s*executable\s*=')  { "executable = $found" }
            elseif ($_ -match '^\s*command\s*=')     { $_ -replace '^\s*command\s*=\s*\S+', "command = $found" }
            else                                     { $_ }
        }
        Set-Content -Path $cfgFile -Value ($fixed -join "`r`n") -Encoding ascii
        Ok "pyvenv.cfg обновлён (бэкап: pyvenv.cfg.bak)"
    }
    if ($DryRun) { return $false }

    $probe = (& $venvExe -V 2>&1 | Out-String).Trim()
    if ($probe -match "Python\s+\d") { Ok "venv запускается: $probe"; return $true }
    Warn "venv всё ещё не запускается: $probe"
    Warn "  пакеты собраны под другой сборкой — тут уже нужно пересоздание venv"
    return $false
}

# --- определить репозиторий, конфиг, venv ---------------------------------
if (-not $Repo) { $Repo = Split-Path $PSScriptRoot -Parent }
$cfgPath = Join-Path $Repo "config\node.local.json"

Head "ОБНОВЛЕНИЕ ASR-УЗЛА"
Write-Host " репозиторий : $Repo"
Write-Host " режим       : $(if ($DryRun) {'DRY-RUN (ничего не меняется)'} else {'выполнение'})"

if (-not (Test-Path $cfgPath)) { Warn "config\node.local.json не найден — прерываю"; exit 2 }
try {
    $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    Warn "config\node.local.json не парсится: $($_.Exception.Message)"
    Warn "(почини JSON и запусти снова)"; exit 2
}
$venv       = $cfg.transcribe_python
$hostLabel  = $cfg.node.host_label
$hubRoot    = $cfg.hub_root
Write-Host " host_label  : $hostLabel"
Write-Host " venv        : $venv"
Write-Host " device      : $($cfg.runtime.device) / $($cfg.runtime.compute_type)"
# venv может СУЩЕСТВОВАТЬ и при этом не запускаться: pyvenv.cfg хранит путь к
# базовому интерпретатору, и если тот пропал (venv создавали под другим юзером,
# Python переставили), python.exe падает с "No Python at '...'". Test-Path такое
# не ловит, а прежний exit 2 убивал заодно и шаги, которым venv вообще не нужен
# (git pull, синхронизация конфига, линки). Теперь проверяем запуском и идём дальше.
$venvOk  = $false
$venvErr = ""
if (Test-Path $venv) {
    $probe = (& $venv -V 2>&1 | Out-String).Trim()
    if ($probe -match "Python\s+\d") { $venvOk = $true; Write-Host " python      : $probe" }
    else { $venvErr = $probe }
} else {
    $venvErr = "файл не найден: $venv"
}
if (-not $venvOk) {
    Warn "venv не запускается: $venvErr"
    Warn "  (шаги, которым нужен venv — доустановка пакетов, torch, проверки — будут пропущены)"
    $pyvenvCfg = Join-Path (Split-Path (Split-Path $venv -Parent) -Parent) "pyvenv.cfg"
    if (Test-Path $pyvenvCfg) {
        $homeLine = (Select-String -Path $pyvenvCfg -Pattern '^\s*home\s*=' -ErrorAction SilentlyContinue |
                     Select-Object -First 1).Line
        if ($homeLine) {
            $homeDir = ($homeLine -split "=", 2)[1].Trim().Trim('"')
            $state = if (Test-Path $homeDir) { "существует" } else { "НЕ СУЩЕСТВУЕТ — вот причина" }
            Warn "  pyvenv.cfg home = $homeDir  ->  $state"
        }
        if (-not $FixVenv) { Warn "  починить, не удаляя пакеты: .\scripts\update-node.ps1 -FixVenv" }
    }
    if ($FixVenv) {
        Step "0. Починка venv (-FixVenv)"
        $venvOk = Repair-Venv $venv
    }
} elseif ($FixVenv) {
    Write-Host " -FixVenv    : venv и так запускается — чинить нечего"
}

# --- 1) git pull ----------------------------------------------------------
Step "1. Обновление кода (git pull)"
if (Test-Path (Join-Path $Repo ".git")) {
    $before = (git -C $Repo rev-parse --short HEAD)
    Do-Run "git -C $Repo pull --ff-only" { git -C $Repo pull --ff-only 2>&1 | ForEach-Object { Write-Host "   $_" } }
    if (-not $DryRun) {
        $after = (git -C $Repo rev-parse --short HEAD)
        if ($before -eq $after) { Ok "уже на свежем: $after" } else { Ok "обновлено: $before -> $after" }
    }
} else { Warn "не git-репозиторий — пропуск" }

# --- 2) конфиг из Hub (опционально) --------------------------------------
Step "2. Синхронизация конфига из Hub"
if ($SyncConfig) {
    $hubCfg = Join-Path $hubRoot "_meta\801-node.local.$hostLabel.json"
    if (Test-Path $hubCfg) {
        Do-Run "backup + copy $hubCfg -> $cfgPath" {
            Copy-Item $cfgPath "$cfgPath.bak" -Force
            Copy-Item $hubCfg $cfgPath -Force
            # JSON проверяем средствами PowerShell: синхронизация конфига не должна
            # зависеть от venv (раньше валидация шла через него и падала вместе с ним).
            try {
                $reload = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
                Ok "новый конфиг: JSON валиден"
                # Копия в Hub может отстать от машины: буква облачного диска меняется
                # (D: занял локальный диск -> GDrive переехал на G:), и синхронизация
                # тогда ЛОМАЕТ рабочий узел, возвращая нерезолвимый hub_root. Проверяем
                # до того, как оставить новый конфиг на месте.
                if ($reload.hub_root -and -not (Test-Path $reload.hub_root)) {
                    Warn "hub_root из Hub-копии не резолвится здесь: $($reload.hub_root)"
                    Copy-Item "$cfgPath.bak" $cfgPath -Force
                    Warn "  откатил на прежний node.local.json (его hub_root: $hubRoot)"
                    Warn "  копия в Hub устарела — залей туда рабочий локальный конфиг: -PushConfig"
                } else {
                    Ok "device теперь: $($reload.runtime.device) / $($reload.runtime.compute_type) (бэкап: node.local.json.bak)"
                }
            } catch {
                Warn "конфиг из Hub не парсится: $($_.Exception.Message)"
                Copy-Item "$cfgPath.bak" $cfgPath -Force
                Warn "  откатил на прежний node.local.json — почини файл в Hub и повтори"
            }
        }
    } else { Warn "в Hub нет $hubCfg — пропуск" }
} elseif ($PushConfig) {
    # Обратное направление: машина — источник истины, Hub — копия. Нужно после
    # локальной правки (сменилась буква диска, путь к venv, device), чтобы
    # следующий -SyncConfig не вернул узел в сломанное состояние.
    $hubMeta = Join-Path $hubRoot "_meta"
    if (-not (Test-Path $hubMeta)) {
        Warn "в Hub нет $hubMeta (диск не смонтирован?) — пропуск"
    } else {
        $hubCfg = Join-Path $hubMeta "801-node.local.$hostLabel.json"
        Do-Run "copy $cfgPath -> $hubCfg (с бэкапом старой копии)" {
            if (Test-Path $hubCfg) { Copy-Item $hubCfg "$hubCfg.bak" -Force }
            Copy-Item $cfgPath $hubCfg -Force
            Ok "конфиг узла выложен в Hub: $hubCfg"
            Ok "  hub_root в нём: $($cfg.hub_root)"
        }
    }
} else {
    Write-Host "   пропуск (укажи -SyncConfig, чтобы заменить конфиг копией из Hub; -PushConfig — наоборот)"
    if ($cfg.runtime.device -eq "gpu") { Warn "ВНИМАНИЕ: device='gpu' невалидно, движок ждёт 'cuda' — запусти с -SyncConfig" }
}

# --- 2b) линки путей (стабильный путь к Hub, если GDrive монтируется иначе) ----
Step "2b. Линки путей (path_links)"
$cfgNow = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$links  = @($cfgNow.path_links)
if (-not $links -or $links.Count -eq 0) {
    Write-Host "   не заданы (config: path_links: [])"
} else {
    foreach ($l in $links) {
        $lp = $l.link; $tg = $l.target
        $kind = if ($l.kind) { $l.kind } else { "Junction" }
        if (-not $lp -or -not $tg) { Warn "   у записи нет link/target — пропуск"; continue }
        if (Test-Path $lp) {
            Ok "путь уже существует: $lp"
        } elseif (-not (Test-Path $tg)) {
            Warn "target не найден: $tg — Google Drive не смонтирован под этим юзером? линк НЕ создан"
            Warn "  (если диск виртуальный per-user — линк не поможет; см. §6: авто-логин юзера с примонтированным диском)"
        } else {
            Do-Run "New-Item -ItemType $kind '$lp' -> '$tg'" {
                $parent = Split-Path $lp -Parent
                if ($parent -and -not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
                try {
                    New-Item -ItemType $kind -Path $lp -Target $tg -Force -ErrorAction Stop | Out-Null
                    Ok "создан ${kind}: $lp -> $tg"
                } catch {
                    Warn "не удалось создать ${kind}: $($_.Exception.Message)"
                    Warn "  (Junction — только локальный NTFS-target; для виртуального/сетевого попробуй kind: SymbolicLink, нужен admin/dev-mode)"
                }
            }
        }
    }
}

# --- 3) недостающие пакеты ------------------------------------------------
Step "3. Доустановка недостающих пакетов (opt-стадии)"
$ensure = @("rapidocr_onnxruntime", "speechbrain")
if (-not $venvOk) { Warn "пропуск: venv не запускается (см. выше, -FixVenv)"; $ensure = @() }
foreach ($pkg in $ensure) {
    $have = (& $venv -c "import importlib.util as u; print(bool(u.find_spec('$pkg')))").Trim()
    if ($have -eq "True") { Ok "$pkg — есть" }
    else {
        Warn "$pkg — отсутствует, ставлю"
        Do-Run "$venv -m pip install --default-timeout=120 --retries 10 $pkg" {
            & $venv -m pip install --default-timeout=120 --retries 10 $pkg
        }
    }
}

# --- 4) torch+torchaudio с CUDA (опционально, рискованно) ----------------
Step "4. torch/torchaudio с CUDA (GPU-диаризация)"
if ($CudaTorch -and -not $venvOk) {
    Warn "пропуск: venv не запускается (см. выше, -FixVenv)"
} elseif ($CudaTorch) {
    Warn "переустановка torch+torchaudio ($CudaVersion). На torch завязаны pyannote и voiceprint —"
    Warn "после установки проверю их импорт; при поломке восстанови прежние версии."
    $url = "https://download.pytorch.org/whl/$CudaVersion"
    Do-Run "$venv -m pip install --force-reinstall torch torchaudio --index-url $url" {
        & $venv -m pip install --force-reinstall --default-timeout=180 --retries 10 torch torchaudio --index-url $url
    }
    Do-Run "проверка torch.cuda + pyannote + speechbrain" {
        & $venv -c "import torch; print('   torch', torch.__version__, '| cuda', torch.cuda.is_available()); import pyannote.audio, speechbrain; print('   pyannote + speechbrain импортируются')"
        if ($LASTEXITCODE -ne 0) { Warn "ПОСЛЕ УСТАНОВКИ ЧТО-ТО СЛОМАЛОСЬ — проверь вывод выше; возможно нужен откат версий" }
        else { Ok "torch с CUDA поставлен, зависимости целы. Не забудь diarization_device: cuda в конфиге." }
    }
} else {
    Write-Host "   пропуск (укажи -CudaTorch для установки GPU-сборки torch; нужен только для GPU-диаризации)"
}

# --- 5) диагностика -------------------------------------------------------
Step "5. Диагностика узла"
if ($NoDiag) { Write-Host "   пропуск (-NoDiag)" }
else {
    $diag = Join-Path $Repo "scripts\node_diagnostics.py"
    # Диагностику полезнее прогнать даже со сломанным venv — она сама покажет, что
    # именно сломано, и запишет отчёт в Hub. Сам скрипт запускается любым Python:
    # проверки внутри venv он делает отдельным вызовом и переживает их падение.
    $diagPy = if ($venvOk) { $venv } else {
        $alt = Get-Command python -ErrorAction SilentlyContinue
        if (-not $alt) { $alt = Get-Command py -ErrorAction SilentlyContinue }
        if ($alt) { Warn "venv не запускается — гоню диагностику системным $($alt.Source)"; $alt.Source } else { "" }
    }
    if (-not (Test-Path $diag)) { Warn "scripts\node_diagnostics.py не найден (сделай git pull)" }
    elseif (-not $diagPy) { Warn "пропуск: нет ни рабочего venv, ни системного python" }
    else {
        # диагностику зовём в авторежиме — паузу для чтения делает update-node в самом конце
        Do-Run "python $diag" {
            & $diagPy $diag --repo $Repo --auto
        }
    }
}

Head "ГОТОВО"
if ($DryRun) { Write-Host " Это был DRY-RUN. Убери -DryRun, чтобы применить." }
else { Write-Host " Отчёт диагностики — в Hub: _meta\801-diag-$hostLabel.txt" }

# Пауза, чтобы человек успел прочитать вывод, если окно закроется после запуска.
# Пропускается в -Auto и в неинтерактивных сессиях (scheduled task / agent), чтобы не подвиснуть.
if (-not $Auto -and [Environment]::UserInteractive) {
    Write-Host ""
    Write-Host "  [нажмите любую клавишу, чтобы закрыть окно] " -NoNewline
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { }
    Write-Host ""
}
