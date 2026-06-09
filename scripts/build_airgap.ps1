# Build the airgap bundle on a CONNECTED Windows host (Docker Desktop).
#
# Output: ..\muhafiz_support.tar
#
# Mirrors scripts/build_airgap.sh. Models are baked into the images, so this
# script is just: build, save, tar.

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

Set-Location $RepoRoot

Write-Host "==> [1/3] Cleaning previous bundle staging"
if (Test-Path $BundleDir) { Remove-Item -Recurse -Force $BundleDir }
New-Item -ItemType Directory -Force -Path $ImagesDir | Out-Null

Write-Host "==> [2/3] Building all images (slow step - ~106 GB of LLM blobs baked in)"
docker compose -p $ProjectName build
if ($LASTEXITCODE -ne 0) { throw "docker compose build failed" }

Write-Host "==> [3/3] Saving images"
docker save $OllamaImage     -o (Join-Path $ImagesDir "ollama.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save ollama failed" }
docker save $YoloImage       -o (Join-Path $ImagesDir "yolo_inference.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save yolo failed" }
docker save $LiveFeedsImage  -o (Join-Path $ImagesDir "live_data_feeds.tar")
if ($LASTEXITCODE -ne 0) { throw "docker save live_feeds failed" }

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
    $ProjectFolder
)
& tar @tarArgs
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

Write-Host ""
Write-Host "Bundle ready: $OutTar"
Get-Item $OutTar | Select-Object Name, @{N="SizeGB";E={[math]::Round($_.Length/1GB,2)}}, LastWriteTime
