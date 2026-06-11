#!/usr/bin/env bash
# Build the airgap bundle on a CONNECTED machine (needs internet; does NOT need
# enough VRAM to run the models).
#
# Usage:
#   ./build_airgap.sh                build ALL services -> ../muhafiz_support.tar
#   ./build_airgap.sh <service>      build ONE service and (re)save only its image
#                                    tar in _bundle/images/. <service> is one of:
#                                    ollama | yolo | live_feeds | whisper
#
# What the full run does:
#   1. Pull the Ollama models on the HOST into ./ollama/models — RESUMABLE, and
#      skipped automatically if already complete.
#   2. Download the Whisper large-v3 weights into whisper/models/ (skipped if present).
#   3. `docker compose build` the image(s).
#   4. Smoke-test each runnable service (whisper, yolo, live_feeds). Ollama is
#      skipped — its models don't fit a build box's GPU (but they ARE downloaded
#      and baked into the image).
#   5. `docker save` the image(s) into ./_bundle/images/*.tar.
#   6. (full builds only) tar the project tree + bundle into ../muhafiz_support.tar.
#
# Env toggles:
#   SKIP_OLLAMA_PULL=1  skip step 1 (./ollama/models already complete)
#   SKIP_MODEL=1        skip step 2 (whisper weights already present)
#   SKIP_SMOKE=1        skip step 4 (e.g. no NVIDIA Container Toolkit here)
#   OLLAMA_DNS=1.1.1.1  DNS server for the pull container (default 1.1.1.1,8.8.8.8)
#   MAX_PULL_ATTEMPTS=N stop retrying the ollama pull after N attempts
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
MAX_PULL_ATTEMPTS="${MAX_PULL_ATTEMPTS:-9999999}"

cd "${REPO_ROOT}"

WHISPER_PORT="$(grep -E '^WHISPER_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')"
WHISPER_PORT="${WHISPER_PORT:-8000}"

# ---------------------------------------------------------------------------
# Optional single-service selection
# ---------------------------------------------------------------------------
SERVICE="${1:-}"
if [ -n "${SERVICE}" ]; then
    case " ollama yolo live_feeds whisper " in
        *" ${SERVICE} "*) : ;;
        *) echo "ERROR: unknown service '${SERVICE}'. Valid: ollama | yolo | live_feeds | whisper (or omit for all)." >&2; exit 1 ;;
    esac
    echo "==> Single-service build: ${SERVICE} (other services left untouched)"
fi
# want <svc> -> true when building everything, or specifically that service.
want() { [ -z "${SERVICE}" ] || [ "${SERVICE}" = "$1" ]; }

# True only if EVERY model in OLLAMA_MODEL_LIST already has its manifest on disk.
ollama_models_present() {
    local base="${OLLAMA_MODELS_DIR}/models/manifests/registry.ollama.ai/library"
    local m name tag
    for m in ${OLLAMA_MODEL_LIST}; do
        name="${m%%:*}"; tag="${m##*:}"
        [ -f "${base}/${name}/${tag}" ] || return 1
    done
    return 0
}

# ---------------------------------------------------------------------------
# 1. Resumable host-side Ollama pull (only if building ollama)
# ---------------------------------------------------------------------------
if want ollama; then
    echo "==> [1/6] Pulling Ollama models to ${OLLAMA_MODELS_DIR} (resumable; skip with SKIP_OLLAMA_PULL=1)"
    if [ "${SKIP_OLLAMA_PULL:-0}" = "1" ]; then
        echo "    SKIP_OLLAMA_PULL=1 — assuming ./ollama/models is already complete"
    elif ollama_models_present; then
        echo "    all Ollama models already present in ./ollama/models — skipping download"
    else
        mkdir -p "${OLLAMA_MODELS_DIR}"
        docker pull "${OLLAMA_BASE_IMAGE}"

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
fi

# ---------------------------------------------------------------------------
# 2. Whisper model (only if building whisper)
# ---------------------------------------------------------------------------
if want whisper; then
    echo "==> [2/6] Downloading Whisper large-v3 weights (skip with SKIP_MODEL=1; auto-skips if present)"
    if [ "${SKIP_MODEL:-0}" = "1" ]; then
        echo "    SKIP_MODEL=1 — assuming whisper/models/whisper-large-v3 already present"
    else
        bash "${REPO_ROOT}/whisper/01_download_model.sh"
    fi
    [ -f "${REPO_ROOT}/whisper/models/whisper-large-v3/model.safetensors" ] || {
        echo "ERROR: whisper/models/whisper-large-v3/model.safetensors missing — download failed." >&2
        exit 1
    }
