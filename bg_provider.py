from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover - fallback for local test env
    logger = logging.getLogger("astrbot_plugin_statuscard")


ASSETS_PATH = Path(__file__).parent / "res" / "assets"
DEFAULT_BG_PATH = ASSETS_PATH / "default_bg.webp"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@dataclass
class BgBytesData:
    data: bytes
    mime: str


def _detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


def _read_default_background() -> Optional[BgBytesData]:
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
