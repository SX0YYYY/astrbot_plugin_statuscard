from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot_plugin_statuscard")

SCHEMA_VERSION = 1


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
        existed = self.path.is_file()
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("state root is not an object")
            self.data = parsed
        except FileNotFoundError:
            self.data = {}
        except Exception as exc:
            logger.warning("读取状态文件失败 %s: %s", self.path, exc)
            self._backup_bad_file()
            self.data = {}
        self._migrate(existed)

    def _migrate(self, existed: bool) -> None:
        if not isinstance(self.data, dict):
            self.data = {}
        if self.data.get("schema_version") == SCHEMA_VERSION:
            return
        self.data["schema_version"] = SCHEMA_VERSION
        if existed:
            self.save()

    def _backup_bad_file(self) -> None:
        try:
            suffix = time.strftime("%Y%m%d_%H%M%S")
            target = self.path.with_name(f"{self.path.name}.bad.{suffix}")
            self.path.replace(target)
            logger.warning("损坏的状态文件已备份到 %s", target)
        except Exception as exc:
            logger.warning("备份损坏状态文件失败: %s", exc)

    def save(self) -> None:
        try:
            self.data.setdefault("schema_version", SCHEMA_VERSION)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("写入状态文件失败 %s: %s", self.path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        return self.data.setdefault(key, default)
