# Транскрибация через media_transcribe.py (stdin JSON).
# Двухшаговый speaker_pass: сначала asr_only (или full) с тем же -OutputDir (по умолчанию …\Transcripts рядом с медиа), затем speaker_pass с тем же -InputPath.
# Явный путь к ASR-сырью: -AsrRawJsonPath (только при ExecutionMode speaker_pass).
# Карта имён для сегментов (ключи как «Speaker 1» … после диаризации): JSON-объект в файле, путь через -SpeakerMapPath.
# Если передан -ZoomVttPath, speaker turns берутся из Zoom .vtt; diarization остаётся fallback.
# По умолчанию без *-transcript.txt (только md/json/vtt); plain text: -IncludePlainTxt.
# ASR recovery: см. RUNBOOK; отключить подхват: -ForceFreshAsr; отключить авто-recover: -NoAsrAutoRecover. Режим только слияния чанков: -ExecutionMode merge_asr_chunks.
# Не запускайте параллельно несколько прогонов на один и тот же InputPath; -Background повышает риск гонки за *-raw.json и обрезанных файлов.
# Диагностика: -StderrLog <path> (только foreground) пишет stderr worker в файл; см. media-transcribe-direct-RUNBOOK.md

param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [string]$OutputDir,
    [string]$SpeakerClipOutputDir,
    [ValidateSet('asr_only', 'speaker_pass', 'full', 'merge_asr_chunks')]
    [string]$ExecutionMode = 'full',
    [ValidateSet('off', 'diarize')]
    [string]$SpeakerMode = 'diarize',
    [ValidateSet('off', 'match', 'enroll')]
    [string]$VoiceprintMode = 'match',
    [ValidateSet('small', 'medium', 'large-v3')]
    [string]$Model = 'medium',
    [ValidateSet('cpu', 'cuda')]
    [string]$RuntimeDevice = 'cuda',
    [string]$LanguageHint = 'ru',
    [int]$ChunkMinutes = 20,
    [int]$ChunkOverlapSec = 30,
    [int]$MaxParallelChunks = 2,
    [string]$PythonBin = 'python',
    [string]$WorkerPath = "$PSScriptRoot\..\src\media_transcribe.py",
    [string]$ModelRoot = '',
    [string]$WorkRoot = "$env:TEMP\asr-work",
    [string]$FfmpegBin = 'ffmpeg',
    [string]$FfprobeBin = 'ffprobe',
    [string]$DiarizationModel = 'pyannote/speaker-diarization-3.1',
    [string]$ProfileStorePath = "$env:USERPROFILE\.cache\speaker-transcribe\voiceprint-profiles.json",
    [double]$VoiceprintThreshold = 0.84,
    [switch]$Background,
    [string]$StdoutPath,
    [string]$StderrPath,
    [string]$AsrRawJsonPath,
    [string]$SpeakerMapPath,
    [string]$ZoomVttPath,
    [string]$ProjectSpeakerRegistryPath,
    [string]$SessionArtifactDir,
    [string]$AsrVariantId,
    [string]$StderrLog,
    [string]$VoiceprintEnrollName,
    [string]$VoiceprintContactRef,
    [string]$VoiceprintContactName,
    [switch]$IncludePlainTxt,
    [switch]$ForceFreshAsr,
    [switch]$NoAsrAutoRecover
)

$ErrorActionPreference = 'Stop'

function Resolve-TranscriptOutputDir {
    param([string]$MediaPath, [string]$ExplicitOutputDir)
    if ($ExplicitOutputDir) {
        return [System.IO.Path]::GetFullPath($ExplicitOutputDir)
    }
    $parent = Split-Path -Parent ([System.IO.Path]::GetFullPath($MediaPath))
    return (Join-Path $parent 'Transcripts')
}

function Resolve-SpeakerClipOutputDir {
    param([string]$TranscriptDir, [string]$ExplicitClipDir)
    if ($ExplicitClipDir) {
        return [System.IO.Path]::GetFullPath($ExplicitClipDir)
    }
    $parent = Split-Path -Parent ([System.IO.Path]::GetFullPath($TranscriptDir))
    return (Join-Path $parent 'speaker-clips')
}

function Resolve-BaseName {
    param([string]$MediaPath)
    $stem = [System.IO.Path]::GetFileNameWithoutExtension($MediaPath)
    $safe = $stem -replace '[^\p{L}\p{Nd}_-]+', '_'
    $safe = $safe -replace '_+', '_'
    return $safe.Trim('_')
}

