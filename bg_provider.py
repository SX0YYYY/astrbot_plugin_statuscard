from __future__ import annotations

import asyncio
import mimetypes
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

try:
    from astrbot.api import logger  # type: ignore
except Exception:  # pragma: no cover - fallback for local test env
    import logging

    logger = logging.getLogger("astrbot_plugin_picstatus")


ASSETS_PATH = Path(__file__).parent / "res" / "assets"
DEFAULT_BG_PATH = ASSETS_PATH / "default_bg.webp"
DEFAULT_TIMEOUT = 10


@dataclass
class BgBytesData:
    data: bytes
    mime: str


@dataclass(frozen=True)
class BackgroundRequest:
    """Background resolve request.

    - provider_chain: providers to try in order, e.g. ("lolicon", "local", "none")
    - local_path: file or directory used by "local" provider
    - timeout/proxy: used by online providers
    - preload_count: background preloading queue size
    - lolicon_r18_type: 0=normal, 1=R18, 2=mixed
    """

    provider_chain: tuple[str, ...]
    local_path: Path | None = None
    timeout: int = DEFAULT_TIMEOUT
    proxy: str | None = None
    preload_count: int = 1
    lolicon_r18_type: int = 0


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


def _guess_mime_from_path(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime:
        return mime
    suffix = path.suffix.lower()
    if suffix == ".webp":
        return "image/webp"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"


def _is_image_file(path: Path) -> bool:
    if not path.is_file():
        return False
    mime, _ = mimetypes.guess_type(path.name)
    return bool(mime and mime.startswith("image/"))


def _create_async_client(
    *, timeout: int, proxy: str | None, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    base_kwargs = {
        "follow_redirects": True,
        "timeout": timeout,
        "headers": headers,
    }
    if proxy:
        try:
            return httpx.AsyncClient(proxy=proxy, **base_kwargs)
        except TypeError:
            return httpx.AsyncClient(proxies=proxy, **base_kwargs)  # type: ignore[arg-type]
    return httpx.AsyncClient(**base_kwargs)


async def _fetch_loli(req: BackgroundRequest) -> Optional[BgBytesData]:
    """Fetch one background from loliapi.

    API: https://www.loliapi.com/acg/pe/
    """
    url = "https://www.loliapi.com/acg/pe/"
    try:
        async with _create_async_client(timeout=req.timeout, proxy=req.proxy) as cli:
            resp = await cli.get(url)
            resp.raise_for_status()
            content = resp.content
            return BgBytesData(
                data=content,
                mime=resp.headers.get("Content-Type") or _detect_image_mime(content),
            )
    except Exception as e:
        logger.warning(f"fetch_loli failed: {e.__class__.__name__}: {e}")
        return None


async def _fetch_lolicon(req: BackgroundRequest) -> Optional[BgBytesData]:
    """Fetch one background from Lolicon (Pixiv). r18_type: 0=off, 1=R18, 2=mixed."""
    try:
        r18_type = max(0, min(2, int(req.lolicon_r18_type)))
    except Exception:
        r18_type = 0

    try:
        async with _create_async_client(
            timeout=req.timeout,
            proxy=req.proxy,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/119.0.0.0 Safari/537.36"
                ),
            },
        ) as cli:
            resp = await cli.get(
                "https://api.lolicon.app/setu/v2",
                params={
                    "num": 1,
                    "r18": r18_type,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            payload = data.get("data") or []
            if not payload:
                return None
            url = (payload[0].get("urls") or {}).get("original")
            if not url:
                return None

            img_resp = await cli.get(url, headers={"Referer": "https://www.pixiv.net/"})
            img_resp.raise_for_status()
            content = img_resp.content
            return BgBytesData(
                data=content,
                mime=img_resp.headers.get("Content-Type")
                or _detect_image_mime(content),
            )
    except Exception as e:
        logger.warning(f"fetch_lolicon failed: {e.__class__.__name__}: {e}")
        return None


def _read_local(path: Path | None = None) -> Optional[BgBytesData]:
    p = path or DEFAULT_BG_PATH
    try:
        target = p
        if target.is_dir():
            candidates = [x for x in target.glob("*") if _is_image_file(x)]
            target = random.choice(candidates) if candidates else DEFAULT_BG_PATH

        data = target.read_bytes()
        mime = _guess_mime_from_path(target)
        if mime == "application/octet-stream":
            mime = _detect_image_mime(data)
        return BgBytesData(data=data, mime=mime)
    except Exception as e:
        logger.warning(f"read_local failed: {e.__class__.__name__}: {e}")
        return None


async def _fetch_local(req: BackgroundRequest) -> Optional[BgBytesData]:
    return _read_local(req.local_path)


async def _fetch_none(req: BackgroundRequest) -> Optional[BgBytesData]:
    return _read_local(DEFAULT_BG_PATH)


BgProvider = Callable[[BackgroundRequest], Awaitable[Optional[BgBytesData]]]


_PROVIDERS: dict[str, BgProvider] = {
    "loli": _fetch_loli,
    "lolicon": _fetch_lolicon,
    "local": _fetch_local,
    "none": _fetch_none,
    "default": _fetch_none,
}


async def _fetch_loli_and_lolicon(req: BackgroundRequest) -> Optional[BgBytesData]:
    """Randomly fetch from loliapi or lolicon, with a best-effort fallback."""
    providers: list[BgProvider] = [_fetch_loli, _fetch_lolicon]
    random.shuffle(providers)
    for provider in providers:
        bg = await provider(req)
        if bg:
            return bg
    return None


_PROVIDERS["loli&lolicon"] = _fetch_loli_and_lolicon


def _normalize_provider(name: str) -> str:
    n = (name or "").strip().lower()
    if n in {"builtin", "built-in", "internal"}:
        return "none"
    return n


class BgPreloader:
    def __init__(self, request: BackgroundRequest):
        self.request = request
        self.queue: asyncio.Queue[BgBytesData] = asyncio.Queue()
        self._preload_task: asyncio.Task | None = None

    async def _fetch_once(self) -> BgBytesData:
        for raw in self.request.provider_chain:
            name = _normalize_provider(raw)
            if not name:
                continue
            provider = _PROVIDERS.get(name)
            if not provider:
                logger.warning(f"Unknown bg provider: {name}")
                continue
            try:
                bg = await provider(self.request)
            except Exception:
                logger.exception(f"bg provider {name} failed")
                bg = None
            if bg:
                return bg

        bg = _read_local(DEFAULT_BG_PATH)
        assert bg, "Default background missing"
        return bg

    async def _fill_queue(self):
        try:
            preload_count = max(1, int(self.request.preload_count))
        except Exception:
            preload_count = 1
        try:
            while self.queue.qsize() < preload_count:
                bg = await self._fetch_once()
                await self.queue.put(bg)
        except Exception:
            logger.exception("BgPreloader fill_queue failed")
        finally:
            self._preload_task = None

    def _ensure_preload(self):
        if self._preload_task and not self._preload_task.done():
            return
        self._preload_task = asyncio.create_task(self._fill_queue())

    async def get(self) -> BgBytesData:
        self._ensure_preload()
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return await self._fetch_once()


async def resolve_background(
    prefer_bytes: bytes | None = None,
    request: BackgroundRequest | None = None,
) -> BgBytesData:
    if prefer_bytes:
        return BgBytesData(prefer_bytes, _detect_image_mime(prefer_bytes))

    req = request or BackgroundRequest(
        provider_chain=("loli",), timeout=DEFAULT_TIMEOUT
    )
    preloader = _get_preloader(req)
    return await preloader.get()


_cached_preloader: BgPreloader | None = None
_cached_key: tuple | None = None


def _get_preloader(request: BackgroundRequest) -> BgPreloader:
    global _cached_preloader, _cached_key
    key = (
        tuple(_normalize_provider(x) for x in request.provider_chain),
        str(request.local_path) if request.local_path else "",
        int(request.timeout),
        request.proxy or "",
        int(request.preload_count),
        int(request.lolicon_r18_type),
    )
    if _cached_preloader is None or _cached_key != key:
        _cached_preloader = BgPreloader(request=request)
        _cached_key = key
    return _cached_preloader
