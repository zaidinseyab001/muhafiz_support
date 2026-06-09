# muhafiz_support — single docker stack

Four services orchestrated with docker-compose. Host ports are set in `.env`
(defaults shown):

| Service       | Image / Build               | Host port | What it does                                                                |
| ------------- | --------------------------- | --------- | --------------------------------------------------------------------------- |
| `ollama`      | `./ollama`                  | `13000`   | Local LLM runtime. Bakes `qwen2.5:72b-instruct-q8_0`, `gemma3:27b-it-q8_0`, `nomic-embed-text` into the image (sized for a 141 GB H200). |
| `yolo`        | `./yolo_inference`          | `7860`    | YOLO WebSocket inference server on CUDA 13 + PyTorch cu130 wheels.          |
| `live_feeds`  | `./live_data_feeds`         | `8188`    | FastAPI HLS video + MP3 audio fan-out (ffmpeg-driven).                      |
| `whisper`     | `./whisper`                 | `8000`    | Speech-to-text API (transformers + CUDA). Bakes `whisper-large-v3` in. File upload, finite URL, live stream (NDJSON) and a WebSocket. |

All GPU-consuming services (`ollama`, `yolo`, `whisper`) request **all** visible NVIDIA GPUs.

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

On a connected box the `ollama` image build bakes ~106 GB of model weights into
the image; the named `ollama_models` volume seeds from it on first boot. For the
airgapped H200, use the bundle flow instead (see `AIRGAP_README.md`) — nothing is
pulled at runtime.

## Endpoints (defaults from `.env`)

- **Live feed UI**:    http://HOST:8188/         (or the cleaner `/live`)
- **HLS playlist**:    http://HOST:8188/hls/stream.m3u8
- **Audio stream**:    http://HOST:8188/live_audio/stream
- **YOLO websocket**:  ws://HOST:7860/
- **Ollama API**:      http://HOST:13000/        (e.g. `POST /api/generate`)
- **Whisper API**:     http://HOST:8000/         (`/transcribe`, `/transcribe_url`, `/transcribe_stream`, `ws://HOST:8000/ws/transcribe`)

### Transcribing the live audio feed

The whisper container can pull straight from the `live_feeds` MP3 stream over the
compose network (no host round-trip):

```bash
# finite/continuous pull of the live audio fan-out -> NDJSON, one line per segment
curl -N -X POST http://HOST:8000/transcribe_stream \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://live_feeds:8188/live_audio/stream"}'

# a saved audio file
curl -X POST http://HOST:8000/transcribe -F 'file=@clip.wav'
```

(Requires `WHISPER_ALLOW_REMOTE_SOURCES=true`, the default.)

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