$resolvedInput = [System.IO.Path]::GetFullPath($InputPath)
if (-not (Test-Path -LiteralPath $resolvedInput)) {
    throw "Input file not found: $resolvedInput"
}
if (-not (Test-Path -LiteralPath $PythonBin)) {
    throw "Python runtime not found: $PythonBin"
}
if (-not (Test-Path -LiteralPath $WorkerPath)) {
    throw "Worker script not found: $WorkerPath"
}

$resolvedOutputDir = Resolve-TranscriptOutputDir -MediaPath $resolvedInput -ExplicitOutputDir $OutputDir
$resolvedClipDir = Resolve-SpeakerClipOutputDir -TranscriptDir $resolvedOutputDir -ExplicitClipDir $SpeakerClipOutputDir
New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null
New-Item -ItemType Directory -Force -Path $resolvedClipDir | Out-Null

$computeType = if ($RuntimeDevice -eq 'cuda') { 'float16' } else { 'int8' }
$baseName = Resolve-BaseName -MediaPath $resolvedInput
$payloadPath = Join-Path $env:TEMP "$baseName-direct-payload.json"

$hfToken = if ($env:HF_TOKEN) { $env:HF_TOKEN } elseif ($env:HUGGINGFACE_TOKEN) { $env:HUGGINGFACE_TOKEN } else { $null }

$payload = [ordered]@{
    input_path = $resolvedInput
    output_dir = $resolvedOutputDir
    session_artifact_dir = if ($SessionArtifactDir) { [System.IO.Path]::GetFullPath($SessionArtifactDir) } else { $resolvedOutputDir }
    speaker_clip_output_dir = $resolvedClipDir
    execution_mode = $ExecutionMode
    work_root = $WorkRoot
    language_hint = $LanguageHint
    mixed_language = $true
    timestamps = $true
    speaker_labels = $false
    quality_preset = $Model
    requested_model = $Model
    selected_model = $Model
    execution_profile = if ($Model -eq 'small') { 'safe-small' } elseif ($Model -eq 'large-v3') { 'quality-large' } else { 'default-medium' }
    output_formats = @('md', 'json', 'vtt')
    model_root = $ModelRoot
    ffmpeg_bin = $FfmpegBin
    ffprobe_bin = $FfprobeBin
    diarization_model = $DiarizationModel
    hf_token = $hfToken
    speaker_mode = $SpeakerMode
    speaker_map = @{}
    min_speakers = $null
    max_speakers = $null
    chunk_minutes = $ChunkMinutes
    chunk_overlap_sec = $ChunkOverlapSec
    max_parallel_chunks = $MaxParallelChunks
    enable_processing_logs = $true
    voiceprint_mode = $VoiceprintMode
    voiceprint_threshold = $VoiceprintThreshold
    voiceprint_enroll_name = $(if ($VoiceprintEnrollName) { $VoiceprintEnrollName } else { $null })
    voiceprint_contact_ref = $(if ($VoiceprintContactRef) { $VoiceprintContactRef } else { $null })
    voiceprint_contact_name = $(if ($VoiceprintContactName) { $VoiceprintContactName } else { $null })
    machine_local_voiceprint_store_path = $ProfileStorePath
    profile_store_path = $ProfileStorePath
    generate_speaker_clips = ($SpeakerMode -eq 'diarize')
    speaker_clip_target_sec = 60
    speaker_clip_min_turn_sec = 2.0
    speaker_clip_crf = 18
    speaker_clip_preset = 'medium'
    speaker_clip_dir_mode = 'both'
    runtime = @{
        device = $RuntimeDevice
        compute_type = $computeType
    }
    environment = @{
        warnings = @()
    }
}

if ($IncludePlainTxt) {
    $payload['output_formats'] = @('md', 'json', 'vtt', 'txt')
}
if ($ForceFreshAsr) {
    $payload['force_fresh_asr'] = $true
}
if ($NoAsrAutoRecover) {
    $payload['auto_recover_asr'] = $false
}

if ($ExecutionMode -eq 'speaker_pass' -and $AsrRawJsonPath) {
    $payload['asr_raw_json_path'] = [System.IO.Path]::GetFullPath($AsrRawJsonPath)
}

if ($AsrVariantId) {
    $payload['asr_variant_id'] = $AsrVariantId
}

if ($ZoomVttPath) {
    $zoomResolved = [System.IO.Path]::GetFullPath($ZoomVttPath)
    if (-not (Test-Path -LiteralPath $zoomResolved)) {
        throw "ZoomVttPath not found: $zoomResolved"
    }
    $payload['zoom_vtt_path'] = $zoomResolved
}

if ($ProjectSpeakerRegistryPath) {
    $registryResolved = [System.IO.Path]::GetFullPath($ProjectSpeakerRegistryPath)
    $payload['project_speaker_registry_path'] = $registryResolved
}

