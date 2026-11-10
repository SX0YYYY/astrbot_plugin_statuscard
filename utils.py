from __future__ import annotations

import math
import platform
import re
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


def delivery_offline(result: Any) -> bool:
    """True when a send failed because the transport itself was unreachable.

    ``DeliveryResult.transport_offline`` is the authoritative signal (the
    delivery layer knows whether the failure was ApiNotAvailable / no
    platform / invalid URL). The alert and monitor engines only looked at
    their own offline latch, so a failure that arrived before the latch was
    set still burned the retry budget (v2.20.8).
    """

    return bool(getattr(result, "transport_offline", False))


# Offline failures never consume the retry budget, so a fixed ladder keeps
# retrying forever. The first few attempts stay at 15 minutes (a short
# reconnect is picked up quickly), then the delay backs off to one hour: a
# target that the direct path keeps rejecting while the framework returns
# False produced an attempt every 15 minutes with no end and no disclosure.
OFFLINE_RETRY_BASE_SECONDS = 900.0
OFFLINE_RETRY_SLOW_SECONDS = 3600.0
OFFLINE_RETRY_FAST_ATTEMPTS = 4


def offline_retry_delay(attempt: int) -> float:
    """Delay before the next retry after a transport-offline failure.

    ``attempt`` is the 1-based count of consecutive offline failures for the
    queued item. The queued notice itself is still delivered when the
    connection returns; this only throttles the polling.
    """

    return (
        OFFLINE_RETRY_BASE_SECONDS
        if attempt <= OFFLINE_RETRY_FAST_ATTEMPTS
        else OFFLINE_RETRY_SLOW_SECONDS
    )


def summary_item_parts(item: Any) -> tuple[str, str, str]:
    """Normalise one aggregation item to ``(label, reason, key)``.

    The aggregator stores 2-tuples for legacy callers and 3-tuples when the
    engine supplies the owning rule/monitor key. A summary callback uses the
    key to tell two same-named items apart; older persisted/test batches have
    no key and fall back to the label+reason match (v2.20.19).
    """

    try:
        label = str(item[0] or "")
        reason = str(item[1] or "")
        key = str(item[2] or "") if len(item) >= 3 else ""
    except (TypeError, IndexError):
        return "", "", ""
    return label, reason, key


def safe_int(value: Any, default: int = 0) -> int:
    """Convert ``value`` to int, falling back instead of raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
    except OverflowError:
        # int(float("inf")) raises OverflowError, not ValueError: a state
        # file holding Infinity used to crash the caller that trusted
        # safe_int never to raise.
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
        # Only text that looks like a URL may be rewritten. Monitor details
        # such as "未找到匹配进程: python #1" or "python?" used to lose
        # everything after the #/? even though they are not URLs (2026-11-06
        # audit). The dotted-host branch below still covers a bare
        # "example.com/api?token=abc".
        head = text.split("#", 1)[0].split("?", 1)[0]
        looks_like_url = bool(
            "://" in text
            or text.startswith("//")
            # The trailing "[?#]" matters: "example.com?token=x" and
            # "internal:8080?token=x" have no path, and without it the query
            # (and its token) survived into state keys, report.db, the
            # dashboard and audit logs (2026-11-06 reverse audit).
            or re.match(r"^[A-Za-z0-9_.\-]+:\d{1,5}(?:/|$|[?#])", text)
            or re.match(
                r"^[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+(?::\d{1,5})?(?:/|$|[?#])",
                text,
            )
            # "user[:pass]@host/path" has no scheme either; the part before
            # "@" is userinfo only when it contains no slash.
            or ("@" in head and "/" not in head.split("@", 1)[0])
            # A bare single-label host with a query/fragment parameter
            # ("host#access_token=x", "host?token=x") is still a URL. Free
            # text such as "python #1" has no "=" after the marker, so it is
            # not rewritten.
            or (
                ("?" in text or "#" in text)
                and "=" in text.split("?", 1)[-1].split("#", 1)[-1]
            )
        )
        if not looks_like_url:
            return text
        # A bare "example.com/api?token=abc" has no scheme, so urlsplit puts
        # everything in path and the query survives. Strip it explicitly --
        # and credentials too: "user[:pass]@host/api" is a legal (if
        # unusual) monitor target, and the userinfo used to survive.
        bare = head
        if "@" in bare:
            userinfo, _, tail = bare.rpartition("@")
            # Credentials only when the part before "@" looks like userinfo
            # (no slashes); otherwise the "@" is just part of the path. A
            # "user:" prefix is indistinguishable from a scheme here, but a
            # scheme-less URL with a netloc-less "@" is a userinfo in
            # practice -- and over-stripping a hypothetical mailto: probe
            # only mangles the display, it never leaks.
            if userinfo and "/" not in userinfo:
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
    if not parts.scheme:
        # A scheme-relative URL ("//host/path"): it has a netloc but no
        # scheme, so the generic assembly produced the broken "://host/path".
        return f"//{host}{parts.path}"
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


def mask_chat_ids(text: str) -> str:
    """Mask bare chat ids inside a free-form error message.

    A receipt error can quote only the session id ("会话 ID 不是数字:
    461961169abc") or embed it in an exception message, so replacing the
    full UMO string did not hide it and the dashboard showed the complete
    group id next to a masked target field.
    """

    import re

    def _mask(match: re.Match[str]) -> str:
        digits = match.group(0)
        keep = min(2, len(digits) - 1)
        return "*" * (len(digits) - keep) + digits[len(digits) - keep :]

    # 5 digits too: early QQ accounts (10000-99999) are real chat ids and
    # slipped through the old 6-digit floor (v2.20.32). This is a redaction
    # helper -- over-masking a bare 5-digit number in an error string is
    # cheaper than leaking an id.
    return re.sub(r"\d{5,}", _mask, str(text or ""))
