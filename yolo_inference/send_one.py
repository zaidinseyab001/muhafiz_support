import asyncio, json, base64, sys
from pathlib import Path
import websockets

async def main(path, ws_url="ws://localhost:8765"):
    img_bytes = Path(path).read_bytes()          # already JPEG/PNG on disk
    async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
        await ws.send(img_bytes)                  # <-- binary frame
        # Server emits 2 messages per frame
        for _ in range(2):
            msg = json.loads(await ws.recv())
            if msg["type"] == "detection":
                Path("annotated.jpg").write_bytes(
                    base64.b64decode(msg["annotated_image_b64"])
                )
                print("detections:", len(msg["detections"]))
            elif msg["type"] == "stable_count":
                print("stable:", msg["currently_visible_stable_count"])

asyncio.run(main(sys.argv[1]))