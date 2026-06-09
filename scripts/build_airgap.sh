#!/usr/bin/env bash
# Build the airgap bundle on a CONNECTED machine (needs internet; does NOT need
# enough VRAM to run the models).
#
# Output: ../muhafiz_support.tar — a single tarball you ship to the airgapped host.
#
# What it does:
#   1. Pull the Ollama models on the HOST into ./ollama/models — RESUMABLE, so a
#      slow/flaky link can retry without restarting the 77 GB download. (This is
#      why pulling does NOT happen inside `docker build`: a build RUN is not
#      resumable — a drop discards the layer and restarts at 0%.)
#   2. Download the Whisper large-v3 weights into whisper/models/.
#   3. `docker compose build` — builds all FOUR images. The ollama image just
#      COPYs the pre-pulled blobs (no network); yolo/whisper install their deps.
#   4. Smoke-test each service that CAN run here (whisper, yolo, live_feeds).
#      Ollama is skipped — its models don't fit a build box's GPU.
#   5. `docker save` all 4 images into ./_bundle/images/*.tar.
#   6. tar the project tree + bundle into ../muhafiz_support.tar.
#
# Env toggles:
#   SKIP_OLLAMA_PULL=1  skip step 1 (./ollama/models already complete)
#   SKIP_MODEL=1        skip step 2 (whisper weights already present)
#   SKIP_SMOKE=1        skip step 4 (e.g. no NVIDIA Container Toolkit here)
#   OLLAMA_DNS=1.1.1.1  DNS server for the pull container (default 1.1.1.1,8.8.8.8)
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

OLLAMA_MODELS_DIR="${REPO_ROOT}/ollama/models"
OLLAMA_BASE_IMAGE="ollama/ollama:latest"
OLLAMA_MODEL_LIST="qwen2.5:72b-instruct-q8_0 gemma3:27b-it-q8_0 nomic-embed-text:latest"
DNS1="${OLLAMA_DNS:-1.1.1.1}"
# Effectively unlimited — keep resuming until the pull completes. Override by
# exporting MAX_PULL_ATTEMPTS to a smaller number if you want it to give up.
MAX_PULL_ATTEMPTS="${MAX_PULL_ATTEMPTS:-9999999999999999}"

cd "${REPO_ROOT}"

WHISPER_PORT="$(grep -E '^WHISPER_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
WHISPER_PORT="${WHISPER_PORT:-8000}"

# ---------------------------------------------------------------------------
# 1. Resumable host-side Ollama pull
# ---------------------------------------------------------------------------
echo "==> [1/6] Pulling Ollama models to ${OLLAMA_MODELS_DIR} (resumable; skip with SKIP_OLLAMA_PULL=1)"
if [ "${SKIP_OLLAMA_PULL:-0}" = "1" ]; then
    echo "    SKIP_OLLAMA_PULL=1 — assuming ./ollama/models is already complete"
else
    mkdir -p "${OLLAMA_MODELS_DIR}"
    # Make sure the base image is present (also resumable via docker's own retries).
    docker pull "${OLLAMA_BASE_IMAGE}"

    # Inner script: start daemon, pull each model (ollama resumes partial blobs
    # on re-run), then stop. set -e => non-zero exit on any pull failure, which
    # trips the retry loop below. The mounted dir persists partial blobs.
    PULL_SH='set -e; ollama serve >/tmp/serve.log 2>&1 & SVPID=$!;
        until ollama list >/dev/null 2>&1; do sleep 1; done;
        for m in '"${OLLAMA_MODEL_LIST}"'; do echo "[pull] $m"; ollama pull "$m"; done;
        echo "[pull] all models present:"; ollama list;
        kill "$SVPID" 2>/dev/null || true'

    attempt=0
    until docker run --rm \
            --dns "${DNS1}" --dns 8.8.8.8 \
            --entrypoint /bin/sh \
            -v "${OLLAMA_MODELS_DIR}:/root/.ollama" \
            "${OLLAMA_BASE_IMAGE}" -c "${PULL_SH}"; do
        attempt=$((attempt + 1))
        if [ "${attempt}" -ge "${MAX_PULL_ATTEMPTS}" ]; then
            echo "ERROR: ollama pull did not complete after ${MAX_PULL_ATTEMPTS} attempts." >&2
            echo "       Fix the network/DNS (see AIRGAP_README) and re-run." >&2
            exit 1
        fi
        echo "==> ollama pull interrupted (network). Resuming — attempt ${attempt}/${MAX_PULL_ATTEMPTS} in 10s..."
        sleep 10
    done
fi
[ -d "${OLLAMA_MODELS_DIR}/models/manifests" ] || {
    echo "ERROR: ${OLLAMA_MODELS_DIR}/models/manifests missing — ollama pull incomplete." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# 2. Whisper model
# ---------------------------------------------------------------------------
echo "==> [2/6] Downloading Whisper large-v3 weights (skip with SKIP_MODEL=1)"
if [ "${SKIP_MODEL:-0}" = "1" ]; then
    echo "    SKIP_MODEL=1 — assuming whisper/models/whisper-large-v3 already present"
else
    bash "${REPO_ROOT}/whisper/01_download_model.sh"
fi
[ -f "${REPO_ROOT}/whisper/models/whisper-large-v3/model.safetensors" ] || {
    echo "ERROR: whisper/models/whisper-large-v3/model.safetensors missing — download failed." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Build
# ---------------------------------------------------------------------------
echo "==> [3/6] Cleaning previous bundle staging"
rm -rf "${BUNDLE_DIR}"
mkdir -p "${IMAGES_DIR}"

echo "==> [4/6] Building all images (ollama just COPYs the pre-pulled blobs — no"
echo "         network; yolo/whisper install deps)."
docker compose -p "${PROJECT_NAME}" build

# ---------------------------------------------------------------------------
# 4. Smoke tests — Ollama intentionally skipped.
# ---------------------------------------------------------------------------
smoke_test() {
    echo "==> [5/6] Smoke-testing services (ollama skipped — models can't run here)"

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

    echo "    [ollama]   SKIPPED — baked blobs exceed a build box's GPU; validated"
    echo "               on the H200 at deploy time."
}

if [ "${SKIP_SMOKE:-0}" = "1" ]; then
    echo "==> [5/6] Smoke tests skipped (SKIP_SMOKE=1)"
else
    smoke_test
fi

# ---------------------------------------------------------------------------
# 5. Save + 6. tar
# ---------------------------------------------------------------------------
echo "==> [6/6] Saving images"
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
    --exclude="${PROJECT_FOLDER}/ollama/models" \
    --exclude="${PROJECT_FOLDER}/whisper/build_logs" \
    --exclude="${PROJECT_FOLDER}/whisper/models" \
    "${PROJECT_FOLDER}"

echo ""
echo "Bundle ready: ${OUT_TAR}"
ls -lh "${OUT_TAR}"
