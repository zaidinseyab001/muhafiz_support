# Airgap deploy — muhafiz_support

This bundle ships everything needed to run the stack on a host with **no internet
access**. All four Docker images are pre-built and self-contained:

- `muhafiz/ollama:latest` — `qwen2.5:72b-instruct-q8_0`, `gemma3:27b-it-q8_0`,
  `nomic-embed-text:latest` baked into `/root/.ollama` (~106 GB layer). Sized
  for the 141 GB H200 deploy target.
- `muhafiz/yolo_inference:latest` — CUDA 13 + PyTorch cu130 + `yolo11x.pt` baked in.
- `muhafiz/live_data_feeds:latest` — FastAPI HLS + audio server.
- `muhafiz/whisper:latest` — transformers + CUDA + `whisper-large-v3` baked in.
  Speech-to-text: file upload, finite URL, live stream (NDJSON), WebSocket.

No volume restore, no model pull, no network calls at runtime.

## Prereqs on the airgapped host

- Docker Engine 19.03+ and Docker Compose v2
- NVIDIA driver + `nvidia-container-toolkit` (the `ollama`, `yolo` and `whisper`
  services request `count: all` GPUs)

Verify GPUs are wired through before deploying:

```bash
docker run --rm --gpus all <any-cuda-image-already-on-host> nvidia-smi
```

## Deploy in 4 steps

1. **Extract the tar:**
   ```bash
   tar -xf muhafiz_support.tar
   cd muhafiz_support
   ```

2. **Edit `.env`** — set `PORT`, `OLLAMA_PORT`, `WS_PORT`, `WHISPER_PORT`,
   `YOLO_DEVICE`, `WHISPER_DEVICE`, etc. for this box. Leave
   `COMPOSE_PROJECT_NAME=muhafiz_support` as-is.

3. **(Optional) edit `ollama/entrypoint.sh`** — change the `MODELS=...` line if
   you want to swap which baked model gets loaded. The script no longer
   hard-fails on a missing model in airgap mode: it logs a warning and keeps
   the daemon alive. Anything added here must already be in the image — the
   `ollama pull` call has no network to reach. (Same rule for whisper: the
   model is baked at build time; changing `WHISPER_MODEL_PATH` to a model that
   isn't in the image requires a rebuild on a connected box.)

4. **Run the deploy:**
   ```bash
   ./deploy.sh
   ```

   Loads images from `_bundle/images/` and runs `docker compose up -d`.
   Idempotent — safe to re-run after editing `.env`.

## What's inside the tar

```
muhafiz_support/
├── docker-compose.yml
├── .env                          <- edit before deploy
├── deploy.sh                     <- one-shot loader + compose up
├── ollama/
│   ├── Dockerfile                <- bakes LLM models in (builder uses this)
│   └── entrypoint.sh             <- edit to change model list
├── yolo_inference/               <- source (kept for diffability / rebuilds)
├── live_data_feeds/              <- source
├── whisper/                      <- source (model NOT here; baked in image)
└── _bundle/
    └── images/
        ├── ollama.tar            <- ~106 GB
        ├── yolo_inference.tar    <- ~8 GB
        ├── live_data_feeds.tar   <- ~1 GB
        └── whisper.tar           <- ~8 GB
```

## Endpoints (defaults from `.env`)

- Live feed UI:    `http://HOST:${PORT}/`            (default 8188)
- YOLO websocket:  `ws://HOST:${WS_PORT}/`           (default 7860)
- Ollama API:      `http://HOST:${OLLAMA_PORT}/`     (default 13000)
- Whisper API:     `http://HOST:${WHISPER_PORT}/`    (default 8000; `/transcribe`,
  `/transcribe_url`, `/transcribe_stream`, `ws://HOST:${WHISPER_PORT}/ws/transcribe`)

## Common ops

```bash
docker compose -p muhafiz_support logs -f ollama
docker compose -p muhafiz_support logs -f yolo
docker compose -p muhafiz_support down              # stop, keep volume
docker compose -p muhafiz_support down -v           # stop + delete volume
                                                    # (next `up` re-seeds from image)
```

## Re-deploying / updating

Rebuild on a connected machine, ship the new tar, extract over the existing
folder, and re-run `./deploy.sh`. The image load overwrites previous tags.
Containers will recreate on the next `compose up`.

## Building the bundle (on a connected machine)

```bash
# Linux / WSL
./scripts/build_airgap.sh

# Windows / Docker Desktop
.\scripts\build_airgap.ps1
```

The build script (in order): **downloads** the whisper-large-v3 weights into
`whisper/models/`, **builds** all four images, **smoke-tests** the runnable
services (whisper health, yolo + live_feeds import checks — `ollama` is skipped
because its models can't load on a build box), **saves** the four images, and
**tars** everything into `..\muhafiz_support.tar`. Expect ~123 GB. The slow step
is the ollama image build (pulls ~106 GB of LLM blobs). Budget a few hours.

Toggles: `SKIP_SMOKE=1` (no GPU toolkit on the build box) and `SKIP_MODEL=1`
(whisper weights already downloaded).

> **Disk on the build box:** the bundle is staged three times — the docker
> images (~115 GB), the `docker save` tars in `_bundle/images/` (~115 GB), and
> the final wrapping `muhafiz_support.tar` (~123 GB). Keep **~370 GB free**
> on the connected machine, or save the image tars to a big disk and skip the
> outer wrap.
