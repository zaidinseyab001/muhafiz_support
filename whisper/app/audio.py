"""ffmpeg-based audio decoding for files, URLs, and live streams.

Everything is normalized to 16 kHz mono float32, which is what the Whisper
feature extractor expects. ffmpeg (installed in the image) handles every input
protocol: local files, http(s), rtsp, rtmp, hls (m3u8), etc.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Iterator, List, Optional

import numpy as np

SAMPLE_RATE = 16000
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _input_args(source: str) -> List[str]:
    """Per-protocol ffmpeg input flags."""
    args: List[str] = []
    low = source.lower()
    if low.startswith("rtsp://"):
        # TCP is far more reliable than the default UDP for RTSP.
        args += ["-rtsp_transport", "tcp"]
    args += ["-i", source]
    return args


def _base_cmd() -> List[str]:
    return [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error"]


def _output_args() -> List[str]:
    return ["-vn", "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"]


def decode_bytes(raw: bytes) -> np.ndarray:
    """Decode an in-memory audio/video file (any format) to 16k mono float32."""
    cmd = _base_cmd() + ["-i", "pipe:0"] + _output_args()
    proc = subprocess.run(cmd, input=raw, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.decode("utf-8", "ignore")[:500] or "ffmpeg decode failed"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def decode_source(source: str, timeout: Optional[int] = None) -> np.ndarray:
    """Decode a finite file/URL (e.g. http://host/clip.mp3) to 16k mono float32."""
    cmd = _base_cmd() + _input_args(source) + _output_args()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Timed out fetching/decoding source after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.decode("utf-8", "ignore")[:500] or "ffmpeg decode failed"
        )
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def iter_stream_segments(source: str, segment_s: float) -> Iterator[np.ndarray]:
    """Continuously decode a LIVE source, yielding ~segment_s float32 arrays.

    Runs until the stream ends or the consumer stops iterating (which triggers
    ffmpeg teardown in the finally block).
    """
    seg_bytes = int(segment_s * SAMPLE_RATE) * 4  # float32 = 4 bytes/sample
    cmd = _base_cmd() + _input_args(source) + _output_args()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        buf = b""
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(seg_bytes - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) >= seg_bytes:
                yield np.frombuffer(buf, dtype=np.float32).copy()
                buf = b""
        if buf:
            yield np.frombuffer(buf, dtype=np.float32).copy()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def pcm16_to_float32(raw: bytes, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Convert raw s16le mono PCM to 16k mono float32 (linear-resample if needed)."""
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if sample_rate != SAMPLE_RATE and arr.size:
        n_out = int(round(arr.size * SAMPLE_RATE / sample_rate))
        if n_out > 0:
            x_old = np.linspace(0.0, 1.0, num=arr.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            arr = np.interp(x_new, x_old, arr).astype(np.float32)
    return arr
