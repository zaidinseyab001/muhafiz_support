#!/usr/bin/env bash
# Build the airgap bundle on a CONNECTED machine.
#
# Output: ../muhafiz_support.tar — a single tarball you ship to the airgapped host.
#
# What it does:
#   1. `docker compose build` — builds all three images, including the custom
#      `muhafiz/ollama` that bakes the LLM models into the image layer, and
#      the yolo image which prefetches yolo11n.pt during build.
#   2. `docker save` all 3 images into ./_bundle/images/*.tar.
#   3. tars the project tree + bundle into ../muhafiz_support.tar.
#
# No volume export step — models live inside the images themselves.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_DIR="${REPO_ROOT}/_bundle"
IMAGES_DIR="${BUNDLE_DIR}/images"
OUT_TAR="${REPO_ROOT}/../muhafiz_support.tar"

PROJECT_NAME="muhafiz_support"
OLLAMA_IMAGE="muhafiz/ollama:latest"
YOLO_IMAGE="muhafiz/yolo_inference:latest"
LIVE_FEEDS_IMAGE="muhafiz/live_data_feeds:latest"

cd "${REPO_ROOT}"

echo "==> [1/3] Cleaning previous bundle staging"
rm -rf "${BUNDLE_DIR}"
mkdir -p "${IMAGES_DIR}"

echo "==> [2/3] Building all images (this is the slow step — ~106 GB of LLM blobs"
echo "         get pulled into the ollama image, plus PyTorch cu130 wheels into yolo)"
docker compose -p "${PROJECT_NAME}" build

echo "==> [3/3] Saving images"
docker save "${OLLAMA_IMAGE}"      -o "${IMAGES_DIR}/ollama.tar"
docker save "${YOLO_IMAGE}"        -o "${IMAGES_DIR}/yolo_inference.tar"
docker save "${LIVE_FEEDS_IMAGE}"  -o "${IMAGES_DIR}/live_data_feeds.tar"

echo "==> Bundling everything into ${OUT_TAR}"
PARENT="$(dirname "${REPO_ROOT}")"
PROJECT_FOLDER="$(basename "${REPO_ROOT}")"

tar -C "${PARENT}" -cf "${OUT_TAR}" \
    --exclude="${PROJECT_FOLDER}/.git" \
    --exclude="${PROJECT_FOLDER}/.venv" \
    --exclude="${PROJECT_FOLDER}/**/__pycache__" \
    --exclude="${PROJECT_FOLDER}/live_data_feeds/.normalized" \
    --exclude="${PROJECT_FOLDER}/live_data_feeds/.tmp" \
    "${PROJECT_FOLDER}"

echo ""
echo "Bundle ready: ${OUT_TAR}"
ls -lh "${OUT_TAR}"
