"""Runtime configuration, sourced entirely from environment variables / .env.

Every tunable lives here so the air-gapped operator only ever edits `.env`.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Model -------------------------------------------------------------
    model_path: str = "/models/whisper-large-v3"
    # "cuda" or "cpu". cuda:0 also accepted.
    device: str = "cuda"
    # torch dtype used for the model weights/compute on GPU.
    # one of: float16, bfloat16, float32
    compute_type: str = "float16"

    # --- Decoding defaults (overridable per request) -----------------------
    # Seconds per chunk for long-form audio. 30 matches Whisper's window.
    chunk_length_s: int = 30
    # Batched inference over chunks. Lower this if you hit VRAM limits.
    batch_size: int = 8
    # Default language (e.g. "en"). Empty => auto-detect.
    default_language: str = ""
    # "transcribe" or "translate".
    default_task: str = "transcribe"
    # Return per-segment timestamps by default.
    return_timestamps: bool = True

    # --- Live / URL sources ------------------------------------------------
    # Max seconds to wait when fetching/decoding a finite URL.
    url_fetch_timeout: int = 300
    # Allow server-side fetching of arbitrary URLs / live streams.
    # In a locked-down deployment set WHISPER_ALLOW_REMOTE_SOURCES=false.
    allow_remote_sources: bool = True

    # --- Live segmentation (VAD + carry-over: no dup, no loss) --------------
    # Use voice-activity detection to cut segments at silence (smooth).
    # If false, segments are cut purely at max_segment_s (fixed windows).
    vad_enabled: bool = True
    # webrtcvad aggressiveness 0-3 (higher = more aggressive silence detection).
    vad_aggressiveness: int = 2
    # A gap of silence at least this long is treated as a segment boundary.
    min_silence_ms: int = 300
    # Don't commit a segment shorter than this (unless a forced cut / flush).
    min_segment_s: float = 1.0
    # Force a cut after this much un-committed audio (bounds latency/memory).
    max_segment_s: float = 25.0
    # Granularity of audio pulled from a live stream and fed to the segmenter.
    stream_read_s: float = 0.5

    # --- Server ------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = SettingsConfigDict(
        env_prefix="WHISPER_",
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
