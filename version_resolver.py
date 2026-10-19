from __future__ import annotations

from importlib import metadata
from inspect import isawaitable
from typing import Any, Callable


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
        except Exception:
            continue
        if isawaitable(value):
            continue
        normalized = _normalize_version(value)
        if normalized:
            return normalized

    for name in attr_names:
        try:
            value = getattr(context, name)
        except Exception:
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
    probes: tuple[Callable[[], str | None], ...] = (
        lambda: _resolve_from_context(context),
        _resolve_from_astrbot_module,
        _resolve_from_package_metadata,
    )
    for probe in probes:
        version = probe()
        if version:
            return version
    normalized_default = _normalize_version(default)
    return normalized_default or "vUnknown"
