"""Whisper transcription API (transformers + torch CUDA).

Loads a local HuggingFace Whisper model (no internet) and serves automatic
speech recognition over several input modes:

GET  /health             -> liveness/readiness + device/model info
POST /transcribe         -> multipart file upload
POST /transcribe_url     -> JSON {url} for a finite file/URL (http/https/...)
POST /transcribe_stream  -> JSON {url} for a LIVE stream (rtsp/hls/rtmp/http);
                            streams NDJSON, one line per segment
WS   /ws/transcribe      -> client pushes raw PCM (s16le mono); server returns
                            partial transcripts per segment

All configuration comes from environment variables (see app/config.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

import numpy as np
import torch
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

from . import audio
from .config import settings
from .streaming import StreamingTranscriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("whisper-api")

_DTYPES = {
    "float16": torch.float16, "fp16": torch.float16,
    "bfloat16": torch.bfloat16, "bf16": torch.bfloat16,
    "float32": torch.float32, "fp32": torch.float32,
}

# Minimum audio length (samples) worth sending to the model (~0.1s).
_MIN_SAMPLES = 1600

app = FastAPI(title="Muhafiz Whisper API", version="1.1.0")

_pipe = None
_resolved_device = "cpu"
_resolved_dtype = "float32"
# Serialize GPU inference across concurrent requests / streams / sockets.
_infer_lock = threading.Lock()


def _resolve_device() -> str:
    want = settings.device.lower()
    if want.startswith("cuda"):
        if not torch.cuda.is_available():
            logger.warning(
                "WHISPER_DEVICE=%s requested but CUDA is unavailable; using CPU. "
                "(Did you run with --gpus all + NVIDIA Container Toolkit?)",
                settings.device,
            )
            return "cpu"
        return settings.device
    return "cpu"


@app.on_event("startup")
def load_model() -> None:
    global _pipe, _resolved_device, _resolved_dtype

    _resolved_device = _resolve_device()
    on_gpu = _resolved_device.startswith("cuda")

    dtype = _DTYPES.get(settings.compute_type.lower(), torch.float16)
    if not on_gpu:
        dtype = torch.float32
    _resolved_dtype = str(dtype).replace("torch.", "")

    logger.info("Loading model '%s' on %s (dtype=%s)",
                settings.model_path, _resolved_device, _resolved_dtype)
    t0 = time.time()

    model = AutoModelForSpeechSeq2Seq.from_pretrained(
        settings.model_path, torch_dtype=dtype,
        low_cpu_mem_usage=True, local_files_only=True,
    )
    model.to(_resolved_device)
    processor = AutoProcessor.from_pretrained(settings.model_path, local_files_only=True)

    _pipe = pipeline(
        task="automatic-speech-recognition",
        model=model,
        tokenizer=processor.tokenizer,
        feature_extractor=processor.feature_extractor,
        torch_dtype=dtype,
        device=_resolved_device,
        chunk_length_s=settings.chunk_length_s,
        batch_size=settings.batch_size,
    )
    logger.info("Model ready in %.1fs", time.time() - t0)


# ---------------------------------------------------------------------------
# Core inference (blocking; always called under the lock)
# ---------------------------------------------------------------------------
def _transcribe_array(
    audio_array: np.ndarray,
    language: Optional[str],
    task: str,
    return_timestamps: bool,
) -> dict:
    if _pipe is None:
        raise RuntimeError("Model still loading")
    if audio_array is None or audio_array.size < _MIN_SAMPLES:
        return {"text": "", "chunks": []}

    generate_kwargs = {"task": task}
    if language:
        generate_kwargs["language"] = language

    with _infer_lock:
        result = _pipe(
            {"raw": audio_array, "sampling_rate": audio.SAMPLE_RATE},
            return_timestamps=return_timestamps,
            generate_kwargs=generate_kwargs,
        )
    return {"text": result.get("text", "").strip(), "chunks": result.get("chunks", [])}


def _defaults(language: Optional[str], task: Optional[str], ts: Optional[bool]):
    lang = language if language is not None else (settings.default_language or None)
    if lang == "":
        lang = None
    return (
        lang,
        task or settings.default_task,
        settings.return_timestamps if ts is None else ts,
    )


def _check_remote_allowed():
    if not settings.allow_remote_sources:
        raise HTTPException(
            status_code=403,
            detail="Remote sources disabled (WHISPER_ALLOW_REMOTE_SOURCES=false)",
        )


def _make_segmenter(lang: Optional[str], task: str, max_segment_s: float) -> StreamingTranscriber:
    """A StreamingTranscriber wired to this model (VAD silence-cut + carry-over)."""
    def _tfn(arr: np.ndarray) -> str:
        return _transcribe_array(arr, lang, task, False)["text"]

    return StreamingTranscriber(
        _tfn,
        sample_rate=audio.SAMPLE_RATE,
        aggressiveness=settings.vad_aggressiveness,
        vad_enabled=settings.vad_enabled,
        min_silence_ms=settings.min_silence_ms,
        min_segment_s=settings.min_segment_s,
        max_segment_s=max_segment_s,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
@app.get("/")
def health() -> dict:
    return {
        "status": "ok" if _pipe is not None else "loading",
        "model_path": settings.model_path,
        "device": _resolved_device,
        "compute_type": _resolved_dtype,
        "cuda_available": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "endpoints": ["/transcribe", "/transcribe_url", "/transcribe_stream", "/ws/transcribe"],
    }


# ---------------------------------------------------------------------------
# 1) File upload
# ---------------------------------------------------------------------------
@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: Optional[str] = Form(None),
    task: Optional[str] = Form(None),
    return_timestamps: Optional[bool] = Form(None),
) -> dict:
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model still loading")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")

    lang, tsk, ts = _defaults(language, task, return_timestamps)
    loop = asyncio.get_event_loop()
    try:
        arr = await loop.run_in_executor(None, audio.decode_bytes, raw)
        result = await loop.run_in_executor(None, _transcribe_array, arr, lang, tsk, ts)
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcribe failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
    return {"text": result["text"], "chunks": result["chunks"],
            "language": lang, "task": tsk, "filename": file.filename}


# ---------------------------------------------------------------------------
# 2) Finite URL
# ---------------------------------------------------------------------------
class UrlRequest(BaseModel):
    url: str
    language: Optional[str] = None
    task: Optional[str] = None
    return_timestamps: Optional[bool] = None


@app.post("/transcribe_url")
async def transcribe_url(req: UrlRequest) -> dict:
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model still loading")
    _check_remote_allowed()

    lang, tsk, ts = _defaults(req.language, req.task, req.return_timestamps)
    loop = asyncio.get_event_loop()
    try:
        arr = await loop.run_in_executor(
            None, audio.decode_source, req.url, settings.url_fetch_timeout
        )
        result = await loop.run_in_executor(None, _transcribe_array, arr, lang, tsk, ts)
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcribe_url failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
    return {"text": result["text"], "chunks": result["chunks"],
            "language": lang, "task": tsk, "url": req.url}


# ---------------------------------------------------------------------------
# 3) Live stream -> NDJSON (one line per segment)
# ---------------------------------------------------------------------------
class StreamRequest(BaseModel):
    url: str
    # Max seconds before a forced cut (VAD cuts earlier at silence). Falls back
    # to WHISPER_MAX_SEGMENT_S. (Field name kept for backwards compatibility.)
    segment_s: Optional[float] = None
    language: Optional[str] = None
    task: Optional[str] = None


@app.post("/transcribe_stream")
async def transcribe_stream(req: StreamRequest):
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Model still loading")
    _check_remote_allowed()

    lang, tsk, _ = _defaults(req.language, req.task, False)
    max_segment_s = req.segment_s or settings.max_segment_s

    def generate():
        seg = _make_segmenter(lang, tsk, max_segment_s)
        idx = 0
        try:
            # Pull small frames; the segmenter commits at silence (no dup/loss).
            for frame in audio.iter_stream_segments(req.url, settings.stream_read_s):
                for s in seg.add_audio(frame):
                    idx += 1
                    yield json.dumps(
                        {"segment": idx, "start": s.start, "end": s.end, "text": s.text}
                    ) + "\n"
            for s in seg.flush():
                idx += 1
                yield json.dumps(
                    {"segment": idx, "start": s.start, "end": s.end, "text": s.text, "final": True}
                ) + "\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("transcribe_stream failed")
            yield json.dumps({"error": str(exc)}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# 4) WebSocket: client pushes raw PCM (s16le mono), gets partial transcripts
# ---------------------------------------------------------------------------
@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    if _pipe is None:
        await ws.send_json({"error": "Model still loading"})
        await ws.close()
        return

    # Mutable state so a config frame can change language/task mid-stream
    # without rebuilding the segmenter (which holds the carry-over buffer).
    state = {"language": settings.default_language or None, "task": settings.default_task}
    sample_rate = audio.SAMPLE_RATE
    loop = asyncio.get_event_loop()

    def _tfn(arr: np.ndarray) -> str:
        return _transcribe_array(arr, state["language"], state["task"], False)["text"]

    seg = StreamingTranscriber(
        _tfn,
        sample_rate=audio.SAMPLE_RATE,
        aggressiveness=settings.vad_aggressiveness,
        vad_enabled=settings.vad_enabled,
        min_silence_ms=settings.min_silence_ms,
        min_segment_s=settings.min_segment_s,
        max_segment_s=settings.max_segment_s,
    )

    async def send_segments(segments, final=False):
        for s in segments:
            await ws.send_json({"text": s.text, "start": s.start, "end": s.end, "final": final})

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break

            # Text frames carry config / control (JSON).
            if msg.get("text") is not None:
                try:
                    cfg = json.loads(msg["text"])
                except Exception:
                    cfg = {}
                if cfg.get("action") == "end":
                    await send_segments(await loop.run_in_executor(None, seg.flush), final=True)
                    continue
                if cfg.get("language") is not None:
                    state["language"] = cfg["language"] or None
                if cfg.get("task"):
                    state["task"] = cfg["task"]
                if cfg.get("sample_rate"):
                    sample_rate = int(cfg["sample_rate"])
                continue

            data = msg.get("bytes")
            if not data:
                continue
            arr = audio.pcm16_to_float32(data, sample_rate)
            segments = await loop.run_in_executor(None, seg.add_audio, arr)
            await send_segments(segments, final=False)

        # Connection closing -> flush whatever remains (no data loss).
        await send_segments(await loop.run_in_executor(None, seg.flush), final=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("ws_transcribe error")
        try:
            await ws.send_json({"error": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
