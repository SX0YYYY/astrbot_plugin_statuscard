from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CpuFreq:
    current: float | None
    min: float | None
    max: float | None


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
    cleaned = [
        item for item in sections
        if isinstance(item, str) and item != "todo"
    ]
    return cleaned if cleaned != sections else None

