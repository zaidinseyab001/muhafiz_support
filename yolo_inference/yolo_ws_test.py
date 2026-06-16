#!/usr/bin/env python3
"""Send an image or video through the YOLO WebSocket and save the annotated output.

The server returns, per frame, a `detection` JSON (boxes + a base64 annotated
JPEG with the boxes already drawn) and a `stable_count` JSON. This script decodes
the annotated image(s), writes them back out, and prints a summary.

Usage
-----
    python yolo_ws_test.py <path-to-image-or-video>
    python yolo_ws_test.py frame.jpg --ws ws://192.168.18.87:7860
    python yolo_ws_test.py clip.mp4 --out annotated.mp4 --fps 10

    # image  -> writes <name>_annotated.jpg
    # video  -> writes <name>_annotated.mp4 (boxes burned into every frame)

Deps: websockets (always). Video also needs opencv-python + numpy.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

import websockets

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

DEFAULT_WS = "ws://192.168.18.87:7860"


async def _recv_pair(ws):
    """Read the two messages the server emits per frame; return (detection, stable)."""
    detection, stable = None, None
    for _ in range(2):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
        t = msg.get("type")
        if t == "detection":
            detection = msg
        elif t == "stable_count":
            stable = msg
        elif t == "error":
            raise RuntimeError(f"server error: {msg.get('message')}")
    return detection, stable


def _summarize(det: dict) -> str:
    dets = det.get("detections", [])
    parts = [f"{d['class_name']}({d['confidence']:.2f})" for d in dets]
    return f"{len(dets)} det [{', '.join(parts)}]  {det.get('inference_ms')}ms"


# ---------------------------------------------------------------------------
# Image — no cv2 needed: just write the annotated JPEG the server returns.
# ---------------------------------------------------------------------------
async def run_image(path: Path, ws_url: str, out: Path) -> None:
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=20) as ws:
        await ws.send(path.read_bytes())                 # <-- binary frame
        det, stab = await _recv_pair(ws)
        print(f"[image] {path.name}: {_summarize(det)}")
        print("  detections:", json.dumps(det.get("detections", []), indent=2))
        b64 = det.get("annotated_image_b64", "")
        if b64:
            out.write_bytes(base64.b64decode(b64))
            print(f"  -> wrote {out}  (annotated, boxes drawn)")
        else:
            print("  WARNING: server returned no annotated image.")
        if stab:
            print(f"  stable: visible={stab.get('currently_visible_stable_count')} "
                  f"raw_tracks={stab.get('raw_active_tracks')}")


# ---------------------------------------------------------------------------
# Video — needs opencv-python + numpy to read frames and write the output clip.
# ---------------------------------------------------------------------------
async def run_video(path: Path, ws_url: str, out: Path, fps_cap: float, jpeg_q: int) -> None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("ERROR: video mode needs opencv-python + numpy:\n"
              "         pip install opencv-python numpy", file=sys.stderr)
        sys.exit(1)

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        print(f"ERROR: cannot open video {path}", file=sys.stderr)
        sys.exit(1)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    out_fps = min(src_fps, fps_cap) if fps_cap > 0 else src_fps
    step = max(1, round(src_fps / out_fps)) if out_fps > 0 else 1

    def b64_to_bgr(b64: str):
        if not b64:
            return None
        arr = np.frombuffer(base64.b64decode(b64), dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    writer = None
    sent = 0
    frame_idx = -1
    last_stable = {}
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024, open_timeout=20) as ws:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            frame_idx += 1
            if frame_idx % step != 0:        # frame-rate cap (subsample)
                continue

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_q])
            if not ok:
                continue
            await ws.send(buf.tobytes())     # <-- binary frame
            det, stab = await _recv_pair(ws)
            sent += 1
            last_stable = stab or last_stable

            annotated = b64_to_bgr(det.get("annotated_image_b64", ""))
            if annotated is None:
                annotated = frame
            if writer is None:
                h, w = annotated.shape[:2]
                writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), out_fps, (w, h))
            writer.write(annotated)

            if sent % 10 == 0:
                print(f"  frame {sent}: {_summarize(det)}  "
                      f"stable_visible={stab.get('currently_visible_stable_count') if stab else '-'}")

    cap.release()
    if writer is not None:
        writer.release()
    print(f"[video] {path.name}: sent {sent} frames -> wrote {out}")
    if last_stable:
        print(f"  final stable: visible={last_stable.get('currently_visible_stable_count')} "
              f"unique_total={last_stable.get('stable_total_unique')} "
              f"by_class={last_stable.get('currently_visible_stable_by_class')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run an image/video through the YOLO WebSocket and save annotated output.")
    ap.add_argument("path", help="image or video file to process")
    ap.add_argument("--ws", default=DEFAULT_WS, help=f"YOLO websocket URL (default {DEFAULT_WS})")
    ap.add_argument("--out", default=None, help="output path (default: <name>_annotated.<ext>)")
    ap.add_argument("--fps", type=float, default=0.0, help="cap frames/sec sent for video (0 = source fps)")
    ap.add_argument("--jpeg-quality", type=int, default=85, help="JPEG quality for video frames sent (default 85)")
    args = ap.parse_args()

    src = Path(args.path)
    if not src.is_file():
        print(f"ERROR: not a file: {src}", file=sys.stderr)
        sys.exit(1)

    ext = src.suffix.lower()
    if ext in IMAGE_EXTS:
        out = Path(args.out) if args.out else src.with_name(f"{src.stem}_annotated.jpg")
        asyncio.run(run_image(src, args.ws, out))
    elif ext in VIDEO_EXTS:
        out = Path(args.out) if args.out else src.with_name(f"{src.stem}_annotated.mp4")
        asyncio.run(run_video(src, args.ws, out, args.fps, args.jpeg_quality))
    else:
        print(f"ERROR: unsupported extension '{ext}'. Images: {sorted(IMAGE_EXTS)}  Videos: {sorted(VIDEO_EXTS)}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
