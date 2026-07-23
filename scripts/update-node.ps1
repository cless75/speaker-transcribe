# update-node.ps1 — привести ASR-узел в актуальное состояние.
#
# Безопасно по умолчанию: обновляет код (git pull), доустанавливает недостающие
# пакеты, прогоняет диагностику. Рискованное — за явными флагами:
#   -CudaTorch    переустановить torch+torchaudio со сборкой CUDA (для GPU-диаризации)
#   -SyncConfig   заменить config/node.local.json копией из Hub _meta (с бэкапом)
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
    [switch]$NoDiag,
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
if (-not (Test-Path $venv)) { Warn "интерпретатор venv не найден: $venv — прерываю"; exit 2 }

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
            & $venv -c "import json; json.load(open(r'$cfgPath',encoding='utf-8')); print('   новый конфиг: JSON валиден')"
            $reload = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
            Ok "device теперь: $($reload.runtime.device) / $($reload.runtime.compute_type) (бэкап: node.local.json.bak)"
        }
    } else { Warn "в Hub нет $hubCfg — пропуск" }
} else {
    Write-Host "   пропуск (укажи -SyncConfig, чтобы заменить конфиг копией из Hub)"
    if ($cfg.runtime.device -eq "gpu") { Warn "ВНИМАНИЕ: device='gpu' невалидно, движок ждёт 'cuda' — запусти с -SyncConfig" }
}

# --- 3) недостающие пакеты ------------------------------------------------
Step "3. Доустановка недостающих пакетов (opt-стадии)"
$ensure = @("rapidocr_onnxruntime", "speechbrain")
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
if ($CudaTorch) {
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
    if (Test-Path $diag) {
        Do-Run "python $diag" {
            & $venv $diag --repo $Repo
        }
    } else { Warn "scripts\node_diagnostics.py не найден (сделай git pull)" }
}

Head "ГОТОВО"
if ($DryRun) { Write-Host " Это был DRY-RUN. Убери -DryRun, чтобы применить." }
else { Write-Host " Отчёт диагностики — в Hub: _meta\801-diag-$hostLabel.txt" }
