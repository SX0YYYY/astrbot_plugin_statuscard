from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path


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

