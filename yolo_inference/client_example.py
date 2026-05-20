"""Reference client: pulls frames from a CCTV RTSP stream (or webcam) and
streams them to the YOLO WebSocket server, printing the responses.

Examples
--------
    # RTSP CCTV feed
    python client_example.py "rtsp://user:pass@192.168.1.50:554/Streaming/Channels/101"

    # Local webcam (device index 0)
    python client_example.py 0 --fps 10

    # Save annotated frames returned by the server
    python client_example.py 0 --save-annotated ./out
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Optional

import cv2
import websockets


async def _consume(ws, save_dir: Optional[Path]) -> None:
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)

    async for msg in ws:
        if not isinstance(msg, str):
            continue
        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            continue

        ptype = payload.get("type")
        if ptype == "detection":
            n = len(payload.get("detections", []))
            print(
                f"[detection] frame={payload['frame_id']:>5}  "
                f"boxes={n:>2}  inference={payload['inference_ms']:>6} ms"
            )
            if save_dir is not None and payload.get("annotated_image_b64"):
                out = save_dir / f"frame_{payload['frame_id']:06d}.jpg"
                out.write_bytes(base64.b64decode(payload["annotated_image_b64"]))
        elif ptype == "stable_count":
            print(
                f"[stable   ] frame={payload['frame_id']:>5}  "
                f"visible_stable={payload['currently_visible_stable_count']:>3}  "
                f"unique_total={payload['stable_total_unique']:>4}  "
                f"raw_tracks={payload['raw_active_tracks']:>3}"
            )
        elif ptype == "error":
            print(f"[error    ] {payload}", file=sys.stderr)
        elif ptype == "ack":
            print(f"[ack      ] {payload}")


async def stream(
    source: str,
    ws_url: str,
    fps_cap: float,
    jpeg_quality: int,
    save_dir: Optional[Path],
) -> None:
    cap_src: object = int(source) if source.isdigit() else source
    cap = cv2.VideoCapture(cap_src)
    if not cap.isOpened():
        print(f"Failed to open source: {source}", file=sys.stderr)
        sys.exit(1)

    frame_interval = (1.0 / fps_cap) if fps_cap > 0 else 0.0
    last_send = 0.0

    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
        print(f"Connected to {ws_url}  (source={source})")
        recv_task = asyncio.create_task(_consume(ws, save_dir))
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("Source read failed; reconnecting capture in 2s...", file=sys.stderr)
                    await asyncio.sleep(2)
                    cap.release()
                    cap = cv2.VideoCapture(cap_src)
                    continue

                if frame_interval > 0:
                    now = time.time()
                    wait = frame_interval - (now - last_send)
                    if wait > 0:
                        await asyncio.sleep(wait)
                last_send = time.time()

                ok, buf = cv2.imencode(
                    ".jpg", frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
                )
                if not ok:
                    continue
                await ws.send(buf.tobytes())
        finally:
            cap.release()
            recv_task.cancel()


def main() -> None:
    p = argparse.ArgumentParser(description="YOLO WS inference client")
    p.add_argument("source", help="RTSP/HTTP URL or webcam index (e.g. 0)")
    p.add_argument("--ws", default="ws://localhost:8765", help="WebSocket server URL")
    p.add_argument("--fps", type=float, default=10.0,
                   help="Max frames/sec to send. 0 = unlimited.")
    p.add_argument("--jpeg-quality", type=int, default=80)
    p.add_argument("--save-annotated", type=Path, default=None,
                   help="If set, save server-returned annotated frames to this directory.")
    args = p.parse_args()

    try:
        asyncio.run(stream(
            args.source, args.ws, args.fps, args.jpeg_quality, args.save_annotated,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
