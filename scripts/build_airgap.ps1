# Build the airgap bundle on a CONNECTED Windows host (Docker Desktop).
#
# Output: ..\muhafiz_support.tar
#
# Mirrors scripts/build_airgap.sh: download whisper model, build, (optional)
# smoke test, save, tar. Models are baked into the images.
#
# Env toggles:
#   $env:SKIP_SMOKE="1"   skip the whisper health smoke test
#   $env:SKIP_MODEL="1"   skip the whisper model download

$ErrorActionPreference = "Stop"

$RepoRoot       = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BundleDir      = Join-Path $RepoRoot "_bundle"
$ImagesDir      = Join-Path $BundleDir "images"
$ParentDir      = Split-Path $RepoRoot -Parent
$ProjectFolder  = Split-Path $RepoRoot -Leaf
$OutTar         = Join-Path $ParentDir "muhafiz_support.tar"

$ProjectName        = "muhafiz_support"
$OllamaImage        = "muhafiz/ollama:latest"
$YoloImage          = "muhafiz/yolo_inference:latest"
$LiveFeedsImage     = "muhafiz/live_data_feeds:latest"
$WhisperImage       = "muhafiz/whisper:latest"

Set-Location $RepoRoot

# Read WHISPER_PORT from .env (default 8000)
$WhisperPort = "8000"
$envLine = Select-String -Path (Join-Path $RepoRoot ".env") -Pattern '^WHISPER_PORT=' -ErrorAction SilentlyContinue | Select-Object -Last 1
if ($envLine) { $WhisperPort = ($envLine.Line -split '=',2)[1].Trim() }

Write-Host "==> [1/5] Downloading Whisper large-v3 weights (skip with `$env:SKIP_MODEL=1)"
$ModelDir = Join-Path $RepoRoot "whisper\models\whisper-large-v3"
if ($env:SKIP_MODEL -eq "1") {
    Write-Host "    SKIP_MODEL=1 - assuming weights already present"
} else {
    python -c "import huggingface_hub" 2>$null
    if ($LASTEXITCODE -ne 0) { python -m pip install --quiet huggingface-hub; if ($LASTEXITCODE -ne 0) { throw 'pip install huggingface-hub failed' } }
    $py = @"
from huggingface_hub import snapshot_download
snapshot_download(repo_id='openai/whisper-large-v3', local_dir=r'$ModelDir', repo_type='model',
    ignore_patterns=['*.bin','*.h5','*.msgpack','*.ot','*.tflite','tf_model*','flax_model*'])
print('whisper model downloaded')
"@
    $py | python -
    if ($LASTEXITCODE -ne 0) { throw "whisper model download failed" }
}
if (-not (Test-Path (Join-Path $ModelDir "model.safetensors"))) { throw "whisper model.safetensors missing after download" }

Write-Host "==> [2/5] Cleaning previous bundle staging"
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null

Write-Host "==> [3/5] Building all images (slow step - ~106 GB of LLM blobs baked in)"
docker compose -p $ProjectName build
if ($LASTEXITCODE -ne 0) { throw "docker compose build failed" }

Write-Host "==> [4/5] Smoke test (ollama skipped - models can't run on a build box)"
if ($env:SKIP_SMOKE -eq "1") {
    Write-Host "    SKIP_SMOKE=1 - skipping whisper health test"
} else {
    docker compose -p $ProjectName up -d whisper
    if ($LASTEXITCODE -ne 0) { throw "whisper failed to start" }
    $ok = $false
    foreach ($i in 1..60) {
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:$WhisperPort/health" -TimeoutSec 5
            if ($resp.Content -match '"status":"ok"') { $ok = $true; break }
        } catch {}
        Start-Sleep -Seconds 5
    }
    docker compose -p $ProjectName logs --tail=40 whisper
    docker compose -p $ProjectName down | Out-Null
    if (-not $ok) { throw "whisper smoke test failed (not ready)" }
    Write-Host "    [whisper] OK"
}

Write-Host "==> [5/5] Saving images"
docker save $OllamaImage     -o (Join-Path $ImagesDir "ollama.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save ollama failed" }
docker save $YoloImage       -o (Join-Path $ImagesDir "yolo_inference.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save yolo failed" }
docker save $LiveFeedsImage  -o (Join-Path $ImagesDir "live_data_feeds.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save live_feeds failed" }
docker save $WhisperImage    -o (Join-Path $ImagesDir "whisper.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save whisper failed" }

Write-Host "==> Bundling everything into $OutTar"
if (Test-Path $OutTar) { Remove-Item -Force $OutTar }

$tarArgs = @(
    "-C", $ParentDir,
    "-cf", $OutTar,
    "--exclude=$ProjectFolder/.git",
    "--exclude=$ProjectFolder/.venv",
    "--exclude=$ProjectFolder/**/__pycache__",
    "--exclude=$ProjectFolder/live_data_feeds/.normalized",
    "--exclude=$ProjectFolder/live_data_feeds/.tmp",
    "--exclude=$ProjectFolder/whisper/build_logs",
    "--exclude=$ProjectFolder/whisper/models",
    $ProjectFolder
)
& tar @tarArgs
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

Write-Host ""
Write-Host "Bundle ready: $OutTar"
Get-Item $OutTar | Select-Object Name, @{N="SizeGB";E={[math]::Round($_.Length/1GB,2)}}, LastWriteTime
