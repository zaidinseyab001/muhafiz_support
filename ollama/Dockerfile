# Bakes the LLM model blobs into /root/.ollama so the image is fully
# self-contained — no network needed at runtime, no volume prep on the
# airgapped host.
#
# Final image size is roughly 106 GB on top of the upstream ollama layers.
# That's fine for the airgap case where shipping a fat image once beats
# trying to hydrate a volume on a disconnected box.
#
# Model lineup is sized for the DEPLOY target — a 141 GB H200 — not the
# connected build box. `ollama pull` only writes blobs to disk, so the
# builder's GPU/VRAM is irrelevant; it just needs disk + network.
#   qwen2.5:72b-instruct-q8_0  ~77 GB  primary text model. Q8 is near-
#                                      lossless; fp16 (145 GB) won't fit a
#                                      single 141 GB H200, so Q8 is the ceiling.
#   gemma3:27b-it-q8_0         ~29 GB  multimodal, Q4->Q8 upgrade.
#   nomic-embed-text           ~0.3 GB embeddings (F16).
# All three co-reside in 141 GB with room for KV cache + the FP32 YOLO
# service, so OLLAMA_KEEP_ALIVE=always (see .env) keeps them all hot.
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
    ollama pull qwen2.5:72b-instruct-q8_0 ; \
    ollama pull gemma3:27b-it-q8_0 ; \
    ollama pull nomic-embed-text:latest ; \
    echo "[build] models pulled — stopping daemon" ; \
    kill "${SERVE_PID}" ; \
    wait "${SERVE_PID}" 2>/dev/null || true ; \
    ollama --version
