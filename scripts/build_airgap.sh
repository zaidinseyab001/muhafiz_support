#!/usr/bin/env bash
# Build the airgap bundle on a CONNECTED machine (needs internet; does NOT need
# enough VRAM to run the models — `ollama pull` / model downloads only write to
# disk).
#
# Output: ../muhafiz_support.tar — a single tarball you ship to the airgapped host.
#
# What it does:
#   1. Download the Whisper large-v3 weights into whisper/models/ (the Dockerfile
#      bakes them in; the airgapped host never downloads anything).
#   2. `docker compose build` — builds all FOUR images:
#        - muhafiz/ollama          bakes the LLM blobs (~106 GB) into the layer
#        - muhafiz/yolo_inference  CUDA13 + cu130 torch + yolo11x prefetched
#        - muhafiz/live_data_feeds FastAPI HLS/audio fan-out
#        - muhafiz/whisper         CUDA + transformers + whisper-large-v3 baked in
#   3. Smoke-test each service that CAN run on the build box (whisper, yolo,
#      live_feeds). Ollama is skipped on purpose — its models don't fit a build
#      box's GPU and can't be loaded here.
#   4. `docker save` all 4 images into ./_bundle/images/*.tar.
#   5. tar the project tree + bundle into ../muhafiz_support.tar.
#
# Env toggles:
#   SKIP_SMOKE=1   skip step 3 (e.g. no NVIDIA Container Toolkit on this box)
#   SKIP_MODEL=1   skip step 1 (weights already present in whisper/models)
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
WHISPER_IMAGE="muhafiz/whisper:latest"

cd "${REPO_ROOT}"

WHISPER_PORT="$(grep -E '^WHISPER_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
WHISPER_PORT="${WHISPER_PORT:-8000}"

echo "==> [1/5] Downloading Whisper large-v3 weights (skip with SKIP_MODEL=1)"
if [ "${SKIP_MODEL:-0}" = "1" ]; then
    echo "    SKIP_MODEL=1 — assuming whisper/models/whisper-large-v3 already present"
else
    bash "${REPO_ROOT}/whisper/01_download_model.sh"
fi
[ -f "${REPO_ROOT}/whisper/models/whisper-large-v3/model.safetensors" ] || {
    echo "ERROR: whisper/models/whisper-large-v3/model.safetensors missing — model download failed." >&2
    exit 1
}

echo "==> [2/5] Cleaning previous bundle staging"
rm -rf "${BUNDLE_DIR}"
mkdir -p "${IMAGES_DIR}"

echo "==> [3/5] Building all images (slow step — ~106 GB of LLM blobs pull into"
echo "         the ollama image, plus cu130 torch into yolo and whisper-large-v3"
echo "         into whisper). Budget a few hours + plenty of disk."
docker compose -p "${PROJECT_NAME}" build

# ---------------------------------------------------------------------------
# Smoke tests — verify each runnable service. Ollama is intentionally skipped.
# ---------------------------------------------------------------------------
smoke_test() {
    echo "==> [4/5] Smoke-testing services (ollama skipped — models can't run here)"

    echo "    [whisper]  booting + waiting for model load on GPU..."
    docker compose -p "${PROJECT_NAME}" up -d whisper
    local ok=0
    for _ in $(seq 1 60); do
        if curl -fsS "http://localhost:${WHISPER_PORT}/health" 2>/dev/null | grep -q '"status":"ok"'; then
            ok=1; break
        fi
        sleep 5
    done
    if [ "${ok}" = "1" ]; then
        echo "    [whisper]  OK — /health reports ready"
    else
        echo "    [whisper]  FAILED to become ready; recent logs:" >&2
        docker compose -p "${PROJECT_NAME}" logs --tail=40 whisper >&2 || true
        docker compose -p "${PROJECT_NAME}" down >/dev/null 2>&1 || true
        exit 1
    fi
    docker compose -p "${PROJECT_NAME}" down >/dev/null 2>&1 || true

    echo "    [yolo]     verifying torch + ultralytics + yolo11x weights..."
    docker run --rm --gpus all --entrypoint python "${YOLO_IMAGE}" -c \
        "import torch, ultralytics; from ultralytics import YOLO; YOLO('yolo11x.pt'); \
print('yolo OK; cuda=%s' % torch.cuda.is_available())" \
        || echo "    [yolo]     WARNING: runtime check failed (GPU toolkit missing on build box?)"

    echo "    [live_feeds] verifying app imports + ffmpeg..."
    docker run --rm --entrypoint sh "${LIVE_FEEDS_IMAGE}" -c \
        "ffmpeg -version >/dev/null 2>&1 && python -c 'import fastapi, uvicorn; print(\"live_feeds OK\")'" \
        || echo "    [live_feeds] WARNING: import/ffmpeg check failed"

    echo "    [ollama]   SKIPPED — LLM blobs are baked in but exceed a build box's"
    echo "               GPU; they are validated on the H200 at deploy time."
}

if [ "${SKIP_SMOKE:-0}" = "1" ]; then
    echo "==> [4/5] Smoke tests skipped (SKIP_SMOKE=1)"
else
    smoke_test
fi

echo "==> [5/5] Saving images"
docker save "${OLLAMA_IMAGE}"      -o "${IMAGES_DIR}/ollama.tar"
docker save "${YOLO_IMAGE}"        -o "${IMAGES_DIR}/yolo_inference.tar"
docker save "${LIVE_FEEDS_IMAGE}"  -o "${IMAGES_DIR}/live_data_feeds.tar"
docker save "${WHISPER_IMAGE}"     -o "${IMAGES_DIR}/whisper.tar"

echo "==> Bundling everything into ${OUT_TAR}"
PARENT="$(dirname "${REPO_ROOT}")"
PROJECT_FOLDER="$(basename "${REPO_ROOT}")"

tar -C "${PARENT}" -cf "${OUT_TAR}" \
    --exclude="${PROJECT_FOLDER}/.git" \
    --exclude="${PROJECT_FOLDER}/.venv" \
    --exclude="${PROJECT_FOLDER}/**/__pycache__" \
    --exclude="${PROJECT_FOLDER}/live_data_feeds/.normalized" \
    --exclude="${PROJECT_FOLDER}/live_data_feeds/.tmp" \
    --exclude="${PROJECT_FOLDER}/whisper/build_logs" \
    --exclude="${PROJECT_FOLDER}/whisper/models" \
    "${PROJECT_FOLDER}"

echo ""
echo "Bundle ready: ${OUT_TAR}"
ls -lh "${OUT_TAR}"
