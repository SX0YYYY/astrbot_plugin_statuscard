from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot_plugin_statuscard")


class StateStore:
    """Small JSON state store with atomic writes.

    The state file lives in AstrBot's plugin_data directory, never in the
    plugin source tree, so plugin updates cannot overwrite collected history.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                self.data = parsed
        except FileNotFoundError:
            self.data = {}
        except Exception as exc:
            logger.warning("读取状态文件失败 %s: %s", self.path, exc)
            self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("写入状态文件失败 %s: %s", self.path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        return self.data.setdefault(key, default)
