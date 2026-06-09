# Whisper — speech-to-text service (part of the muhafiz_support stack)

Whisper **large-v3** as a self-contained GPU Docker image (HuggingFace
Transformers + PyTorch CUDA). The model is **baked into the image**, so the
air-gapped target never downloads anything.

> This is now a service in the **root** `docker-compose.yml`. It is built,
> bundled and deployed by the root flow — **not** standalone. All runtime config
> comes from the **root `.env`** (`WHISPER_*` keys), injected via `env_file`.

## Layout

```
app/                        FastAPI server (app/server.py, app/config.py)
Dockerfile                  CUDA base, installs deps, bakes in model + app
requirements.txt            torch+cu121, transformers, fastapi, webrtcvad, ...
01_download_model.sh        downloads whisper-large-v3 into models/ (build box)
install_nvidia_toolkit.sh   optional: enable GPU inside Docker on a host
models/whisper-large-v3/    weights (git-ignored; created by 01_download_model.sh)
```

## Build / deploy

Use the root scripts — do not build this folder on its own:

```bash
# connected build box (downloads the model, builds + smoke-tests, makes the tar)
./scripts/build_airgap.sh

# airgapped H200 (after transferring muhafiz_support.tar)
tar -xf muhafiz_support.tar && cd muhafiz_support && ./deploy.sh
```

Config lives in the root `.env` (`WHISPER_MODEL_PATH`, `WHISPER_DEVICE`,
`WHISPER_PORT`, `WHISPER_BATCH_SIZE`, `WHISPER_VAD_*`, ...).

## Use (default port 8000)

Four input modes — file upload, finite URL, live stream, and a WebSocket:

```bash
# Health
curl http://HOST:8000/health

# 1) File upload
curl -X POST http://HOST:8000/transcribe -F 'file=@audio.wav'
#    optional: -F language=en  -F task=transcribe  -F return_timestamps=true

# 2) Finite URL (http/https/...): downloads + transcribes once
curl -X POST http://HOST:8000/transcribe_url \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://host/clip.mp3","language":"en"}'

# 3) Live stream (rtsp/hls/rtmp/http): streams NDJSON, one line per segment.
#    Point it at the stack's own live audio fan-out over the compose network:
curl -N -X POST http://HOST:8000/transcribe_stream \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://live_feeds:8188/live_audio/stream"}'
#    -> {"segment":1,"start":0.0,"end":..,"text":"..."}  (repeating)

# 4) WebSocket (client pushes raw PCM s16le mono @16kHz):
#    ws://HOST:8000/ws/transcribe
#    - JSON text frame to configure: {"language":"en","sample_rate":16000}
#    - binary frames of s16le PCM audio
#    - receive {"text":"...","final":false} per segment; {"action":"end"} flushes
```

**Note on "live":** Whisper is not a streaming model. Modes 3 & 4 use
**VAD-based segmentation with carry-over**: audio is buffered and only committed
up to a detected silence gap, so segments *partition* the stream — **no
duplication and no data loss**, words cut at pauses rather than mid-word. If no
pause occurs for `WHISPER_MAX_SEGMENT_S`, a forced cut bounds latency. Tune via
`WHISPER_VAD_*` / `WHISPER_MIN_SILENCE_MS` / `WHISPER_MAX_SEGMENT_S` in the root
`.env`; set `WHISPER_VAD_ENABLED=false` for plain fixed windows. Server-side
fetching can be disabled with `WHISPER_ALLOW_REMOTE_SOURCES=false`.
