# Muhafiz Support — Service Integration Guide

API reference for the four services in the stack. Aimed at client/integration
developers calling these services.

- **No authentication** on any service (deploy behind your own gateway/firewall).
- All JSON bodies are UTF-8. Ollama and Whisper return JSON; YOLO and the Whisper
  WebSocket exchange JSON + binary frames.
- **`HOST`** = the machine running the stack (use its IP, or `localhost` on the box).
- Ports below are the **defaults from `.env`** and can be changed per deployment —
  always confirm with whoever deployed it.

| Service | Base URL | Protocol | Purpose |
|---|---|---|---|
| Ollama | `http://HOST:13000` | HTTP/REST | LLM chat, generation, embeddings, vision |
| YOLO | `ws://HOST:7860` | WebSocket (binary) | Object detection + tracking on video frames |
| Live Feeds | `http://HOST:8188` | HTTP (HLS + MP3) | Surveillance video (HLS) + audio (MP3) fan-out |
| Whisper | `http://HOST:8000` | HTTP + WebSocket | Speech-to-text (file, URL, live stream, live WS) |

> **Internal vs external hostnames:** services can reach each other inside the
> Docker network by **service name** (e.g. `http://live_feeds:8188`, `http://ollama:11434`).
> External clients must use `http://HOST:<published-port>` instead.

---

## 1. Ollama (LLMs) — `http://HOST:13000`

Local LLM runtime. Three models are baked in:

| Model name (exact tag) | Use for | Notes |
|---|---|---|
| `qwen2.5:72b-instruct-q8_0` | Text chat / generation / reasoning | **text only** |
| `gemma3:27b-it-q8_0` | Vision + text (image understanding) | accepts images |
| `nomic-embed-text:latest` | Embeddings (vector search) | 768-dim |

> Use the exact tag strings above as the `model` field. If a call returns a 404
> "model not found", list what's installed with `GET /api/tags`.

### 1.1 List / inspect models

```bash
curl http://HOST:13000/api/tags     # installed models
curl http://HOST:13000/api/ps       # models currently loaded in VRAM
curl http://HOST:13000/api/version
```

### 1.2 Generate (single prompt) — `POST /api/generate`

Request:
```json
{
  "model": "qwen2.5:72b-instruct-q8_0",
  "prompt": "In one sentence, what is a perimeter intrusion alert?",
  "stream": false
}
```
```bash
curl http://HOST:13000/api/generate -d '{"model":"qwen2.5:72b-instruct-q8_0","prompt":"Hello","stream":false}'
```
Response (key fields):
```json
{ "model": "...", "response": "the generated text", "done": true,
  "total_duration": 123456789, "eval_count": 42 }
```

### 1.3 Chat (multi-turn) — `POST /api/chat`

Request:
```json
{
  "model": "qwen2.5:72b-instruct-q8_0",
  "messages": [
    {"role": "system", "content": "You are a surveillance analysis assistant."},
    {"role": "user",   "content": "List 3 checks when a camera goes offline."}
  ],
  "stream": false
}
```
Response:
```json
{ "model": "...", "message": {"role":"assistant","content":"..."}, "done": true }
```

### 1.4 Vision / image understanding — `POST /api/chat` with `gemma3`

Only `gemma3:27b-it-q8_0` accepts images (Qwen is text-only). `images` is an array
of **base64 strings** (raw base64, **no** `data:` prefix).

```json
{
  "model": "gemma3:27b-it-q8_0",
  "messages": [
    {
      "role": "user",
      "content": "Describe any people or vehicles in this CCTV frame.",
      "images": ["<BASE64_OF_JPEG>"]
    }
  ],
  "stream": false
}
```
```bash
IMG=$(base64 -w0 frame.jpg)            # macOS: base64 -i frame.jpg | tr -d '\n'
curl http://HOST:13000/api/chat -d '{"model":"gemma3:27b-it-q8_0","messages":[{"role":"user","content":"What is in this image?","images":["'"$IMG"'"]}],"stream":false}'
```

### 1.5 Embeddings — `POST /api/embed`

