#!/bin/sh
# Start ollama serve in the background, wait for the API to come up,
# pull the three models we need (no-op if already cached in the
# mounted volume), then hand the foreground back to ollama serve.
set -e

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

for model in qwen2.5:14b gemma3:27b nomic-embed-text:latest; do
    echo "[ollama-init] ensuring model: ${model}"
    ollama pull "${model}"
done

echo "[ollama-init] all models present — attaching to serve (pid ${SERVE_PID})."
wait "${SERVE_PID}"
