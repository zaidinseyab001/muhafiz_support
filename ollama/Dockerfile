# Bakes the LLM model blobs into /root/.ollama so the image is fully
# self-contained — no network needed at runtime, no volume prep on the
# airgapped host.
#
# Final image size is roughly 106 GB on top of the upstream ollama layers.
# That's fine for the airgap case where shipping a fat image once beats
# trying to hydrate a volume on a disconnected box.
#
# Model lineup is sized for the DEPLOY target — a 141 GB H200 — not the
# connected build box (the builder's GPU/VRAM is irrelevant).
#   qwen2.5:72b-instruct-q8_0  ~77 GB  primary text model. Q8 is near-
#                                      lossless; fp16 (145 GB) won't fit a
#                                      single 141 GB H200, so Q8 is the ceiling.
#   gemma3:27b-it-q8_0         ~29 GB  multimodal, Q4->Q8 upgrade.
#   nomic-embed-text           ~0.3 GB embeddings (F16).
# All three co-reside in 141 GB with room for KV cache + the FP32 YOLO
# service, so OLLAMA_KEEP_ALIVE=always (see .env) keeps them all hot.
#
# IMPORTANT — models are NOT pulled during this build. Pulling 100+ GB inside a
# `docker build` RUN is not resumable: any network drop discards the layer and
# restarts at 0%, which never completes on a slow/flaky link. Instead,
# scripts/build_airgap.sh pulls the models on the HOST into ./ollama/models
# (resumable — Ollama continues partial blobs across retries), and we just COPY
# the finished blobs in here. Zero network during this build.
FROM ollama/ollama:latest

# ./ollama/models is a fully-populated OLLAMA home (the /root/.ollama tree:
# models/blobs + models/manifests) produced by the host-side pull step.
COPY models /root/.ollama
