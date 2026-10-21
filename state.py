from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from astrbot.api import logger

SCHEMA_VERSION = 1


class StateStore:
    """Small JSON state store with atomic writes.

    The state file lives in AstrBot's plugin_data directory, never in the
    plugin source tree, so plugin updates cannot overwrite collected history.

    The store is touched from two kinds of threads: the event loop (the
    sampler, the alert engine and the monitor) and worker threads started by
    ``asyncio.to_thread`` (the system-info collector). Both mutate the same
    dictionary, so every read-modify-write cycle is guarded by a lock and
    the serialisation works on a snapshot instead of the live mapping.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        existed = self.path.is_file()
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("state root is not an object")
            with self._lock:
                self.data = parsed
        except FileNotFoundError:
            with self._lock:
                self.data = {}
        except Exception as exc:
            logger.warning("读取状态文件失败 %s: %s", self.path, exc)
            self._backup_bad_file()
            with self._lock:
                self.data = {}
        self._migrate(existed)

    def _migrate(self, existed: bool) -> None:
        with self._lock:
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
        """Persist the state atomically.

        The mapping is copied under the lock and the copy is serialised
        afterwards, so a worker thread that mutates the live mapping
        directly cannot make the serialisation fail. The temporary file name
        carries the process id so two concurrent saves never fight over the
        same file.
        """

        try:
            with self._lock:
                self.data.setdefault("schema_version", SCHEMA_VERSION)
                snapshot = copy.deepcopy(self.data)
            payload = json.dumps(snapshot, ensure_ascii=False, indent=1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
        except Exception as exc:
            logger.warning("写入状态文件失败 %s: %s", self.path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def setdefault(self, key: str, default: Any) -> Any:
        with self._lock:
            return self.data.setdefault(key, default)

    def set(self, key: str, value: Any) -> None:
        """Assign a top-level key under the lock."""

        with self._lock:
            self.data[key] = value

    @contextmanager
    def section(self, key: str, default: Any = None) -> Iterator[dict[str, Any]]:
        """Yield a nested section for in-place mutation under the lock.

        The returned mapping is the live object, so callers may keep working
        on it after the block; only the mutation itself is serialised. This
        keeps the event loop and the worker threads from interleaving their
        read-modify-write cycles on the same nested structure.
        """

        with self._lock:
            section = self.data.setdefault(key, default if default is not None else {})
            if not isinstance(section, dict):
                section = {}
                self.data[key] = section
            yield section
