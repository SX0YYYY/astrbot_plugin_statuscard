"""Version helpers.

Two different versions matter to the plugin: its own version, and the
version of AstrBot it is running on. The first is a constant, the second
has to be probed because the running AstrBot does not expose it in a
single documented place.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from importlib import metadata
from inspect import isawaitable
from typing import Any

from astrbot.api import logger

# The version of this plugin. Keep it in sync with ``metadata.yaml``.
__version__ = "2.18.6"

# Probing imports the package and consults its metadata, and it is called
# from several render paths. A short cache keeps a burst of status cards or
# a dashboard refresh from redoing the same import every time.
_VERSION_CACHE_TTL = 300.0
_version_cache: tuple[float, str, str] | None = None


def _normalize_version(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.startswith("v"):
        return cleaned
    return f"v{cleaned}"


def _resolve_from_context(context: Any) -> str | None:
    if context is None:
        return None

    method_names = (
        "get_astrbot_version",
        "get_core_version",
        "get_version",
    )
    attr_names = (
        "astrbot_version",
        "core_version",
        "version",
    )

    for name in method_names:
        getter = getattr(context, name, None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception as exc:
            logger.debug("读取 AstrBot 版本属性 %s 失败: %s", name, exc)
            continue
        if isawaitable(value):
            continue
        normalized = _normalize_version(value)
        if normalized:
            return normalized

    for name in attr_names:
        try:
            value = getattr(context, name)
        except Exception as exc:
            logger.debug("读取 AstrBot 版本属性 %s 失败: %s", name, exc)
            continue
        normalized = _normalize_version(value)
        if normalized:
            return normalized
    return None


def _resolve_from_astrbot_module() -> str | None:
    try:
        import astrbot
    except Exception:
        return None
    return _normalize_version(getattr(astrbot, "__version__", None))


def _resolve_from_package_metadata() -> str | None:
    try:
        return _normalize_version(metadata.version("astrbot"))
    except Exception:
        return None


def resolve_astrbot_version(context: Any = None, default: str = "vUnknown") -> str:
    """Return the version of the running AstrBot, for display on the card.

    The version is probed from the context first, then from the imported
    package, and finally from the installed package metadata, so it works
    across the AstrBot versions the plugin supports. The result is cached
    for a few minutes because the probes import modules.
    """

    global _version_cache
    now = time.monotonic()
    if (
        _version_cache is not None
        and _version_cache[1] == default
        and now - _version_cache[0] < _VERSION_CACHE_TTL
    ):
        return _version_cache[2]
    probes: tuple[Callable[[], str | None], ...] = (
        lambda: _resolve_from_context(context),
        _resolve_from_astrbot_module,
        _resolve_from_package_metadata,
    )
    for probe in probes:
        version = probe()
        if version:
            _version_cache = (now, default, version)
            return version
    normalized_default = _normalize_version(default)
    result = normalized_default or "vUnknown"
    _version_cache = (now, default, result)
    return result
