# Airgap deploy — muhafiz_support

This bundle ships everything needed to run the stack on a host with **no internet
access**. All three Docker images are pre-built and self-contained:

- `muhafiz/ollama:latest` — `qwen2.5:72b-instruct-q8_0`, `gemma3:27b-it-q8_0`,
  `nomic-embed-text:latest` baked into `/root/.ollama` (~106 GB layer). Sized
  for the 141 GB H200 deploy target.
- `muhafiz/yolo_inference:latest` — CUDA 13 + PyTorch cu130 + `yolo11x.pt` baked in.
- `muhafiz/live_data_feeds:latest` — FastAPI HLS + audio server.

No volume restore, no model pull, no network calls at runtime.

## Prereqs on the airgapped host

- Docker Engine 19.03+ and Docker Compose v2
- NVIDIA driver + `nvidia-container-toolkit` (the `ollama` and `yolo` services
  request `count: all` GPUs)

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

2. **Edit `.env`** — set `PORT`, `OLLAMA_PORT`, `WS_PORT`, `YOLO_DEVICE`, etc. for
   this box. Leave `COMPOSE_PROJECT_NAME=muhafiz_support` as-is.

3. **(Optional) edit `ollama/entrypoint.sh`** — change the `MODELS=...` line if
   you want to swap which baked model gets loaded. The script no longer
   hard-fails on a missing model in airgap mode: it logs a warning and keeps
   the daemon alive. Anything added here must already be in the image — the
   `ollama pull` call has no network to reach.

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
└── _bundle/
    └── images/
        ├── ollama.tar            <- ~106 GB
        ├── yolo_inference.tar    <- ~8 GB
        └── live_data_feeds.tar   <- ~1 GB
```

## Endpoints (defaults from `.env`)

- Live feed UI:    `http://HOST:${PORT}/`        (default 8188)
- YOLO websocket:  `ws://HOST:${WS_PORT}/`       (default 7860)
- Ollama API:      `http://HOST:${OLLAMA_PORT}/` (default 13000)

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

Output is `..\muhafiz_support.tar` next to the project folder. Expect ~115 GB.
The slow step is the ollama image build — it runs `ollama serve` during build
and pulls the three models (~106 GB) into a single layer. Budget a few hours
on a decent connection, and make sure the build box has enough free disk —
see the disk note below.

> **Disk on the build box:** the bundle is staged three times — the docker
> image (~106 GB), the `docker save` tar in `_bundle/images/` (~106 GB), and
> the final wrapping `muhafiz_support.tar` (~115 GB). Keep **~350 GB free**
> on the connected machine, or build the ollama image tar separately and skip
> the outer wrap.
