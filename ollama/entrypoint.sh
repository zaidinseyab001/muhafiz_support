#!/bin/sh
# Start ollama serve in the background, wait for the API to come up,
# pull the models we need (no-op if already cached in the mounted
# volume — which is the airgap path), then hand the foreground back
# to ollama serve.
#
# Airgap behavior: a failed `ollama pull` is logged and skipped rather
# than fatal, so the daemon stays up even if one model is missing or
# the box has no outbound network. The expectation in airgap is that
# the ollama_models volume was pre-populated by the bundle.
set -u

ollama serve &
SERVE_PID=$!

# Wait for the daemon to accept requests. `ollama list` exits non-zero
# until the server is up — poll it instead of relying on curl, which
# isn't in the upstream image.
echo "[ollama-init] waiting for ollama daemon..."
until ollama list >/dev/null 2>&1; do
    sleep 1
done
echo "[ollama-init] daemon is ready."

# Edit this list to add/remove models. In an airgapped deploy these
# must already be present in the mounted ollama_models volume; the
# pull below becomes a no-op when the blobs are cached locally.
MODELS="qwen2.5:72b-instruct-q8_0 gemma3:27b-it-q8_0 nomic-embed-text:latest"

for model in $MODELS; do
    if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "${model}"; then
        echo "[ollama-init] model already cached: ${model}"
        continue
    fi
    echo "[ollama-init] pulling model: ${model}"
    if ! ollama pull "${model}"; then
        echo "[ollama-init] WARNING: pull failed for ${model} — continuing (airgap?)"
    fi
done

echo "[ollama-init] init done — attaching to serve (pid ${SERVE_PID})."
wait "${SERVE_PID}"
