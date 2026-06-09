# =============================================================================
# Muhafiz Whisper API - air-gapped GPU image
# Backend: HuggingFace Transformers + PyTorch (CUDA 12.1 wheels on CUDA 12.4 rt)
# =============================================================================
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Force fully offline model loading at runtime (no HF network calls)
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    # ---- App defaults (override at runtime via -e / .env) ----
    WHISPER_MODEL_PATH=/models/whisper-large-v3 \
    WHISPER_DEVICE=cuda \
    WHISPER_COMPUTE_TYPE=float16 \
    WHISPER_CHUNK_LENGTH_S=30 \
    WHISPER_BATCH_SIZE=8 \
    WHISPER_DEFAULT_LANGUAGE="" \
    WHISPER_DEFAULT_TASK=transcribe \
    WHISPER_RETURN_TIMESTAMPS=true \
    WHISPER_HOST=0.0.0.0 \
    WHISPER_PORT=8000 \
    PATH="/opt/whisper/bin:${PATH}"

# System runtime deps (ffmpeg is required for audio decoding in the pipeline)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        ffmpeg \
        libsndfile1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build an isolated virtualenv and install Python deps INSIDE the image.
# (Internet is needed here on the BUILD machine only.)
RUN python3 -m venv /opt/whisper && \
    /opt/whisper/bin/pip install --upgrade pip setuptools wheel
COPY requirements.txt /tmp/requirements.txt
RUN /opt/whisper/bin/pip install -r /tmp/requirements.txt

# Bake in the model weights and application code.
# (models/whisper-large-v3 is produced by 01_download_model.sh on the build box.)
COPY models/whisper-large-v3 /models/whisper-large-v3
COPY app /app/app

WORKDIR /app

# Sanity-check imports at build time so a broken image fails fast.
RUN /opt/whisper/bin/python -c "import torch, transformers, fastapi; print('deps OK', torch.__version__)"

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=5 \
    CMD curl -fsS "http://localhost:${WHISPER_PORT}/health" || exit 1

EXPOSE 8000

# Server reads host/port/model/device entirely from env (app/config.py).
ENTRYPOINT ["/opt/whisper/bin/python", "-m", "app.server"]
