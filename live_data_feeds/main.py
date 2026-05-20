import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from tempfile import mkdtemp

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

BASE_DIR = Path(__file__).parent
VIDEO_DIR = BASE_DIR / "surveillance"
AUDIO_DIR = BASE_DIR / "voice analysis"

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}

# ---- HLS video settings -------------------------------------------------
HLS_SEGMENT_SECONDS = 4          # segment duration → ~8-12s glass-to-glass latency, smoother playback
HLS_LIST_SIZE = 10               # number of segments visible in playlist (~40s window)
HLS_VIDEO_BITRATE_K = 1500       # kbps; used during one-time normalization
HLS_MAX_WIDTH = 1280
HLS_MAX_HEIGHT = 720
HLS_FPS = 25
HLS_PRESET = "veryfast"          # libx264 preset for the one-time normalization pass
HLS_LOOP_REPS = 1000             # concat-manifest repetitions; ffmpeg's -stream_loop is unreliable on concat
_HLS_PARENT = BASE_DIR / ".tmp"
_HLS_PARENT.mkdir(exist_ok=True)
HLS_DIR = Path(mkdtemp(prefix="surveillance_hls_", dir=str(_HLS_PARENT)))
NORMALIZED_DIR = BASE_DIR / ".normalized"  # cache of pre-encoded uniform-format clips

# ---- Audio MP3 settings -------------------------------------------------
AUDIO_SAMPLE_RATE = 44100
AUDIO_BITRATE = "128k"
AUDIO_CHANNELS = 2


def _list_media(directory: Path, exts: set[str]) -> list[Path]:
    if not directory.exists():
        raise RuntimeError(f"Media directory missing: {directory}")
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in exts)
    if not files:
        raise RuntimeError(f"No media files in {directory}")
    return files


