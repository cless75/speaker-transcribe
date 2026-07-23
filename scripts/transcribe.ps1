<#
.SYNOPSIS
  Quick transcription (ASR + diarization) of a single file or a whole folder.

.DESCRIPTION
  Thin wrapper over src/media_transcribe_cli.py with sensible defaults. Accepts a
  file or a folder (then processes all media in batch). Uses the venv python by
  default; everything is configurable via parameters. ASCII-only output so the
  script parses under any console code page.

.EXAMPLE
  .\scripts\transcribe.ps1 -InputPath C:\work\inbox\meeting.m4a

.EXAMPLE
  .\scripts\transcribe.ps1 -InputPath C:\work\inbox -OutputDir C:\work\output -Model medium

.NOTE
  If "running scripts is disabled" -> run once:
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$InputPath,                                   # file or folder with media
  [string]$OutputDir   = "C:\work\output",             # where transcripts go
  [string]$Model       = "medium",                      # faster-whisper model
  [ValidateSet("on","off")]
  [string]$SpeakerMode = "on",                          # speaker diarization
  [ValidateSet("hms","vtt","both","none")]
  [string]$Timestamps  = "both",
  [string]$PythonBin   = "C:\work\venvs\asr\Scripts\python.exe",  # venv python (no activation)
  [switch]$ListOnly                                     # only show Inbox contents, do not run ASR
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"                                   # correct UTF-8 output (Cyrillic transcripts)

$repo = Split-Path $PSScriptRoot -Parent
$cli  = Join-Path $repo "src\media_transcribe_cli.py"

if (-not (Test-Path $PythonBin)) { throw "venv python not found: $PythonBin (create venv per docs/node-setup.html)" }
if (-not (Test-Path $cli))       { throw "CLI not found: $cli" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

# Keep in sync with audio_inbox_watch.DEFAULT_SCAN_EXTENSIONS: this manual runner was
# missing .webm (Telemost/Meet exports) and .m4v/.oga, so those files were silently
# skipped in batch mode while the watcher picked them up fine.
$exts = @("*.m4a","*.mp4","*.wav","*.mp3","*.oga","*.mov","*.mkv","*.webm","*.m4v","*.avi")

function Invoke-One([string]$file) {
  Write-Host ">> $file" -ForegroundColor Cyan
  & $PythonBin $cli --input $file --output-dir $OutputDir --model $Model `
      --speaker-mode $SpeakerMode --timestamps $Timestamps
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 9) {   # exit 9 = false CUDA-teardown
    Write-Warning "Exit code $LASTEXITCODE for $file (check for *-transcript.md)"
  }
}

if (Test-Path $InputPath -PathType Container) {
  $files = Get-ChildItem $InputPath -Recurse -File -Include $exts | Sort-Object Length
  Write-Host "Inbox: $InputPath - found $($files.Count) media files" -ForegroundColor Yellow
  $files | ForEach-Object { "{0,7:N0} MB  {1}" -f ($_.Length/1MB), $_.FullName } | Write-Host
  if ($ListOnly) { Write-Host "(-ListOnly: ASR not started)" -ForegroundColor DarkGray; return }
  foreach ($f in $files) { Invoke-One $f.FullName }
}
else {
  Invoke-One (Resolve-Path $InputPath).Path
}

Write-Host "Done. Results in: $OutputDir" -ForegroundColor Green
