# CUDA 13 runtime image with cuDNN — matches the cu130 PyTorch wheels.
FROM nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps:
#   python3 + venv  — runtime
#   ffmpeg          — used by some pipelines, harmless to include
#   libgl1, libglib — required by opencv-python (non-headless build)
#   ca-certificates, curl — for pip downloads / healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Use an isolated venv so we don't hit PEP 668 "externally-managed" errors on Ubuntu 24.04.
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install the CUDA 13 PyTorch wheels FIRST so the rest of requirements.txt
# resolves against them instead of pulling the CPU build from PyPI.
RUN pip install --upgrade pip && \
    pip install \
        --extra-index-url https://download.pytorch.org/whl/cu130 \
        "torch>=2.5.0" \
        "torchvision>=0.20.0"

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "server.py"]