```json
{ "model": "nomic-embed-text:latest", "input": "suspicious package near gate 3" }
```
`input` may also be an array of strings for batch embedding. Response:
```json
{ "model": "nomic-embed-text:latest", "embeddings": [[0.01, -0.02, ...]] }
```

### 1.6 Streaming responses

Set `"stream": true` on `/api/generate` or `/api/chat`. The server returns
**newline-delimited JSON** (one object per chunk); the final object has
`"done": true`. Read the body line-by-line; do not wait for the whole response.

### 1.7 OpenAI-compatible endpoints

The same server also speaks the OpenAI API shape, useful if your client library
already targets OpenAI:

```bash
curl http://HOST:13000/v1/chat/completions -d '{
  "model":"qwen2.5:72b-instruct-q8_0",
  "messages":[{"role":"user","content":"hi"}]
}'
# also: POST /v1/embeddings , POST /v1/completions
```

Full Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md

---

## 2. YOLO (object detection + tracking) — `ws://HOST:7860`

A **WebSocket** service. Each connection is an independent tracking session
(track IDs are isolated per connection). Open one connection per camera/stream.

### 2.1 Protocol

- **Client → server:**
  - **Binary frame** = one JPEG- or PNG-encoded image. One image per message.
  - **Text frame** (optional control JSON): `{"cmd": "reset"}` — clears the
    stability counter for this connection.
- **Server → client:** for **each** image it sends **two** JSON text messages,
  in order: `detection`, then `stable_count`.
- Max inbound message size: 16 MB (a 1080p JPEG is ~300 KB).

### 2.2 Response: `detection`

```jsonc
{
  "type": "detection",
  "frame_id": 1,                      // incrementing per processed frame
  "timestamp": 1718000000.123,        // server epoch seconds
  "inference_ms": 42.1,
  "image_size": {"w": 1920, "h": 1080},
  "detections": [
    {
      "track_id": 3,                  // stable id across frames; null if untracked
      "class_id": 0,                  // COCO class id
      "class_name": "person",
      "confidence": 0.91,
      "bbox": {"x1": 100.0, "y1": 50.0, "x2": 240.0, "y2": 380.0,
               "w": 140.0, "h": 330.0}    // pixels, top-left origin
    }
  ],
  "annotated_image_b64": "<base64 JPEG with boxes drawn>",
  "annotated_image_format": "jpeg"
}
```

### 2.3 Response: `stable_count`

A "stable" track = seen continuously for several frames (filters out flicker/false
positives). Counts only stable IDs.

```jsonc
{
  "type": "stable_count",
  "frame_id": 1,
  "timestamp": 1718000000.123,
  "currently_visible_stable_ids": [3, 7],
  "currently_visible_stable_count": 2,
  "currently_visible_stable_by_class": {"0": 1, "2": 1},  // class_id -> count
  "stable_total_unique": 5,           // unique stable ids ever seen this session
  "raw_active_tracks": 4              // raw tracker count (pre-stability-filter)
}
```

### 2.4 Control + error messages

- Send `{"cmd":"reset"}` → server replies `{"type":"ack","cmd":"reset"}`.
- On a bad frame/command the server replies `{"type":"error","message":"..."}`
  (and, for frame errors, includes `"frame_id"`).

### 2.5 Examples

Single frame (Python):
```python
import asyncio, json, websockets
from pathlib import Path

async def main():
    async with websockets.connect("ws://HOST:7860", max_size=16*1024*1024) as ws:
        await ws.send(Path("frame.jpg").read_bytes())   # binary frame
        detection   = json.loads(await ws.recv())       # type=detection
        stable      = json.loads(await ws.recv())       # type=stable_count
        print(detection["detections"], stable["currently_visible_stable_count"])

asyncio.run(main())
```

Continuous stream from RTSP/webcam (reference client shipped in repo):
```bash
cd yolo_inference
python client_example.py 0 --ws ws://HOST:7860 --fps 10        # webcam index 0
python client_example.py "rtsp://user:pass@cam:554/Streaming/Channels/101" --ws ws://HOST:7860
```

COCO class IDs commonly relevant to CCTV: `0`=person, `1`=bicycle, `2`=car,
`3`=motorcycle, `5`=bus, `7`=truck. (Detected classes can be restricted server-side
via `YOLO_CLASSES` in `.env`.)

