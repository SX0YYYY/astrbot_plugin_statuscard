from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass
class CpuFreq:
    current: float | None
    min: float | None
    max: float | None


def cfg_get(config: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path out of a nested configuration.

    AstrBot hands the plugin a nested structure, but the tests and the
    migration code sometimes pass a plain dictionary. Both are handled the
    same way here so every module can read the configuration the same.
    """

    current: Any = config
    for part in path.split("."):
        if current is None:
            return default
        if hasattr(current, "get") or isinstance(current, dict):
            try:
                current = current.get(part)
            except Exception:
                return default
        else:
            return default
    return default if current is None else current


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_ts() -> int:
    return int(time.time())


def readable_python_version() -> str:
    v = sys.version.split(" ")[0]
    return f"Python {v}"


def system_name() -> str:
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def is_supported_platform(name: object) -> bool:
    """Only QQ/OneBot (aiocqhttp) has the data the status card needs.

    An empty name is allowed so tests and internal callers without a platform
    context can still render a degraded card; a named non-aiocqhttp platform is
    rejected explicitly.
    """

    text = str(name or "").strip().lower()
    return not text or text == "aiocqhttp"


RETRY_DELAYS = (30.0, 120.0, 600.0)


def retry_delay(attempt: int) -> float:
    """Return a short retry delay for a failed delivery attempt."""

    index = max(1, min(int(attempt or 1), len(RETRY_DELAYS))) - 1
    return RETRY_DELAYS[index]


def clean_report_sections(sections: Any) -> list[str] | None:
    """Remove the retired ``todo`` module from a persisted section list.

    Returns ``None`` when nothing needs to change, so callers can avoid an
    unnecessary config write.
    """

    if not isinstance(sections, list):
        return None
    cleaned = [item for item in sections if isinstance(item, str) and item != "todo"]
    return cleaned if cleaned != sections else None


def redact_url(url: Any) -> str:
    """Strip credentials, query strings and fragments from a probe target.

    Monitor URLs may carry credentials either in the ``user:password@host``
    part or in ``?token=`` style parameters, so only the scheme, host, port
    and path are kept for ``report.db`` and reports. Non-URL text is returned
    unchanged.
    """

    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    if not parts.netloc:
        return text
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        # Keep IPv6 literals readable.
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}{parts.path}"


def truncate_text(text: Any, limit: int = 2000) -> str:
    """Clamp user-supplied text to a chat-safe length."""

    value = str(text or "")
    limit = max(1, int(limit))
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 12)].rstrip() + "\n…（已截断）"
