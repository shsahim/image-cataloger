"""Video probing and frame sampling through ffmpeg.

ffmpeg is looked up on PATH (and the usual Homebrew locations) first; otherwise
the static binary bundled with the `imageio-ffmpeg` wheel is used, so the app
works without a system install.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from .imaging import PreparedImage, UnreadableImage, prepare_image_bytes

DEFAULT_FRAMES = 5


class VideoError(Exception):
    """The file could not be probed or no frame could be extracted."""


@dataclass(frozen=True)
class VideoInfo:
    width: int | None
    height: int | None
    duration: float | None


@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(candidate):
            return candidate
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_STREAM = re.compile(r"Stream #\d+:\d+.*?: Video:.*?\b(\d{2,5})x(\d{2,5})\b")


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    exe = find_ffmpeg()
    if not exe:
        raise VideoError("ffmpeg not found (install ffmpeg or `pip install imageio-ffmpeg`)")
    try:
        return subprocess.run([exe, "-hide_banner", "-nostdin", *args], capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise VideoError(f"ffmpeg timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise VideoError(f"cannot run ffmpeg: {e}") from e


def probe_video(path: str | os.PathLike, timeout: float = 60.0) -> VideoInfo:
    """Read duration and frame size from the container header (`ffmpeg -i`)."""
    proc = _run(["-i", str(path)], timeout)
    text = proc.stderr.decode("utf-8", "replace")
    if "Invalid data found" in text or "No such file" in text or "moov atom not found" in text:
        raise VideoError(text.strip().splitlines()[-1][:200] if text.strip() else "unreadable video")
    duration = None
    m = _DURATION.search(text)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    width = height = None
    m = _VIDEO_STREAM.search(text)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
    if width is None and duration is None:
        raise VideoError("no video stream found")
    return VideoInfo(width, height, duration)


def sample_frames(
    path: str | os.PathLike,
    count: int = DEFAULT_FRAMES,
    max_side: int = 1568,
    quality: int = 85,
    duration: float | None = None,
    timeout: float = 60.0,
) -> list[PreparedImage]:
    """Extract *count* frames spread evenly through the clip, as prepared JPEGs.

    Each frame is seeked with `-ss` before `-i` (fast, keyframe-accurate), scaled
    so its long side is at most *max_side*, and encoded in memory. Frames that
    cannot be read (seek past the end of a mis-reported duration) are skipped;
    VideoError is raised only when nothing at all could be extracted.
    """
    count = max(1, count)
    if duration is None:
        duration = probe_video(path, timeout).duration
    if not duration or duration <= 0:
        times = [0.0]
    else:
        times = [duration * (i + 0.5) / count for i in range(count)]
    frames: list[PreparedImage] = []
    last_error = ""
    for t in times:
        proc = _run(
            ["-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-an",
             "-vf", f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-"],
            timeout,
        )
        if proc.returncode != 0 or not proc.stdout:
            last_error = proc.stderr.decode("utf-8", "replace").strip()[-200:]
            continue
        try:
            frames.append(prepare_image_bytes(proc.stdout, max_side, quality))
        except UnreadableImage as e:
            last_error = str(e)
    if not frames:
        raise VideoError(f"no frames could be extracted: {last_error or 'unknown error'}")
    return frames
