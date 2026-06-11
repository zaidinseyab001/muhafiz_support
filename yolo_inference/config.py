"""Runtime configuration for the YOLO WebSocket inference service.

All values are overridable via environment variables so the same code runs
unchanged on the CPU-only dev box and on the GPU prod server.
"""
from __future__ import annotations

import os
from pathlib import Path

import torch
from dotenv import load_dotenv

# Load variables from a sibling .env file before any os.getenv lookup runs.
# override=False means a real shell env var still wins over the .env entry,
# which is the behaviour you want in containerised prod deployments.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
# Ultralytics auto-downloads the weights file on first use.
# Swap to yolo11s.pt / yolo11m.pt on the GPU box for higher accuracy.
MODEL_PATH = os.getenv("YOLO_MODEL", "yolo11n.pt")

# Multi-object tracker config shipped with Ultralytics.
#   bytetrack.yaml  — faster, lower memory, good default for CCTV
#   botsort.yaml    — uses re-id features, more robust to occlusion
TRACKER_CONFIG = os.getenv("YOLO_TRACKER", "bytetrack.yaml")

CONF_THRESHOLD = _env_float("YOLO_CONF", 0.35)
IOU_THRESHOLD = _env_float("YOLO_IOU", 0.5)
IMG_SIZE = _env_int("YOLO_IMGSZ", 640)

# Optional class allowlist (COCO ids). Empty = detect everything.
# Typical CCTV-relevant ids: 0=person, 1=bicycle, 2=car, 3=motorcycle,
# 5=bus, 7=truck.
_classes_env = os.getenv("YOLO_CLASSES", "").strip()
CLASSES: list[int] | None = (
    [int(c) for c in _classes_env.split(",") if c.strip()] if _classes_env else None
)

# ---------------------------------------------------------------------------
# Device — auto-detect; honour explicit override
# ---------------------------------------------------------------------------
# Prefer the GPU, but fall back to CPU when it's full/too small (so a shared
# dev box where the LLM already fills VRAM doesn't crash YOLO with CUDA OOM).
AUTO_CPU_FALLBACK = os.getenv("YOLO_AUTO_CPU_FALLBACK", "1") == "1"
# yolo11x FP32 needs only a few hundred MB to load; require a small margin.
MIN_FREE_VRAM_MB = _env_int("YOLO_MIN_FREE_VRAM_MB", 1200)


def _free_vram_mb(idx: int = 0) -> float | None:
    try:
        free, _total = torch.cuda.mem_get_info(idx)
        return free / (1024 * 1024)
    except Exception:
        return None


_device_env = os.getenv("YOLO_DEVICE", "auto").lower()
if _device_env == "auto":
    if torch.cuda.is_available():
        DEVICE = "cuda:0"
        if AUTO_CPU_FALLBACK:
            _free = _free_vram_mb(0)
            if _free is not None and _free < MIN_FREE_VRAM_MB:
                DEVICE = "cpu"
    else:
        DEVICE = "cpu"
else:
    DEVICE = _device_env

# FP16 only makes sense on CUDA. Disabled automatically on CPU.
USE_HALF = DEVICE.startswith("cuda") and os.getenv("YOLO_HALF", "1") == "1"

# ---------------------------------------------------------------------------
# Stability filter
# ---------------------------------------------------------------------------
# A track id is promoted to "stable" only after it has been continuously
# detected for STABILITY_FRAMES frames. This suppresses single-frame false
# positives and tracker id-flicker so the count emitted to consumers does
# not bounce around.
STABILITY_FRAMES = _env_int("STABILITY_FRAMES", 5)

# Once a track id has been missing for more than this many consecutive
# frames, its state is dropped. Its contribution to the cumulative
# "unique stable ids ever seen" counter is retained.
STABILITY_GRACE = _env_int("STABILITY_GRACE", 30)

# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = _env_int("WS_PORT", 8765)

# Max inbound message size — a 1080p JPEG at quality 90 is ~300 KB; 16 MB
# gives plenty of headroom for higher resolutions.
WS_MAX_SIZE = _env_int("WS_MAX_SIZE", 16 * 1024 * 1024)
WS_PING_INTERVAL = _env_int("WS_PING_INTERVAL", 20)
WS_PING_TIMEOUT = _env_int("WS_PING_TIMEOUT", 20)

# JPEG quality for the annotated frame echoed back to the client.
ANNOTATED_JPEG_QUALITY = _env_int("ANNOTATED_JPEG_QUALITY", 80)
