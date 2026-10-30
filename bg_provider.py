from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

ASSETS_PATH = Path(__file__).parent / "res" / "assets"
DEFAULT_BG_PATH = ASSETS_PATH / "default_bg.webp"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# A 10 MB file can still decode into a bitmap several hundred MB wide
# (highly compressible solid-colour PNGs). Cap the pixel count before
# Pillow ever decodes the full image; anything above this falls back to
# the built-in background instead of risking an OOM on a 2 GB process.
MAX_IMAGE_PIXELS = 60_000_000


@dataclass
class BgBytesData:
    data: bytes
    mime: str


def _flatten_to_rgb(image):
    """Return an RGB view of *image* honouring EXIF orientation.

    Animated GIF/WebP inputs land on their first frame (Pillow behaviour),
    which is what a background needs. Partially transparent images are
    composited onto white so the average colour matches what a browser
    would actually paint.
    """

    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        base = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(base, image.convert("RGBA"))
    return image.convert("RGB")


def shrink_image_bytes(
    data: bytes, target_width: int = 1170, quality: int = 85
) -> tuple[bytes, str]:
    """Downscale and re-encode an uploaded background before embedding.

    The rendered HTML is shipped to the remote T2I service as one JSON
    POST, so an uncompressed multi-megabyte photo turns the request body
    into several MB. Downscale to the card's pixel width and re-encode as
    JPEG: typical photos land near 0.3 MB, and the retry ladder below
    (q85 -> 75 -> 60) keeps even pathological inputs at or under ~1.2 MB.
    (2026-10-31 probing: the remote service renders even a 3 MB data URL
    fine -- size was never the cause of the "washed out" look; that was
    the white mask plus cover scaling, both since removed. Shrink anyway:
    smaller body, faster upload.) GIF/WebP animations flatten to their
    first frame, which is what a background needs anyway. Raises
    ValueError for images beyond MAX_IMAGE_PIXELS so callers can fall
    back to the built-in background instead of decoding hundreds of MB.
    """

    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as probe:
        if probe.width * probe.height > MAX_IMAGE_PIXELS:
            raise ValueError(
                f"图片像素 {probe.width}x{probe.height} 超过上限 "
                f"{MAX_IMAGE_PIXELS}，已回退内置背景"
            )

    with Image.open(BytesIO(data)) as image:
        image = _flatten_to_rgb(image)
        width, height = image.size
        if width > target_width:
            image = image.resize(
                (target_width, max(1, round(height * target_width / width))),
                Image.LANCZOS,
            )
        for attempt_quality in (quality, 75, 60):
            out = BytesIO()
            image.save(out, "JPEG", quality=attempt_quality, optimize=True)
            encoded = out.getvalue()
            if len(encoded) <= 1_200_000:
                break
    return encoded, "image/jpeg"


def fade_color_from_bytes(data: bytes) -> tuple[int, int, int] | None:
    """Average colour of a (usually already shrunk) background image.

    Used as the underlay the card fades into below the background photo
    (v2.19.4): the photo shows once at full width, then dissolves into
    its own dominant tone instead of repeating down the page. Returns
    None when the image cannot be decoded; callers apply a neutral
    fallback. Transparent areas composite onto white, matching what the
    browser paints.
    """

    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            pixel = _flatten_to_rgb(image).resize((1, 1)).getpixel((0, 0))
        return int(pixel[0]), int(pixel[1]), int(pixel[2])
    except Exception:
        return None


def _detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    # Unknown magic: let Pillow identify the format instead of handing the
    # browser an application/octet-stream payload it refuses to render.
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            fmt = (image.format or "").lower()
        if fmt:
            # Pillow reports "JPEG" (never "jpg"), so the plain f-string
            # already yields image/jpeg.
            return f"image/{fmt}"
    except Exception:
        pass
    return "application/octet-stream"


def _read_default_background() -> BgBytesData | None:
    try:
        if not DEFAULT_BG_PATH.is_file():
            logger.error("内置背景缺失: %s", DEFAULT_BG_PATH)
            return None
        if DEFAULT_BG_PATH.stat().st_size > MAX_IMAGE_BYTES:
            logger.error("内置背景超过 10 MB: %s", DEFAULT_BG_PATH)
            return None
        data = DEFAULT_BG_PATH.read_bytes()
        return BgBytesData(data=data, mime=_detect_image_mime(data))
    except Exception as exc:
        logger.warning("读取内置背景失败: %s", exc)
        return None


async def resolve_background(prefer_bytes: bytes | None = None) -> BgBytesData:
    """Return the uploaded background when present, otherwise the built-in one."""

    if prefer_bytes:
        return BgBytesData(prefer_bytes, _detect_image_mime(prefer_bytes))

    background = _read_default_background()
    if background is None:
        raise RuntimeError("内置背景读取失败")
    return background