fi

# ---------------------------------------------------------------------------
# 3. Staging — wipe only on a full build; for a single service keep the other
#    services' existing image tars in place.
# ---------------------------------------------------------------------------
echo "==> [3/6] Preparing bundle staging"
if [ -z "${SERVICE}" ]; then
    rm -rf "${BUNDLE_DIR}"
fi
mkdir -p "${IMAGES_DIR}"

# ---------------------------------------------------------------------------
# 4. Build
# ---------------------------------------------------------------------------
echo "==> [4/6] Building image(s)${SERVICE:+ for ${SERVICE}}"
if [ -n "${SERVICE}" ]; then
    docker compose -p "${PROJECT_NAME}" build "${SERVICE}"
else
    docker compose -p "${PROJECT_NAME}" build
fi

# ---------------------------------------------------------------------------
# 5. Smoke tests — per service; Ollama intentionally skipped.
# ---------------------------------------------------------------------------
smoke_test() {
    echo "==> [5/6] Smoke-testing${SERVICE:+ ${SERVICE}} (ollama always skipped — models can't run here)"

    if want whisper; then
        echo "    [whisper]  booting + waiting for model load..."
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
    fi

    if want yolo; then
        echo "    [yolo]     verifying torch + ultralytics + yolo11x weights..."
        docker run --rm --gpus all --entrypoint python "${YOLO_IMAGE}" -c \
            "import torch, ultralytics; from ultralytics import YOLO; YOLO('yolo11x.pt'); \
print('yolo OK; cuda=%s' % torch.cuda.is_available())" \
            || echo "    [yolo]     WARNING: runtime check failed (GPU toolkit missing on build box?)"
    fi

    if want live_feeds; then
        echo "    [live_feeds] verifying app imports + ffmpeg..."
        docker run --rm --entrypoint sh "${LIVE_FEEDS_IMAGE}" -c \
            "ffmpeg -version >/dev/null 2>&1 && python -c 'import fastapi, uvicorn; print(\"live_feeds OK\")'" \
            || echo "    [live_feeds] WARNING: import/ffmpeg check failed"
    fi

    if want ollama; then
        echo "    [ollama]   SKIPPED — baked blobs exceed a build box's GPU; validated"
        echo "               on the H200 at deploy time."
    fi
}

if [ "${SKIP_SMOKE:-0}" = "1" ]; then
    echo "==> [5/6] Smoke tests skipped (SKIP_SMOKE=1)"
else
    smoke_test
fi

# ---------------------------------------------------------------------------
# 6. Save image(s)
# ---------------------------------------------------------------------------
echo "==> [6/6] Saving image(s)"
if want ollama;     then docker save "${OLLAMA_IMAGE}"      -o "${IMAGES_DIR}/ollama.tar"; fi
if want yolo;       then docker save "${YOLO_IMAGE}"        -o "${IMAGES_DIR}/yolo_inference.tar"; fi
if want live_feeds; then docker save "${LIVE_FEEDS_IMAGE}"  -o "${IMAGES_DIR}/live_data_feeds.tar"; fi
if want whisper;    then docker save "${WHISPER_IMAGE}"     -o "${IMAGES_DIR}/whisper.tar"; fi

# ---------------------------------------------------------------------------
# Bundle — only on a full build. Single-service runs just (re)produce the one
# image tar, which you can `docker load` on the target to update that service.
# ---------------------------------------------------------------------------
if [ -n "${SERVICE}" ]; then
    case "${SERVICE}" in
        yolo)       svc_tar="yolo_inference.tar" ;;
        live_feeds) svc_tar="live_data_feeds.tar" ;;
        *)          svc_tar="${SERVICE}.tar" ;;
    esac
    echo ""
    echo "Single-service image ready: ${IMAGES_DIR}/${svc_tar}"
    echo "On the target: docker load -i ${svc_tar}  &&  docker compose -p ${PROJECT_NAME} up -d ${SERVICE}"
    echo "(run ./scripts/build_airgap.sh with no arguments to repackage the full ../muhafiz_support.tar)"
    ls -lh "${IMAGES_DIR}/${svc_tar}"
    exit 0
fi

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
