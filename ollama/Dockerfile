# Bakes the LLM model blobs into /root/.ollama so the image is fully
# self-contained — no network needed at runtime, no volume prep on the
# airgapped host.
#
# Final image size is roughly 26 GB on top of the upstream ollama layers.
# That's fine for the airgap case where shipping a fat image once beats
# trying to hydrate a volume on a disconnected box.
FROM ollama/ollama:latest

# Run the daemon in the background just long enough to `ollama pull` each
# model into /root/.ollama, then shut it down so the layer is clean. The
# `set -e` semantics mean any pull failure kills the build, which is the
# behavior we want at build time (unlike runtime, where we tolerate it).
RUN set -eux ; \
    ollama serve & SERVE_PID=$! ; \
    echo "[build] waiting for ollama daemon..." ; \
    until ollama list >/dev/null 2>&1 ; do sleep 1 ; done ; \
    echo "[build] daemon up — pulling models" ; \
    ollama pull qwen2.5:14b ; \
    ollama pull gemma3:27b ; \
    ollama pull nomic-embed-text:latest ; \
    echo "[build] models pulled — stopping daemon" ; \
    kill "${SERVE_PID}" ; \
    wait "${SERVE_PID}" 2>/dev/null || true ; \
    ollama --version