def _normalize_video(src: Path, dst: Path) -> None:
    """One-time pass: re-encode src to a uniform 1280x720@25 H.264 MPEG-TS
    file so the live concat demuxer can stream-copy between clips without
    the encoder choking on resolution / fps changes."""
    vf = (
        f"scale={HLS_MAX_WIDTH}:{HLS_MAX_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={HLS_MAX_WIDTH}:{HLS_MAX_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={HLS_FPS}"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", HLS_PRESET,
        "-pix_fmt", "yuv420p",
        "-profile:v", "main",
        "-level", "4.0",
        "-g", str(HLS_FPS * HLS_SEGMENT_SECONDS),
        "-keyint_min", str(HLS_FPS * HLS_SEGMENT_SECONDS),
        "-sc_threshold", "0",
        "-b:v", f"{HLS_VIDEO_BITRATE_K}k",
        "-maxrate", f"{HLS_VIDEO_BITRATE_K}k",
        "-bufsize", f"{HLS_VIDEO_BITRATE_K * 2}k",
        "-an",
        "-f", "mpegts",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def _prepare_normalized(sources: list[Path], cache_dir: Path) -> list[Path]:
    """Return a list of normalized .ts files (one per source). Cached on disk —
    re-normalizes only when the cached file is missing or older than the source."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for src in sources:
        dst = cache_dir / (src.stem + ".ts")
        if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            print(f"[normalize] {src.name} -> {dst.name}", flush=True)
            try:
                _normalize_video(src, dst)
            except subprocess.CalledProcessError as e:
                print(f"[normalize] FAILED {src.name}: {e}", flush=True)
                if dst.exists():
                    dst.unlink()
                continue
        out.append(dst)
    if not out:
        raise RuntimeError(f"No videos could be normalized into {cache_dir}")
    return out


# ---------------------------------------------------------------------------
# Video broadcaster — single ffmpeg, HLS output served to all clients
# ---------------------------------------------------------------------------
class HLSVideoBroadcaster:
    """Continuously transcodes the playlist of videos to HLS segments using a
    single ffmpeg process. All HTTP clients pull from the same .m3u8 / .ts
    files — encoding cost and bandwidth do not scale with viewer count.
    """

    PLAYLIST_NAME = "stream.m3u8"
    SEGMENT_PREFIX = "seg"

    def __init__(self, videos: list[Path], hls_dir: Path):
        self.videos = videos
        self.hls_dir = hls_dir
        self.index = 0
        self.index_lock = threading.Lock()

        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._restart_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_dir()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="hls-video", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._restart_event.set()
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        if self._thread:
            self._thread.join(timeout=5)
        self._cleanup_dir()

    def current_name(self) -> str:
        with self.index_lock:
            return self.videos[self.index].name

    def current_index(self) -> int:
        with self.index_lock:
            return self.index

    def advance(self) -> str:
        with self.index_lock:
            self.index = (self.index + 1) % len(self.videos)
            name = self.videos[self.index].name
        self._restart_event.set()
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        return name

    def playlist_path(self) -> Path:
        return self.hls_dir / self.PLAYLIST_NAME

    def _cleanup_dir(self) -> None:
        if not self.hls_dir.exists():
            return
        for p in self.hls_dir.iterdir():
            try:
                p.unlink()
            except (IsADirectoryError, FileNotFoundError):
                pass

    def _build_manifest(self, start_index: int) -> Path:
        manifest = self.hls_dir / "concat.txt"
        ordered = self.videos[start_index:] + self.videos[:start_index]
        # Escape single quotes once per path per concat demuxer rules.
        escaped = [str(v.absolute()).replace("'", "'\\''") for v in ordered]
        # Repeat the whole playlist many times so the manifest never runs out
        # during a realistic session — ffmpeg's -stream_loop -1 is unreliable
        # with the concat demuxer and exits at end-of-input instead of looping.
        lines = []
        for _ in range(HLS_LOOP_REPS):
            for path in escaped:
                lines.append(f"file '{path}'\n")
        manifest.write_text("".join(lines))
        return manifest

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self.index_lock:
                start_index = self.index

            manifest = self._build_manifest(start_index)
            self._restart_event.clear()

            # Inputs are pre-normalized to a uniform codec/resolution/fps, so we
            # can stream-copy at runtime. Zero re-encoding → near-zero CPU and
            # clean transitions across the concat demuxer's file boundaries.
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-re",
                "-f", "concat",
                "-safe", "0",
                "-i", str(manifest),
                "-c:v", "copy",
                "-an",
                "-f", "hls",
                "-hls_time", str(HLS_SEGMENT_SECONDS),
                "-hls_list_size", str(HLS_LIST_SIZE),
                "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
                "-hls_segment_type", "mpegts",
                "-hls_segment_filename", str(self.hls_dir / f"{self.SEGMENT_PREFIX}_%05d.ts"),
                str(self.playlist_path()),
            ]

            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                time.sleep(2.0)
                continue

            with self._proc_lock:
                self._proc = proc

            try:
                while not self._stop_event.is_set() and not self._restart_event.is_set():
                    if proc.poll() is not None:
                        break
                    time.sleep(0.5)
            finally:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                with self._proc_lock:
                    self._proc = None


# ---------------------------------------------------------------------------
# Audio broadcaster — single ffmpeg producer, per-viewer chunk queue fan-out
# ---------------------------------------------------------------------------
class AudioBroadcaster:
    """Decodes audio files to a continuous MP3 stream and fans bytes out to
    every connected listener. Listeners join 'live' — they get bytes emitted
    after they connect, not the beginning of the current file.
    """

    CHUNK_SIZE = 4096
    QUEUE_MAXSIZE = 64  # ~per-viewer buffer; oldest is dropped if a viewer is slow

    def __init__(self, audios: list[Path]):
        self.audios = audios
        self.index = 0
        self.index_lock = threading.Lock()

        self._viewers: set[queue.Queue[bytes | None]] = set()
        self._viewers_lock = threading.Lock()

        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._skip_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="audio-broadcaster", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._skip_event.set()
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        with self._viewers_lock:
            for q in self._viewers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        if self._thread:
            self._thread.join(timeout=3)

    def current_name(self) -> str:
        with self.index_lock:
            return self.audios[self.index].name

    def current_index(self) -> int:
        with self.index_lock:
            return self.index

    def advance(self) -> str:
        with self.index_lock:
            self.index = (self.index + 1) % len(self.audios)
            name = self.audios[self.index].name
        self._skip_event.set()
        with self._proc_lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        return name

    def add_viewer(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAXSIZE)
        with self._viewers_lock:
            self._viewers.add(q)
        return q

    def remove_viewer(self, q: queue.Queue) -> None:
        with self._viewers_lock:
            self._viewers.discard(q)

    def _broadcast(self, chunk: bytes) -> None:
        with self._viewers_lock:
            viewers = list(self._viewers)
        for q in viewers:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                # Slow viewer — drop oldest to make room.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass

    def _advance_internal(self) -> None:
        with self.index_lock:
            self.index = (self.index + 1) % len(self.audios)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            with self.index_lock:
                audio_path = self.audios[self.index]

            self._skip_event.clear()
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-re",                            # read input at native rate (real-time pacing)
                "-i", str(audio_path),
                "-vn",
                "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_SAMPLE_RATE),
                "-c:a", "libmp3lame",
                "-b:a", AUDIO_BITRATE,
                "-f", "mp3",
                "pipe:1",
            ]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                # ffmpeg missing — sleep briefly and try next file.
                time.sleep(1.0)
                self._advance_internal()
                continue

            with self._proc_lock:
                self._proc = proc

            try:
                assert proc.stdout is not None
                while not self._stop_event.is_set() and not self._skip_event.is_set():
                    chunk = proc.stdout.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    self._broadcast(chunk)
            finally:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=2)
                except Exception:
                    proc.kill()
                with self._proc_lock:
                    self._proc = None

            if not self._skip_event.is_set():
                self._advance_internal()


# ---------------------------------------------------------------------------
# FastAPI wiring
# ---------------------------------------------------------------------------
app = FastAPI(title="Surveillance Live Feed")

_video_sources = _list_media(VIDEO_DIR, VIDEO_EXTS)
_normalized_videos = _prepare_normalized(_video_sources, NORMALIZED_DIR)
video_broadcaster = HLSVideoBroadcaster(_normalized_videos, HLS_DIR)
audio_broadcaster = AudioBroadcaster(_list_media(AUDIO_DIR, AUDIO_EXTS))


@app.on_event("startup")
def _startup() -> None:
    video_broadcaster.start()
    audio_broadcaster.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    video_broadcaster.stop()
    audio_broadcaster.stop()
    shutil.rmtree(HLS_DIR, ignore_errors=True)


# ---- HLS file serving ----------------------------------------------------
hls_router = APIRouter(prefix="/hls", tags=["hls"])

_HLS_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "X-Accel-Buffering": "no",
    "Access-Control-Allow-Origin": "*",
}


@hls_router.get("/stream.m3u8")
def hls_playlist():
    path = video_broadcaster.playlist_path()
    # On first request the encoder may still be priming — wait briefly.
    if not path.exists():
        deadline = time.monotonic() + 8.0
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
    if not path.exists():
        raise HTTPException(status_code=503, detail="Stream not ready")
    # Read into memory and return as a plain Response — the playlist is rewritten
    # every ~2s and can shrink, so honoring Range (as FileResponse does) causes
    # 416s when a client retries with a stale byte offset.
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="Stream not ready")
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers=_HLS_NO_CACHE_HEADERS,
    )


@hls_router.get("/{segment}")
def hls_segment(segment: str):
    # Strict validation — only allow files we produced.
    if "/" in segment or ".." in segment or not segment.endswith(".ts"):
        raise HTTPException(status_code=404)
    if not segment.startswith(HLSVideoBroadcaster.SEGMENT_PREFIX + "_"):
        raise HTTPException(status_code=404)
    path = video_broadcaster.hls_dir / segment
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="video/mp2t",
        headers=_HLS_NO_CACHE_HEADERS,
    )


# ---- video metadata router ----------------------------------------------
video_router = APIRouter(prefix="/surveillance_video", tags=["video"])


@video_router.get("/playlist")
def video_playlist():
    return {
        "count": len(video_broadcaster.videos),
        "current_index": video_broadcaster.current_index(),
        "videos": [v.name for v in video_broadcaster.videos],
    }


@video_router.get("/current")
def video_current():
    return {"index": video_broadcaster.current_index(), "name": video_broadcaster.current_name()}


@video_router.post("/next")
def video_next():
    name = video_broadcaster.advance()
    return {"index": video_broadcaster.current_index(), "name": name}


# ---- audio router --------------------------------------------------------
audio_router = APIRouter(prefix="/live_audio", tags=["audio"])


def _audio_chunks():
    q = audio_broadcaster.add_viewer()
    try:
        while True:
            chunk = q.get()
            if chunk is None:
                break
            yield chunk
    finally:
        audio_broadcaster.remove_viewer(q)


@audio_router.get("/stream")
def audio_stream():
    return StreamingResponse(
        _audio_chunks(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@audio_router.get("/playlist")
def audio_playlist():
    return {
        "count": len(audio_broadcaster.audios),
        "current_index": audio_broadcaster.current_index(),
        "audios": [a.name for a in audio_broadcaster.audios],
    }


@audio_router.get("/current")
def audio_current():
    return {"index": audio_broadcaster.current_index(), "name": audio_broadcaster.current_name()}


@audio_router.post("/next")
def audio_next():
    name = audio_broadcaster.advance()
    return {"index": audio_broadcaster.current_index(), "name": name}


app.include_router(hls_router)
app.include_router(video_router)
app.include_router(audio_router)


_HLS_PLAYER_SCRIPT = """
(function() {
  var video = document.getElementById('video');
  var src = '/hls/stream.m3u8';
  function startPlay() { video.play().catch(function(){}); }
  if (window.Hls && Hls.isSupported()) {
    var hls = new Hls({
      liveSyncDuration: 10,
      liveMaxLatencyDuration: 30,
      maxBufferLength: 30,
      backBufferLength: 10,
      manifestLoadingMaxRetry: 6,
      levelLoadingMaxRetry: 6,
      fragLoadingMaxRetry: 6,
      lowLatencyMode: false
    });
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MEDIA_ATTACHED, startPlay);
    hls.on(Hls.Events.ERROR, function(_, data) {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        setTimeout(function(){ hls.startLoad(); }, 1000);
      } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        hls.recoverMediaError();
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = src;
    video.addEventListener('loadedmetadata', startPlay);
  }
})();
"""


@app.get("/live", response_class=HTMLResponse)
def live_player():
    """Clean, full-bleed video-only player. Use this URL in Chrome/Firefox/Edge."""
    return f"""
    <!doctype html>
    <html>
      <head>
        <title>Live Video</title>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <style>
          html, body {{ margin:0; padding:0; height:100%; background:#000; }}
          video {{ width:100%; height:100%; object-fit:contain; background:#000; display:block; }}
        </style>
      </head>
      <body>
        <video id="video" controls autoplay muted playsinline></video>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
        <script>{_HLS_PLAYER_SCRIPT}</script>
      </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <!doctype html>
    <html>
      <head>
        <title>Surveillance Live Feed</title>
        <style>
          body { background:#111; color:#eee; font-family: sans-serif; text-align:center; margin:0; padding:1rem; }
          video { max-width: 100%; height: auto; border: 2px solid #333; background:#000; }
          .row { margin: 1rem auto; max-width: 1000px; }
          .meta { font-size: 0.9rem; color: #aaa; }
          a { color: #6cf; }
        </style>
      </head>
      <body>
        <h2>Surveillance Live Feed</h2>
        <div class="row">
          <video id="video" controls autoplay muted playsinline></video>
        </div>
        <div class="row">
          <audio src="/live_audio/stream" controls autoplay></audio>
        </div>
        <div class="meta">
          video:
            <a href="/surveillance_video/playlist">playlist</a> &middot;
            <a href="/surveillance_video/current">current</a> &middot;
            <a href="/hls/stream.m3u8">.m3u8</a>
          &nbsp;|&nbsp;
          audio:
            <a href="/live_audio/playlist">playlist</a> &middot;
            <a href="/live_audio/current">current</a>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
        <script>
          (function() {
            var video = document.getElementById('video');
            var src = '/hls/stream.m3u8';
            function startPlay() { video.play().catch(function(){}); }
            if (window.Hls && Hls.isSupported()) {
              var hls = new Hls({
                liveSyncDuration: 10,
                liveMaxLatencyDuration: 30,
                maxBufferLength: 30,
                backBufferLength: 10,
                manifestLoadingMaxRetry: 6,
                levelLoadingMaxRetry: 6,
                fragLoadingMaxRetry: 6,
                lowLatencyMode: false
              });
              hls.loadSource(src);
              hls.attachMedia(video);
              hls.on(Hls.Events.MEDIA_ATTACHED, startPlay);
              hls.on(Hls.Events.ERROR, function(_, data) {
                if (!data.fatal) return;
                if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                  setTimeout(function(){ hls.startLoad(); }, 1000);
                } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                  hls.recoverMediaError();
                }
              });
            } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
              video.src = src;
              video.addEventListener('loadedmetadata', startPlay);
            }
          })();
        </script>
      </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn
    import os
    from dotenv import load_dotenv
    load_dotenv()
    port = os.getenv("PORT")
    uvicorn.run(app, host="0.0.0.0", port=port)
