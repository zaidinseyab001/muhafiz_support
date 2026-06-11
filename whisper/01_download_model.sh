#!/bin/bash
# =============================================================================
# 01_download_model.sh - Download Whisper large-v3 model (via Docker)
# =============================================================================
# Downloads openai/whisper-large-v3 (~3 GB, safetensors) into
# models/whisper-large-v3/ so the Dockerfile can bake it into the image.
#
# Runs the download INSIDE a python container so it does not depend on the build
# box's host Python/pip (which on Ubuntu 24.04 is PEP 668 "externally-managed"
# and rejects `pip install`). Forces working DNS, and snapshot_download resumes
# partial files, so a flaky link just means re-run.
#
# Usage:   ./01_download_model.sh
#          HF_TOKEN=hf_xxx ./01_download_model.sh   # optional: higher HF rate limits
# Toggle:  OLLAMA_DNS / WHISPER_DNS to override the DNS server (default 1.1.1.1)
#          HF_TOKEN  passed through to the download container if set (never baked
#                    into any file — keep it in your shell env only)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_CACHE_DIR="${SCRIPT_DIR}/models"
MODEL_NAME="openai/whisper-large-v3"
MODEL_DIR="${MODEL_CACHE_DIR}/whisper-large-v3"
LOG_FILE="${SCRIPT_DIR}/build_logs/model_download.log"
DNS1="${WHISPER_DNS:-${OLLAMA_DNS:-1.1.1.1}}"
PY_IMAGE="python:3.12-slim"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-9999999}"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
mkdir -p "$(dirname "$LOG_FILE")"
log_info()    { echo -e "${BLUE}[INFO]${NC} $*"    | tee -a "$LOG_FILE"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"     | tee -a "$LOG_FILE"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"    | tee -a "$LOG_FILE"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $*" | tee -a "$LOG_FILE"; }

check_docker() {
    command -v docker >/dev/null 2>&1 || { log_error "docker not found on PATH."; exit 1; }
    docker info >/dev/null 2>&1 || { log_error "cannot talk to the docker daemon (need sudo, or add user to docker group)."; exit 1; }
    log_success "docker available"
}

check_disk_space() {
    local avail_gb
    avail_gb=$(( $(df "$SCRIPT_DIR" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
    log_info "Available disk space: ${avail_gb}GB"
    [ "$avail_gb" -ge 7 ] || { log_error "Need 7+ GB for whisper-large-v3, have ${avail_gb}GB"; exit 1; }
    log_success "Disk space OK"
}

download_model() {
    mkdir -p "$MODEL_CACHE_DIR"

    if [ -f "$MODEL_DIR/model.safetensors" ]; then
        log_success "Model already present — skipping download."
        return 0
    fi

    log_info "Downloading ${MODEL_NAME} (~3 GB) into ${MODEL_DIR} via ${PY_IMAGE}"

    # The container installs huggingface_hub (pip works fine inside the slim
    # image — no PEP 668), then snapshot_download with resume.
    local DL_SH
    DL_SH='set -e
pip install --quiet --no-cache-dir huggingface-hub
python - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="'"${MODEL_NAME}"'",
    local_dir="/out/whisper-large-v3",
    repo_type="model",
    ignore_patterns=["*.bin","*.h5","*.msgpack","*.ot","*.tflite","tf_model*","flax_model*"],
)
print("downloaded")
PY'

    # If HF_TOKEN isn't already in the environment, pick it up from the root .env.
    if [ -z "${HF_TOKEN:-}" ] && [ -f "${SCRIPT_DIR}/../.env" ]; then
        HF_TOKEN="$(grep -E '^HF_TOKEN=' "${SCRIPT_DIR}/../.env" | tail -1 | cut -d= -f2- | tr -d '[:space:]'\''"')"
        export HF_TOKEN
    fi
    if [ -n "${HF_TOKEN:-}" ]; then
        log_info "Using HF_TOKEN (higher rate limits)"
    fi

    local attempt=0
    until docker run --rm \
            --dns "${DNS1}" --dns 8.8.8.8 \
            -e HF_TOKEN="${HF_TOKEN:-}" \
            -v "${MODEL_CACHE_DIR}:/out" \
            "${PY_IMAGE}" sh -c "${DL_SH}"; do
        attempt=$((attempt + 1))
        if [ "${attempt}" -ge "${MAX_ATTEMPTS}" ]; then
            log_error "whisper model download failed after ${MAX_ATTEMPTS} attempts."
            exit 1
        fi
        log_warning "download interrupted; resuming — attempt ${attempt} in 10s..."
        sleep 10
    done

    # Files are written root-owned by the container; hand them back to the user.
    docker run --rm -v "${MODEL_CACHE_DIR}:/out" "${PY_IMAGE}" \
        chown -R "$(id -u):$(id -g)" /out 2>/dev/null || true

    log_success "Download completed"
}

verify_download() {
    for f in model.safetensors config.json; do
        [ -f "$MODEL_DIR/$f" ] || { log_error "Missing file after download: $f"; exit 1; }
    done
    log_success "Model verified ($(du -sh "$MODEL_DIR" | awk '{print $1}'))"
}

main() {
    echo "=============================================================="
    echo "Download Whisper large-v3 model (containerized)"
    echo "=============================================================="
    check_docker
    check_disk_space
    download_model
    verify_download
    echo "Location: $MODEL_DIR"
}

main "$@"
