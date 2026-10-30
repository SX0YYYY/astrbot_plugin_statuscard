from __future__ import annotations

import math
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger


@dataclass
class CpuFreq:
    current: float | None
    min: float | None
    max: float | None


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert ``value`` to float, falling back instead of raising.

    ``state.json`` is hand-editable and older versions wrote different types,
    so a single bad scalar must not make the alert/monitor loops raise on
    every sample (which silently disabled the whole engine).
    """

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Convert ``value`` to int, falling back instead of raising."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def clamp_config(value: float, low: float, high: float, label: str):
    """Clamp a configured number to the range the settings page can express.

    A hand-edited JSON can hold values outside the slider range; the clamped
    value still takes effect, but a warning says so once instead of letting
    runtime behaviour silently diverge from what the page shows.
    """

    # A hand-edited JSON may contain NaN/Infinity (Python's json accepts
    # them); max/min would silently pin NaN to the upper bound, which also
    # dodged the audit's zero-check. Clamp to low and warn (v2.20.0).
    if isinstance(value, float) and math.isnan(value):
        logger.warning(
            "%s=%s 不是有效数字（NaN/Infinity），已按 %s 生效",
            label,
            value,
            low,
        )
        return low
    clamped = max(low, min(high, value))
    if clamped != value:
        logger.warning(
            "%s=%s 超出配置页范围 %s–%s，已按 %s 生效",
            label,
            value,
            low,
            high,
            clamped,
        )
    return clamped


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
        # A bare "example.com/api?token=abc" has no scheme, so urlsplit puts
        # everything in path and the query survives. Strip it explicitly --
        # and credentials too: "user[:pass]@host/api" is a legal (if
        # unusual) monitor target, and the userinfo used to survive.
        bare = text.split("#", 1)[0].split("?", 1)[0]
        if "@" in bare:
            head, _, tail = bare.rpartition("@")
            # Credentials only when the part before "@" looks like userinfo
            # (no slashes); otherwise the "@" is just part of the path. A
            # "user:" prefix is indistinguishable from a scheme here, but a
            # scheme-less URL with a netloc-less "@" is a userinfo in
            # practice -- and over-stripping a hypothetical mailto: probe
            # only mangles the display, it never leaks.
            if head and "/" not in head:
                return tail
        return bare
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


def normalize_targets(raw: Any, platform_id: str) -> list[str]:
    """Normalize push targets: plain group numbers become full UMOs.

    Alerts, service monitoring and the reports all accept either a full
    unified message origin or a bare QQ group number; both resolve to the
    same UMO here so every push path shares one definition.
    """

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value:
            continue
        target = value if ":" in value else f"{platform_id}:GroupMessage:{value}"
        if target not in out:
            out.append(target)
    return out


def mask_umo(target: object) -> str:
    """Hide the middle of a chat id inside a UMO.

    default:GroupMessage:461961169 becomes
    default:GroupMessage:*******69: the value still tells two targets
    apart, but no full chat id is exposed -- shared by the dashboard API
    (verify results), the status-card diagnostic command and anywhere else
    a UMO could reach a human reader.
    """

    text = str(target or "")
    head, _, tail = text.rpartition(":")
    digits = "".join(ch for ch in tail if ch.isdigit())
    # Short IDs used to pass through untouched, which leaked the UMO the
    # callers promise never to expose; mask anything digit-bearing.
    if not digits:
        return text
    # Keep the last two digits: enough to tell two targets apart, never
    # enough to identify an account (old behaviour kept four for long IDs
    # and none for very short ones).
    keep = min(2, len(digits) - 1)
    masked = "*" * (len(digits) - keep) + digits[len(digits) - keep :]
    return f"{head}:{masked}" if head else masked