---

## 3. Live Feeds (video + audio) — `http://HOST:8188`

Serves a continuous HLS video stream and an MP3 audio stream built from the
on-disk surveillance/audio libraries. All endpoints are GET except the
parameterless `POST /next` controls.

### 3.1 Browser players (HTML)

- `http://HOST:8188/`      — video + audio + links
- `http://HOST:8188/live`  — full-screen video-only player

### 3.2 Video (HLS)

| Method | Path | Returns |
|---|---|---|
| GET | `/hls/stream.m3u8` | HLS playlist (`application/vnd.apple.mpegurl`) |
| GET | `/hls/{segment}.ts` | A media segment (`video/mp2t`), e.g. `/hls/seg_00001.ts` |
| GET | `/surveillance_video/playlist` | `{"count":N,"current_index":i,"videos":["a.mp4",...]}` |
| GET | `/surveillance_video/current` | `{"index":i,"name":"a.mp4"}` |
| POST | `/surveillance_video/next` | advance to next clip → `{"index":i,"name":"b.mp4"}` |

Play in any HLS player by pointing it at `http://HOST:8188/hls/stream.m3u8`
(hls.js, VLC, ffplay, Safari native, etc.).

### 3.3 Audio (MP3 stream)

| Method | Path | Returns |
|---|---|---|
| GET | `/live_audio/stream` | Continuous MP3 byte stream (`audio/mpeg`). Listener joins "live". |
| GET | `/live_audio/playlist` | `{"count":N,"current_index":i,"audios":["a.mp3",...]}` |
| GET | `/live_audio/current` | `{"index":i,"name":"a.mp3"}` |
| POST | `/live_audio/next` | advance to next track |

```bash
curl http://HOST:8188/hls/stream.m3u8
curl -N http://HOST:8188/live_audio/stream -o live.mp3        # Ctrl-C to stop
curl http://HOST:8188/surveillance_video/playlist
curl -X POST http://HOST:8188/live_audio/next
```

---

## 4. Whisper (speech-to-text) — `http://HOST:8000`

Model `whisper-large-v3`, baked in. Four input modes. Common optional fields:
`language` (e.g. `en`, `ur`, `ar`; empty/omitted = auto-detect), `task`
(`transcribe` | `translate`, where `translate` outputs English),
`return_timestamps` (bool).

### 4.1 Health — `GET /health`

```json
{ "status": "ok", "model_path": "/models/whisper-large-v3", "device": "cuda",
  "compute_type": "float16", "cuda_available": true, "gpu": "NVIDIA H200",
  "endpoints": ["/transcribe","/transcribe_url","/transcribe_stream","/ws/transcribe"] }
```
`"status"` is `"loading"` until the model is ready (first ~1–2 min after start).

### 4.2 File upload — `POST /transcribe` (multipart/form-data)

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | file | yes | any ffmpeg-decodable audio/video |
| `language` | text | no | e.g. `en`; omit = auto |
| `task` | text | no | `transcribe` (default) or `translate` |
| `return_timestamps` | text | no | `true`/`false` |

```bash
curl -X POST http://HOST:8000/transcribe \
  -F 'file=@clip.wav' -F 'language=en' -F 'return_timestamps=true'
```
Response:
```json
{ "text": "full transcript", "chunks": [{"timestamp":[0.0,3.2],"text":"..."}],
  "language": "en", "task": "transcribe", "filename": "clip.wav" }
```

### 4.3 Finite URL **or on-disk path** — `POST /transcribe_url` (JSON)

`url` is handed straight to ffmpeg, so it accepts an `http(s)://` URL **or a file
path inside the container**. The surveillance audio corpus is mounted read-only
at **`/data/audio`**, so saved clips can be transcribed by path — no upload:
```json
{ "url": "/data/audio/clip.mp3", "language": "en",
  "task": "transcribe", "return_timestamps": true }
```
```json
{ "url": "http://host/clip.mp3", "language": "en" }
```
Response: same shape as `/transcribe`, with `"url"` instead of `"filename"`.
(Requires `WHISPER_ALLOW_REMOTE_SOURCES=true`, the default — it gates this
endpoint for both URLs and paths.)

