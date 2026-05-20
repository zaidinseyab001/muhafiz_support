# muhafiz_support — single docker stack

Three services orchestrated with docker-compose:

| Service       | Image / Build               | Host port | What it does                                                                |
| ------------- | --------------------------- | --------- | --------------------------------------------------------------------------- |
| `ollama`      | `ollama/ollama:latest`      | `11434`   | Local LLM runtime. Pulls `qwen2.5:14b`, `gemma3:27b`, `nomic-embed-text` on first start. |
| `yolo`        | `./yolo_inference`          | `5000`    | YOLO WebSocket inference server on CUDA 13 + PyTorch cu130 wheels.          |
| `live_feeds`  | `./live_data_feeds`         | `8181`    | FastAPI HLS video + MP3 audio fan-out (ffmpeg-driven).                      |

All GPU-consuming services (`ollama`, `yolo`) request **all** visible NVIDIA GPUs.

## Prereqs

- Docker Engine 19.03+ and Docker Compose v2
- NVIDIA driver on the host
- `nvidia-container-toolkit` installed and registered with the docker daemon

Verify the GPU plumbing before bringing the stack up:

```bash
docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi
```

## Bring up

```bash
docker compose up -d --build
```

First boot of `ollama` will pull ~26 GB of model weights into the `ollama_models` named volume — subsequent restarts are instant.

## Endpoints

- **Live feed UI**:    http://HOST:8181/         (or the cleaner `/live`)
- **HLS playlist**:    http://HOST:8181/hls/stream.m3u8
- **Audio stream**:    http://HOST:8181/live_audio/stream
- **YOLO websocket**:  ws://HOST:5000/
- **Ollama API**:      http://HOST:11434/        (e.g. `POST /api/generate`)

## Reusing host-pulled ollama models

If you've already pulled the models on the host (`~/.ollama` or `/usr/share/ollama/.ollama`) and want to skip the re-download, swap the volume in `docker-compose.yml`:

```yaml
    volumes:
      - /usr/share/ollama/.ollama:/root/.ollama   # bind-mount instead of named volume
      - ./ollama/entrypoint.sh:/entrypoint.sh:ro
```

The `entrypoint.sh` script does an `ollama pull` for each model on boot — it's a no-op when the blobs are already present.

## Tweaking GPU assignment

To pin a single GPU instead of `count: all`, replace the relevant service's `deploy.resources.reservations.devices` entry with:

```yaml
            - driver: nvidia
              device_ids: ["0"]
              capabilities: [gpu]
```

## Common operations

```bash
docker compose logs -f yolo               # tail yolo logs
docker compose logs -f ollama             # watch model pulls during first boot
docker compose exec ollama ollama list    # inspect cached models
docker compose down                       # stop everything (keeps volumes)
docker compose down -v                    # stop + nuke model cache + normalized clips
```
