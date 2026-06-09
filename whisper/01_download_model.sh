#!/bin/bash
# =============================================================================
# 01_download_model.sh - Download Whisper large-v3 model
# =============================================================================
# Downloads openai/whisper-large-v3 (~3 GB, safetensors) from Hugging Face and
# stores it in models/whisper-large-v3/ so the Dockerfile can bake it into the
# image. The air-gapped target never downloads anything.
#
# Usage:
#   ./01_download_model.sh
#
# Output:
#   models/whisper-large-v3/
#     - model.safetensors        (weights)
#     - config.json, generation_config.json, preprocessor_config.json
#     - tokenizer.json, vocab.json, merges.txt, ... (tokenizer/processor)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_CACHE_DIR="${SCRIPT_DIR}/models"
MODEL_NAME="openai/whisper-large-v3"
MODEL_DIR="${MODEL_CACHE_DIR}/whisper-large-v3"
LOG_FILE="${SCRIPT_DIR}/build_logs/model_download.log"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

mkdir -p "$(dirname "$LOG_FILE")"

log_info()    { echo -e "${BLUE}[INFO]${NC} $*"    | tee -a "$LOG_FILE"; }
log_success() { echo -e "${GREEN}[\xE2\x9C\x93]${NC} $*" | tee -a "$LOG_FILE"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"    | tee -a "$LOG_FILE"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $*" | tee -a "$LOG_FILE"; }

check_python() {
    log_info "Checking Python installation..."
    if ! command -v python3 &> /dev/null; then
        log_error "Python3 is not installed. Install Python 3.10+ before continuing."
        exit 1
    fi
    log_success "Python $(python3 --version 2>&1 | awk '{print $2}') found"
}

check_disk_space() {
    log_info "Checking disk space..."
    AVAILABLE=$(df "$SCRIPT_DIR" | awk 'NR==2 {print $4}')
    AVAILABLE_GB=$((AVAILABLE / 1024 / 1024))
    log_info "Available disk space: ${AVAILABLE_GB}GB"
    if [ "$AVAILABLE_GB" -lt 7 ]; then
        log_error "Not enough disk space. Need 7+ GB for whisper-large-v3, have ${AVAILABLE_GB}GB"
        exit 1
    fi
    log_success "Disk space OK"
}

install_huggingface_hub() {
    if python3 -c "import huggingface_hub" 2>/dev/null; then
        log_success "huggingface-hub already installed"
        return 0
    fi
    log_info "Installing huggingface-hub..."
    python3 -m pip install --quiet huggingface-hub 2>> "$LOG_FILE" || {
        log_error "Failed to install huggingface-hub"
        exit 1
    }
    log_success "huggingface-hub installed"
}

download_model() {
    log_info "Downloading ${MODEL_NAME} from Hugging Face..."
    log_info "Model size: ~3 GB (safetensors)"
    log_warning "This may take several minutes depending on internet speed"
    echo ""

    mkdir -p "$MODEL_DIR"

    # Skip download if weights already present.
    if [ -f "$MODEL_DIR/model.safetensors" ]; then
        log_success "Model already exists. Skipping download."
        return 0
    fi

    MODEL_DIR="$MODEL_DIR" MODEL_NAME="$MODEL_NAME" python3 << 'PYTHON_EOF'
import os, sys
from pathlib import Path
from huggingface_hub import snapshot_download

MODEL_NAME = os.environ["MODEL_NAME"]
LOCAL_DIR = Path(os.environ["MODEL_DIR"])

print(f"Downloading {MODEL_NAME} -> {LOCAL_DIR}\n")

try:
    snapshot_download(
        repo_id=MODEL_NAME,
        local_dir=str(LOCAL_DIR),
        repo_type="model",
        # Keep the modern safetensors weights; skip duplicate / other-framework
        # formats so we don't double the on-disk size.
        ignore_patterns=["*.bin", "*.h5", "*.msgpack", "*.ot", "*.tflite",
                         "tf_model*", "flax_model*"],
    )
    print(f"\n✓ Model downloaded to {LOCAL_DIR}")

    required = ["model.safetensors", "config.json"]
    missing = [f for f in required if not (LOCAL_DIR / f).exists()]
    for f in required:
        p = LOCAL_DIR / f
        if p.exists():
            print(f"  ✓ {f} ({p.stat().st_size / 1024**3:.2f} GB)")
    if missing:
        print(f"\nERROR: Missing required files: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
except Exception as e:
    print(f"\nERROR: Failed to download model: {e}", file=sys.stderr)
    sys.exit(1)
PYTHON_EOF

    log_success "Download completed"
}

create_manifest() {
    cat > "$MODEL_DIR/manifest.json" << EOF
{
  "model_name": "whisper-large-v3",
  "model_type": "${MODEL_NAME}",
  "downloaded_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "weights": "model.safetensors"
}
EOF
    log_success "Manifest created: $MODEL_DIR/manifest.json"
}

verify_download() {
    log_info "Verifying download..."
    [ -d "$MODEL_DIR" ] || { log_error "Model directory not found"; return 1; }
    for file in model.safetensors config.json; do
        if [ ! -f "$MODEL_DIR/$file" ]; then
            log_error "Missing file: $file"
            return 1
        fi
    done
    log_success "Model verified ($(du -sh "$MODEL_DIR" | awk '{print $1}'))"
}

summary() {
    echo ""
    echo "=============================================================="
    log_success "Model downloaded: whisper-large-v3"
    echo "=============================================================="
    echo "Location: $MODEL_DIR"
    find "$MODEL_DIR" -maxdepth 1 -type f -printf "  - %f (%s bytes)\n" 2>/dev/null || true
    echo "=============================================================="
}

main() {
    echo ""
    echo "=============================================================="
    echo "Download Whisper large-v3 model"
    echo "=============================================================="
    check_python
    check_disk_space
    install_huggingface_hub
    download_model
    create_manifest
    verify_download
    summary
}

main "$@"
