<#
.SYNOPSIS
  Быстрый запуск транскрибации (ASR + диаризация) одного файла или целой папки.

.DESCRIPTION
  Тонкая обёртка над src/media_transcribe_cli.py с разумными настройками по умолчанию.
  Принимает файл или папку (тогда обрабатывает все медиа пакетно). Интерпретатор
  берётся из venv по умолчанию; всё настраивается параметрами.

.EXAMPLE
  .\scripts\transcribe.ps1 -InputPath C:\work\inbox\meeting.m4a

.EXAMPLE
  .\scripts\transcribe.ps1 -InputPath C:\work\inbox -OutputDir C:\work\output -Model medium
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$InputPath,                                   # файл или папка с медиа
  [string]$OutputDir   = "C:\work\output",              # куда складывать транскрипты
  [string]$Model       = "medium",                      # модель faster-whisper
  [ValidateSet("on","off")]
  [string]$SpeakerMode = "on",                          # диаризация спикеров
  [ValidateSet("hms","vtt","both","none")]
  [string]$Timestamps  = "both",
  [string]$PythonBin   = "C:\work\venvs\asr\Scripts\python.exe",  # venv python (не активируем)
  [switch]$ListOnly                                              # только показать, что в Inbox, без запуска ASR
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"                                   # корректный UTF-8 вывод (кириллица)

$repo = Split-Path $PSScriptRoot -Parent
$cli  = Join-Path $repo "src\media_transcribe_cli.py"

if (-not (Test-Path $PythonBin)) { throw "venv python не найден: $PythonBin  (создайте venv по docs/node-setup.html)" }
if (-not (Test-Path $cli))       { throw "CLI не найден: $cli" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$exts = @("*.m4a","*.mp4","*.wav","*.mp3","*.mov","*.mkv")

function Invoke-One([string]$file) {
  Write-Host ">> $file" -ForegroundColor Cyan
  & $PythonBin $cli --input $file --output-dir $OutputDir --model $Model `
      --speaker-mode $SpeakerMode --timestamps $Timestamps
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 9) {   # exit 9 = ложный CUDA-teardown
    Write-Warning "Код возврата $LASTEXITCODE для $file (проверьте наличие *-transcript.md)"
  }
}

if (Test-Path $InputPath -PathType Container) {
  $files = Get-ChildItem $InputPath -Recurse -File -Include $exts | Sort-Object Length
  Write-Host "Inbox: $InputPath — найдено $($files.Count) медиафайлов" -ForegroundColor Yellow
  $files | ForEach-Object { "{0,7:N0} MB  {1}" -f ($_.Length/1MB), $_.FullName } | Write-Host
  if ($ListOnly) { Write-Host "(-ListOnly: ASR не запускался)" -ForegroundColor DarkGray; return }
  foreach ($f in $files) { Invoke-One $f.FullName }
}
else {
  Invoke-One (Resolve-Path $InputPath).Path
}

Write-Host "Готово. Результаты в: $OutputDir" -ForegroundColor Green
