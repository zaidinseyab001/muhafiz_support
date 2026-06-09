#!/usr/bin/env bash
# =============================================================================
# install_nvidia_toolkit.sh
# Installs the NVIDIA Container Toolkit and registers it with Docker, so that
# `--gpus all` / the compose GPU reservation works.
# Run this on ANY machine (build or airgap target) that has an NVIDIA GPU.
# Requires: internet access + apt (Ubuntu/Debian) + a working NVIDIA driver.
# For a TRUE air-gapped target, see the note at the bottom of this file.
# =============================================================================
set -euo pipefail

green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
red() { printf '\033[0;31m%s\033[0m\n' "$*"; }

# 0. Driver present?
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
    red "No working NVIDIA driver found (nvidia-smi failed)."
    red "Install the GPU driver first, then re-run this script."
    exit 1
fi
green "==> NVIDIA driver OK:"; nvidia-smi -L

# 1. Already installed?
if docker info 2>/dev/null | grep -qi 'Runtimes:.*nvidia' || \
   command -v nvidia-ctk >/dev/null 2>&1 && docker info 2>/dev/null | grep -qi nvidia; then
    green "==> NVIDIA Container Toolkit already present; (re)configuring Docker."
fi

# 2. Add the NVIDIA repo (idempotent) and install
green "==> Adding NVIDIA Container Toolkit apt repo"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null

green "==> Installing nvidia-container-toolkit"
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# 3. Register runtime with Docker and restart it
green "==> Configuring Docker runtime"
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 4. Verify
green "==> Verifying GPU access inside a container"
if sudo docker run --rm --gpus all nvidia/cuda:12.4.1-runtime-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
    green "==================================================================="
    green " SUCCESS: Docker can now use the GPU. Re-run ./run.sh"
    green "==================================================================="
else
    red "GPU still not accessible from containers. Check:"
    red "  - docker info | grep -i runtime   (should list 'nvidia')"
    red "  - the CUDA base image is available locally (it is, inside your app image)"
fi

# -----------------------------------------------------------------------------
# AIR-GAPPED TARGET (no internet):
#   You cannot apt-install here. Instead, on a connected machine of the SAME
#   Ubuntu version, download the .deb packages:
#       sudo apt-get install --download-only nvidia-container-toolkit
#       # debs land in /var/cache/apt/archives/*.deb  -> copy them to the target
#   Then on the target:
#       sudo dpkg -i nvidia-container-toolkit*.deb libnvidia-container*.deb
#       sudo nvidia-ctk runtime configure --runtime=docker
#       sudo systemctl restart docker
#   (The NVIDIA GPU *driver* must also already be installed on the target host.)
# -----------------------------------------------------------------------------
