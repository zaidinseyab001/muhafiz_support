# Build the airgap bundle on a CONNECTED Windows host (Docker Desktop).
#
# Output: ..\muhafiz_support.tar
#
# Mirrors scripts/build_airgap.sh: download whisper model, build, (optional)
# smoke test, save, tar. Models are baked into the images.
#
# Env toggles:
#   $env:SKIP_OLLAMA_PULL="1"  skip the resumable ollama model pull
#   $env:SKIP_SMOKE="1"        skip the whisper health smoke test
#   $env:SKIP_MODEL="1"        skip the whisper model download

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

Write-Host "==> [1/6] Pulling Ollama models to ollama\models (resumable; skip with `$env:SKIP_OLLAMA_PULL=1)"
$OllamaModelsDir = Join-Path $RepoRoot "ollama\models"
if ($env:SKIP_OLLAMA_PULL -eq "1") {
    Write-Host "    SKIP_OLLAMA_PULL=1 - assuming ollama\models already complete"
} else {
    New-Item -ItemType Directory -Force -Path $OllamaModelsDir | Out-Null
    docker pull ollama/ollama:latest
    if ($LASTEXITCODE -ne 0) { throw "docker pull ollama/ollama:latest failed" }
    $pullSh = 'set -e; ollama serve >/tmp/serve.log 2>&1 & SVPID=$!; until ollama list >/dev/null 2>&1; do sleep 1; done; for m in qwen2.5:72b-instruct-q8_0 gemma3:27b-it-q8_0 nomic-embed-text:latest; do echo "[pull] $m"; ollama pull "$m"; done; ollama list; kill "$SVPID" 2>/dev/null || true'
    # Effectively unlimited — keep resuming until the pull completes. Set
    # $env:MAX_PULL_ATTEMPTS to a number to make it give up after that many tries.
    $maxAttempts = if ($env:MAX_PULL_ATTEMPTS) { [int64]$env:MAX_PULL_ATTEMPTS } else { [int64]9999999999999999 }
    $ok = $false
    $i = 0
    while ($i -lt $maxAttempts) {
        $i++
        docker run --rm --dns 1.1.1.1 --dns 8.8.8.8 --entrypoint /bin/sh -v "${OllamaModelsDir}:/root/.ollama" ollama/ollama:latest -c $pullSh
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        Write-Host "==> ollama pull interrupted (network). Resuming - attempt $i in 10s..."
        Start-Sleep -Seconds 10
    }
    if (-not $ok) { throw "ollama pull did not complete (fix network/DNS, then re-run)" }
}
if (-not (Test-Path (Join-Path $OllamaModelsDir "models\manifests"))) { throw "ollama\models incomplete (models\manifests missing)" }

Write-Host "==> [2/6] Downloading Whisper large-v3 weights (skip with `$env:SKIP_MODEL=1)"
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

Write-Host "==> [3/6] Cleaning previous bundle staging"
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null

Write-Host "==> [4/6] Building all images (ollama COPYs pre-pulled blobs - no network)"
docker compose -p $ProjectName build
if ($LASTEXITCODE -ne 0) { throw "docker compose build failed" }

Write-Host "==> [5/6] Smoke test (ollama skipped - models can't run on a build box)"
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

Write-Host "==> [6/6] Saving images"
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
    "--exclude=$ProjectFolder/ollama/models",
    "--exclude=$ProjectFolder/whisper/build_logs",
    "--exclude=$ProjectFolder/whisper/models",
    $ProjectFolder
)
& tar @tarArgs
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

Write-Host ""
Write-Host "Bundle ready: $OutTar"
Get-Item $OutTar | Select-Object Name, @{N="SizeGB";E={[math]::Round($_.Length/1GB,2)}}, LastWriteTime
