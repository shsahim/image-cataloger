"""Image decoding, validation and downscaling for the API engine."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

try:  # HEIC/HEIF support is optional
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - depends on the environment
    HEIF_SUPPORTED = False

# Claude accepts images up to 1568 px on the long side without downscaling
# them server-side; anything bigger costs no extra accuracy.
DEFAULT_MAX_SIDE = 1568
DEFAULT_JPEG_QUALITY = 85

# Pillow refuses to open absurdly large images by default (decompression-bomb guard).
# Recovered photos can legitimately be huge, so raise the ceiling but keep one.
Image.MAX_IMAGE_PIXELS = 400_000_000


class UnreadableImage(Exception):
    """Raised when a file cannot be decoded as an image."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    media_type: str
    width: int  # dimensions of the encoded (possibly downscaled) image
    height: int
    source_width: int
    source_height: int

    @property
    def estimated_tokens(self) -> int:
        return estimate_image_tokens(self.width, self.height)


def estimate_image_tokens(width: int, height: int) -> int:
    """Approximate Claude vision token cost: (w * h) / 750, after downscaling."""
    return max(1, int(width * height / 750))


def read_dimensions(path: str | os.PathLike) -> tuple[int, int]:
    """Read width/height from the header only. Raises UnreadableImage."""
    try:
        with Image.open(path) as im:
            return im.width, im.height
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        raise UnreadableImage(f"{type(e).__name__}: {e}") from e


def prepare_image(
    path: str | os.PathLike,
    max_side: int = DEFAULT_MAX_SIDE,
    quality: int = DEFAULT_JPEG_QUALITY,
) -> PreparedImage:
    """Decode, apply EXIF orientation, downscale to *max_side* and re-encode as JPEG.

    Everything happens in memory; the source file is never modified. Animated
    formats contribute their first frame. Transparency is flattened onto white.
    """
    try:
        with Image.open(path) as im:
            return _encode(im, max_side, quality)
    except UnreadableImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        raise UnreadableImage(f"{type(e).__name__}: {e}") from e


def prepare_image_bytes(data: bytes, max_side: int = DEFAULT_MAX_SIDE,
                        quality: int = DEFAULT_JPEG_QUALITY) -> PreparedImage:
    """Same as prepare_image, for an encoded image already in memory (e.g. a video frame)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return _encode(im, max_side, quality)
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
        raise UnreadableImage(f"{type(e).__name__}: {e}") from e


def _encode(im: Image.Image, max_side: int, quality: int) -> PreparedImage:
    im.load()  # force full decode so truncated files fail here, not later
    source_w, source_h = im.width, im.height
    im = ImageOps.exif_transpose(im) or im
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        im = background
    elif im.mode != "RGB":
        im = im.convert("RGB")
    longest = max(im.width, im.height)
    if longest > max_side:
        scale = max_side / longest
        new_size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
        im = im.resize(new_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return PreparedImage(
        data=buf.getvalue(),
        media_type="image/jpeg",
        width=im.width,
        height=im.height,
        source_width=source_w,
        source_height=source_h,
    )