### 4.4 Live stream → NDJSON — `POST /transcribe_stream` (JSON)

For a **continuous/live** source (`rtsp://`, `hls .m3u8`, `rtmp://`, `http`).
Streams **newline-delimited JSON**, one line per detected speech segment.
Read the response incrementally.

Request:
```json
{ "url": "http://live_feeds:8188/live_audio/stream",
  "language": "en", "segment_s": 10 }
```
- `segment_s` (optional): max seconds before a forced cut (VAD cuts earlier at
  silence). Falls back to the server's `WHISPER_MAX_SEGMENT_S`.

Output (one JSON object per line):
```json
{"segment":1,"start":0.0,"end":9.4,"text":"..."}
{"segment":2,"start":9.4,"end":17.0,"text":"..."}
{"segment":3,"start":17.0,"end":18.2,"text":"...","final":true}
```
```bash
curl -N -X POST http://HOST:8000/transcribe_stream \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://live_feeds:8188/live_audio/stream"}'
```

### 4.5 Live WebSocket — `ws://HOST:8000/ws/transcribe`

Push raw audio from a client; receive partial transcripts as segments complete.

- **Audio format:** raw **s16le, mono PCM** (16-bit signed little-endian).
  Default sample rate 16000 Hz — set another via the config frame.
- **Text frame (config / control), any time:**
  ```json
  {"language":"en","task":"transcribe","sample_rate":16000}
  ```
  Send `{"action":"end"}` to flush the final buffered segment.
- **Binary frames:** chunks of PCM bytes (e.g. ~0.5 s each).
- **Server → client** (per completed segment):
  ```json
  {"text":"...","start":12.0,"end":14.5,"final":false}
  ```
  `final:true` is sent on flush / disconnect. Errors: `{"error":"..."}`.

Example (Python, streaming a 16 kHz mono WAV):
```python
import asyncio, json, wave, websockets

async def main():
    w = wave.open("clip16k_mono.wav", "rb")          # 16 kHz, mono, s16le
    async with websockets.connect("ws://HOST:8000/ws/transcribe") as ws:
        await ws.send(json.dumps({"language": "en", "sample_rate": w.getframerate()}))
        chunk = w.readframes(8000)                    # ~0.5s
        while chunk:
            await ws.send(chunk)                      # binary PCM
            try:
                while True:
                    print(json.loads(await asyncio.wait_for(ws.recv(), 0.01)))
            except asyncio.TimeoutError:
                pass
            chunk = w.readframes(8000)
        await ws.send(json.dumps({"action": "end"}))  # flush tail
        print(json.loads(await ws.recv()))

asyncio.run(main())
```
Make a test PCM/WAV with ffmpeg: `ffmpeg -i any.mp3 -ar 16000 -ac 1 clip16k_mono.wav`

> **Note on "live":** Whisper is not natively streaming. Modes 4.4/4.5 use
> VAD-based segmentation with carry-over — audio is committed only up to a
> detected silence gap, so segments partition the stream with **no duplication
> and no data loss**. If no pause occurs for `WHISPER_MAX_SEGMENT_S`, a forced
> cut bounds latency.

---

## Error handling (all services)

- **Ollama / Whisper (HTTP):** standard HTTP status codes. Errors return JSON
  `{"detail":"..."}` (Whisper) or `{"error":"..."}` (Ollama). Whisper returns
  `503` while the model is still loading — retry after `/health` is `"ok"`.
- **YOLO / Whisper (WebSocket):** errors arrive as a JSON message
  (`{"type":"error",...}` for YOLO, `{"error":"..."}` for Whisper) rather than
  an HTTP code. Handle them on the receive loop.
- No rate limiting or auth is enforced by the services themselves.

## Quick connectivity check

```bash
curl http://HOST:13000/api/tags        # ollama
curl http://HOST:8188/surveillance_video/playlist   # live_feeds
curl http://HOST:8000/health           # whisper
# yolo: open a ws to ws://HOST:7860 and send one JPEG (see 2.5)
```