if ($SpeakerMapPath) {
    $mapResolved = [System.IO.Path]::GetFullPath($SpeakerMapPath)
    if (-not (Test-Path -LiteralPath $mapResolved)) {
        throw "SpeakerMapPath not found: $mapResolved"
    }
    # Кириллица: worker читает UTF-8 JSON с диска (speaker_map_path), не встраиваем карту в stdin.
    $payload['speaker_map_path'] = $mapResolved
}

$payloadJson = $payload | ConvertTo-Json -Depth 10
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($payloadPath, $payloadJson, $utf8NoBom)

if ($Background -and $StderrLog) {
    throw '-StderrLog is for foreground runs only; with -Background use -StderrPath (or default stderr next to output).'
}

if ($Background) {
    # RU: не запускайте параллельно несколько транскрибаций на один InputPath (гонка в Transcripts).
    Write-Warning 'Background: do not run parallel transcriptions for the same InputPath (race on Transcripts).'
    if (-not $StdoutPath) {
        $StdoutPath = Join-Path $resolvedOutputDir "$baseName-direct-stdout.log"
    }
    if (-not $StderrPath) {
        $StderrPath = Join-Path $resolvedOutputDir "$baseName-direct-stderr.log"
    }
    if (Test-Path -LiteralPath $StdoutPath) { Remove-Item -LiteralPath $StdoutPath -Force }
    if (Test-Path -LiteralPath $StderrPath) { Remove-Item -LiteralPath $StderrPath -Force }
    $command = "`$ErrorActionPreference='Stop'; Get-Content -LiteralPath '$payloadPath' -Raw | & '$PythonBin' '$WorkerPath'"
    $proc = Start-Process -FilePath powershell.exe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command) `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -PassThru `
        -WindowStyle Hidden
    [pscustomobject]@{
        mode = 'background'
        pid = $proc.Id
        input = $resolvedInput
        execution_mode = $ExecutionMode
        output_dir = $resolvedOutputDir
        speaker_clip_output_dir = $resolvedClipDir
        stdout = $StdoutPath
        stderr = $StderrPath
        payload = $payloadPath
    } | ConvertTo-Json -Depth 5
    return
}

if ($StderrLog) {
    # Start-Process + RedirectStandardError: без этого PowerShell 5.1 помечает каждую строку stderr python как NativeCommandError при 2>.
    $stderrResolved = [System.IO.Path]::GetFullPath($StderrLog)
    $stderrParent = Split-Path -Parent $stderrResolved
    if ($stderrParent -and -not (Test-Path -LiteralPath $stderrParent)) {
        New-Item -ItemType Directory -Force -Path $stderrParent | Out-Null
    }
    $payloadResolved = [System.IO.Path]::GetFullPath($payloadPath)
    $tempStdout = Join-Path $env:TEMP ("{0}-worker-stdout-{1}.json" -f $baseName, ([Guid]::NewGuid().ToString('n').Substring(0, 8)))
    try {
        if (Test-Path -LiteralPath $tempStdout) { Remove-Item -LiteralPath $tempStdout -Force }
        if (Test-Path -LiteralPath $stderrResolved) { Remove-Item -LiteralPath $stderrResolved -Force }
        Write-Host "[run-media-transcribe-direct] Прогресс worker (stderr) пишется в файл — откройте второе окно:" -ForegroundColor Cyan
        Write-Host "  Get-Content -LiteralPath '$stderrResolved' -Wait -Tail 30" -ForegroundColor Cyan
        Write-Host "[run-media-transcribe-direct] В этой консоли появится только итоговый JSON после завершения." -ForegroundColor DarkGray
        $proc = Start-Process -FilePath $PythonBin `
            -ArgumentList @($WorkerPath) `
            -RedirectStandardInput $payloadResolved `
            -RedirectStandardOutput $tempStdout `
            -RedirectStandardError $stderrResolved `
            -NoNewWindow -Wait -PassThru
        if (Test-Path -LiteralPath $tempStdout) {
            Get-Content -LiteralPath $tempStdout -Raw -Encoding UTF8 | Write-Output
        }
        if ($null -ne $proc.ExitCode) {
            $global:LASTEXITCODE = $proc.ExitCode
        }
        if ($proc.ExitCode -ne 0) {
            Write-Warning ("Python exit code: {0}; stderr: {1}" -f $proc.ExitCode, $stderrResolved)
        }
    } finally {
        if (Test-Path -LiteralPath $tempStdout) {
            Remove-Item -LiteralPath $tempStdout -Force -ErrorAction SilentlyContinue
        }
    }
} else {
    Get-Content -LiteralPath $payloadPath -Raw | & $PythonBin $WorkerPath
}
