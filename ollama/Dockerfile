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

# Pull each model in its OWN layer. ollama writes blobs to /root/.ollama on
# the filesystem, so a model that finishes is baked into a cached layer — if a
# later pull fails (bad tag, network, disk), a rebuild resumes at the broken
# model instead of re-downloading the 77 GB Qwen from scratch. Each layer
# starts the daemon, waits, pulls one model, then stops it. `set -e` aborts
# the build on any pull failure (the behavior we want at build time).
RUN set -eux ; ollama serve & SERVE_PID=$! ; \
    until ollama list >/dev/null 2>&1 ; do sleep 1 ; done ; \
    ollama pull qwen2.5:72b-instruct-q8_0 ; \
    kill "${SERVE_PID}" ; wait "${SERVE_PID}" 2>/dev/null || true

RUN set -eux ; ollama serve & SERVE_PID=$! ; \
    until ollama list >/dev/null 2>&1 ; do sleep 1 ; done ; \
    ollama pull gemma3:27b-it-q8_0 ; \
    kill "${SERVE_PID}" ; wait "${SERVE_PID}" 2>/dev/null || true

RUN set -eux ; ollama serve & SERVE_PID=$! ; \
    until ollama list >/dev/null 2>&1 ; do sleep 1 ; done ; \
    ollama pull nomic-embed-text:latest ; \
    ollama list ; \
    kill "${SERVE_PID}" ; wait "${SERVE_PID}" 2>/dev/null || true
