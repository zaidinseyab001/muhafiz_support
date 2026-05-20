"""YOLO WebSocket inference server.

Protocol
--------
Client -> Server:
    * Binary message: JPEG (or PNG)-encoded frame bytes. One frame per message.
    * Text message (optional control JSON):
        {"cmd": "reset"}   - clear tracker stability state for this connection.

Server -> Client (two JSON messages emitted per processed frame):
    1) {"type": "detection",     ...boxes + base64 annotated JPEG }
    2) {"type": "stable_count",  ...stable-object counters         }

Run
---
    python server.py
    # override defaults:
    YOLO_DEVICE=cuda:0 YOLO_MODEL=yolo11s.pt WS_PORT=8765 python server.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Optional, Tuple

import cv2
import numpy as np
import websockets
from websockets.server import WebSocketServerProtocol

import config
from inference import YoloInference
from tracker import StabilityFilter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("yolo-ws")


def _decode_frame(payload: bytes) -> Optional[np.ndarray]:
    arr = np.frombuffer(payload, dtype=np.uint8)
    if arr.size == 0:
        return None
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _encode_annotated(frame_bgr: np.ndarray) -> str:
    ok, buf = cv2.imencode(
        ".jpg",
        frame_bgr,
        [int(cv2.IMWRITE_JPEG_QUALITY), config.ANNOTATED_JPEG_QUALITY],
    )
    if not ok:
        return ""
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _extract_boxes(result, names) -> Tuple[list, dict, list]:
    """Pull detections out of the Ultralytics Result object."""
    out: list = []
    id_to_class: dict = {}
    track_ids: list = []

    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return out, id_to_class, track_ids

    xyxy = boxes.xyxy.cpu().numpy()
    confs = boxes.conf.cpu().numpy() if boxes.conf is not None else None
    clss = boxes.cls.cpu().numpy().astype(int) if boxes.cls is not None else None
    ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else None

    name_lookup = names if isinstance(names, dict) else {}

    for i in range(len(xyxy)):
        x1, y1, x2, y2 = (float(v) for v in xyxy[i])
        cls_id = int(clss[i]) if clss is not None else -1
        conf = float(confs[i]) if confs is not None else 0.0
        tid = int(ids[i]) if ids is not None else None

        if tid is not None:
            id_to_class[tid] = cls_id
            track_ids.append(tid)

        out.append({
            "track_id": tid,
            "class_id": cls_id,
            "class_name": name_lookup.get(cls_id, str(cls_id)),
            "confidence": round(conf, 4),
            "bbox": {
                "x1": round(x1, 2), "y1": round(y1, 2),
                "x2": round(x2, 2), "y2": round(y2, 2),
                "w": round(x2 - x1, 2),
                "h": round(y2 - y1, 2),
            },
        })
    return out, id_to_class, track_ids


async def _send_json(ws: WebSocketServerProtocol, payload: dict) -> None:
    await ws.send(json.dumps(payload, separators=(",", ":")))


async def handle_connection(ws: WebSocketServerProtocol) -> None:
    peer = ws.remote_address
    log.info("Client connected: %s", peer)

    inference = YoloInference()
    stability = StabilityFilter()
    frame_id = 0
    t_session = time.time()
    loop = asyncio.get_running_loop()

    try:
        async for message in ws:
            # Text-frame -> control command.
            if isinstance(message, str):
                try:
                    ctrl = json.loads(message)
                except json.JSONDecodeError:
                    await _send_json(ws, {
                        "type": "error",
                        "message": "expected binary frame or JSON control message",
                    })
                    continue
                if ctrl.get("cmd") == "reset":
                    stability = StabilityFilter()
                    await _send_json(ws, {"type": "ack", "cmd": "reset"})
                else:
                    await _send_json(ws, {
                        "type": "error",
                        "message": f"unknown cmd: {ctrl.get('cmd')!r}",
                    })
                continue

            t0 = time.time()
            frame = _decode_frame(message)
            if frame is None:
                await _send_json(ws, {
                    "type": "error",
                    "frame_id": frame_id,
                    "message": "could not decode frame",
                })
                continue

            frame_id += 1

            # Inference is synchronous and CPU/GPU-bound — push it off the
            # event loop so other connections (and WS pings) keep flowing.
            result = await loop.run_in_executor(None, inference.track, frame)

            boxes, id_to_class, track_ids = _extract_boxes(result, result.names)
            annotated_b64 = _encode_annotated(result.plot())
            stab = stability.update(track_ids, id_to_class)
            inference_ms = round((time.time() - t0) * 1000.0, 2)
            now = time.time()

            await _send_json(ws, {
                "type": "detection",
                "frame_id": frame_id,
                "timestamp": now,
                "inference_ms": inference_ms,
                "image_size": {"w": int(frame.shape[1]), "h": int(frame.shape[0])},
                "detections": boxes,
                "annotated_image_b64": annotated_b64,
                "annotated_image_format": "jpeg",
            })

            await _send_json(ws, {
                "type": "stable_count",
                "frame_id": frame_id,
                "timestamp": now,
                **stab,
            })

    except websockets.ConnectionClosed as e:
        log.info("Client %s closed: code=%s reason=%r", peer, e.code, e.reason)
    except Exception:
        log.exception("Error handling client %s", peer)
    finally:
        inference.close()
        elapsed = max(time.time() - t_session, 1e-6)
        log.info(
            "Disconnected %s after %d frames (%.2f fps avg)",
            peer, frame_id, frame_id / elapsed,
        )


async def main() -> None:
    log.info(
        "Starting YOLO WebSocket server on ws://%s:%s  (device=%s, half=%s, model=%s, tracker=%s)",
        config.WS_HOST, config.WS_PORT, config.DEVICE, config.USE_HALF,
        config.MODEL_PATH, config.TRACKER_CONFIG,
    )
    async with websockets.serve(
        handle_connection,
        config.WS_HOST,
        config.WS_PORT,
        max_size=config.WS_MAX_SIZE,
        ping_interval=config.WS_PING_INTERVAL,
        ping_timeout=config.WS_PING_TIMEOUT,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
