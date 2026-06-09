"""Streaming segmentation for live transcription.

Design goal: NO duplication and NO data loss.

How: audio is accumulated in a buffer. We only "commit" (transcribe + emit) the
buffer up to a *silence gap* found by VAD, then keep everything after that gap
buffered (carry-over) for the next segment. Segments therefore PARTITION the
audio — every sample is transcribed exactly once:

  - no duplication: committed spans never overlap.
  - no data loss:   the un-committed tail is carried over and finally flushed.

If no silence appears for `max_segment_s`, we force a cut at the quietest frame
(best-effort, minimizes word splitting) so latency/memory stay bounded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger("whisper-api")

try:
    import webrtcvad  # provided by webrtcvad-wheels
    _HAS_WEBRTC = True
except Exception:  # pragma: no cover
    _HAS_WEBRTC = False


@dataclass
class Segment:
    start: float
    end: float
    text: str


class _Vad:
    """Per-frame speech/non-speech detector.

    Uses webrtcvad when available; otherwise an adaptive energy threshold.
    When `enabled` is False, every frame is reported as speech, so the
    segmenter falls back to fixed `max_segment_s` windows.
    """

    def __init__(self, sample_rate: int, frame_ms: int, aggressiveness: int,
                 enabled: bool = True, energy_thresh: float = 0.004):
        self.sr = sample_rate
        self.frame_len = int(sample_rate * frame_ms / 1000)
        self.enabled = enabled
        self.energy_thresh = energy_thresh
        self._vad = None
        if enabled and _HAS_WEBRTC:
            try:
                self._vad = webrtcvad.Vad(int(max(0, min(3, aggressiveness))))
            except Exception:  # pragma: no cover
                self._vad = None

    def speech_flags(self, audio: np.ndarray) -> np.ndarray:
        n_frames = len(audio) // self.frame_len
        if n_frames == 0:
            return np.zeros(0, dtype=bool)
        if not self.enabled:
            return np.ones(n_frames, dtype=bool)

        if self._vad is not None:
            clipped = np.clip(audio[: n_frames * self.frame_len], -1.0, 1.0)
            pcm16 = (clipped * 32767.0).astype("<i2")
            flags = np.empty(n_frames, dtype=bool)
            for i in range(n_frames):
                fr = pcm16[i * self.frame_len:(i + 1) * self.frame_len].tobytes()
                try:
                    flags[i] = self._vad.is_speech(fr, self.sr)
                except Exception:
                    flags[i] = True  # err on the side of keeping audio
            return flags

        # Energy fallback with adaptive threshold.
        frames = audio[: n_frames * self.frame_len].reshape(n_frames, self.frame_len)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
        thr = max(self.energy_thresh, 0.5 * float(np.median(rms)))
        return rms > thr


class StreamingTranscriber:
    """Feed audio in arbitrary-sized arrays; get back committed Segments."""

    def __init__(
        self,
        transcribe_fn: Callable[[np.ndarray], str],
        *,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        aggressiveness: int = 2,
        vad_enabled: bool = True,
        min_silence_ms: int = 300,
        min_segment_s: float = 1.0,
        max_segment_s: float = 25.0,
        end_keep_ms: int = 200,
    ):
        self.transcribe_fn = transcribe_fn
        self.sr = sample_rate
        self.frame_len = int(sample_rate * frame_ms / 1000)
        self.vad = _Vad(sample_rate, frame_ms, aggressiveness, enabled=vad_enabled)
        self.min_silence_frames = max(1, int(min_silence_ms / frame_ms))
        self.min_segment_samples = int(min_segment_s * sample_rate)
        self.max_segment_samples = int(max_segment_s * sample_rate)
        # Keep this much most-recent audio un-committed (a word may be ongoing).
        self.end_keep_frames = max(1, int(end_keep_ms / frame_ms))
        self.buf = np.zeros(0, dtype=np.float32)
        self.t_offset = 0.0  # absolute start time of buf[0]

    # -- public ------------------------------------------------------------
    def add_audio(self, arr: np.ndarray) -> List[Segment]:
        if arr is None or arr.size == 0:
            return []
        self.buf = np.concatenate([self.buf, np.asarray(arr, dtype=np.float32)])
        out: List[Segment] = []
        while True:
            cut = self._find_cut()
            if not cut or cut <= 0 or cut >= len(self.buf):
                # cut==len(buf) would commit everything incl. the keep-tail; only
                # allowed via flush(). Stop here otherwise.
                if cut is not None and cut >= len(self.buf) and len(self.buf) >= self.max_segment_samples:
                    out.append(self._emit(self.buf))
                    self.buf = np.zeros(0, dtype=np.float32)
                break
            out.append(self._emit(self.buf[:cut]))
            self.buf = self.buf[cut:]
        return out

    def flush(self) -> List[Segment]:
        out: List[Segment] = []
        if self.buf.size > 0:
            out.append(self._emit(self.buf))
        self.buf = np.zeros(0, dtype=np.float32)
        return out

    # -- internals ---------------------------------------------------------
    def _emit(self, seg_audio: np.ndarray) -> Segment:
        dur = len(seg_audio) / self.sr
        text = ""
        try:
            text = (self.transcribe_fn(seg_audio) or "").strip()
        except Exception:
            logger.exception("segment transcription failed")
        seg = Segment(round(self.t_offset, 2), round(self.t_offset + dur, 2), text)
        self.t_offset += dur
        return seg

    def _find_cut(self) -> Optional[int]:
        n = len(self.buf)
        if n < self.min_segment_samples:
            return None

        flags = self.vad.speech_flags(self.buf)
        nf = len(flags)
        if nf == 0:
            return None

        end_frame = nf - self.end_keep_frames
        min_frame = max(1, self.min_segment_samples // self.frame_len)
        if end_frame <= min_frame:
            return self._forced_cut(flags) if n >= self.max_segment_samples else None

        # Find the last silence run (>= min_silence_frames) in [min_frame, end_frame).
        i = end_frame - 1
        while i >= min_frame:
            if not flags[i]:
                j = i
                while j >= 0 and not flags[j]:
                    j -= 1
                run_start, run_end = j + 1, i + 1
                if run_end - run_start >= self.min_silence_frames:
                    mid = (run_start + run_end) // 2
                    if mid >= min_frame:
                        return mid * self.frame_len
                i = j
            else:
                i -= 1

        if n >= self.max_segment_samples:
            return self._forced_cut(flags)
        return None

    def _forced_cut(self, flags: np.ndarray) -> int:
        nf = len(flags)
        end_frame = max(1, nf - self.end_keep_frames)
        min_frame = max(1, self.min_segment_samples // self.frame_len)
        if end_frame <= min_frame:
            cut_frame = max(min_frame, end_frame)
        else:
            n_use = end_frame * self.frame_len
            frames = self.buf[:n_use].reshape(end_frame, self.frame_len)
            rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
            cut_frame = min_frame + int(np.argmin(rms[min_frame:end_frame]))
        return max(self.frame_len, cut_frame * self.frame_len)
