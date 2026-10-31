from __future__ import annotations

import copy
import json
import os
import shutil
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
        self._save_lock = threading.Lock()
        self.load()

    def load(self) -> None:
        existed = self.path.is_file()
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise TypeError("state root is not an object")
            with self._lock:
                self.data = parsed
        except FileNotFoundError:
            # The main file can be deleted by an external cleaner while the
            # previous good copy is still on disk; restoring it keeps the
            # month counters, latches and catch-up markers instead of
            # silently starting from zero.
            if not self._restore_from_backup():
                with self._lock:
                    self.data = {}
        except Exception as exc:
            logger.warning("读取状态文件失败 %s: %s", self.path, exc)
            restored = self._restore_from_backup()
            if not restored:
                self._backup_bad_file()
                with self._lock:
                    self.data = {}
        self._migrate(existed)

    def _restore_from_backup(self) -> bool:
        """Try the previous good copy before falling back to an empty state.

        A truncated write used to wipe the network history, the month
        counters, every monitor latch and the report catch-up markers in one
        go; the previous file is almost always still intact.
        """

        backup = self.path.with_name(self.path.name + ".bak")
        try:
            if not backup.is_file():
                return False
            parsed = json.loads(backup.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                return False
            with self._lock:
                self.data = parsed
            logger.warning("状态文件损坏，已从备份恢复: %s", backup)
            # Rewrite state.json immediately. Leaving the broken file in place
            # made the next save() copy it over .bak, so a second corruption
            # lost the whole state despite the backup.
            try:
                self._write_payload(json.dumps(parsed, ensure_ascii=False, indent=1))
            except Exception as exc:
                logger.warning("回写恢复后的状态文件失败: %s", exc)
            return True
        except Exception:
            return False

    @staticmethod
    def _is_readable_state(path: Path) -> bool:
        """True when ``path`` currently holds a JSON object."""

        try:
            return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
        except Exception:
            return False

    def _write_payload(self, payload: str) -> None:
        """Write ``payload`` to the state file atomically.

        fsync before the rename: os.replace only guarantees that a reader
        sees the old or the new file, not that the bytes reached the disk. A
        power loss could otherwise leave a truncated JSON, and the previous
        copy is what load() needs.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(payload, encoding="utf-8")
        try:
            # Restrict the temporary file before it becomes state.json: after
            # the rename the old code had a short window where the file kept
            # the default umask permissions.
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        try:
            with open(tmp, "rb+") as handle:
                os.fsync(handle.fileno())
        except Exception:
            pass
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

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
        carries the process and thread id so two concurrent saves from the
        same process never fight over the same file.

        Some callers still mutate nested sections across an ``await`` (the
        alert and monitor state machines do this while a message is being
        sent). Such a mutation can change a dict's size while ``deepcopy`` is
        iterating it; a single failed snapshot used to drop the whole save.
        Retrying the snapshot a few times makes that window harmless without
        changing every call site.
        """

        try:
            with self._save_lock:
                with self._lock:
                    self.data.setdefault("schema_version", SCHEMA_VERSION)
                    snapshot = None
                    for attempt in range(3):
                        try:
                            snapshot = copy.deepcopy(self.data)
                            break
                        except RuntimeError:
                            if attempt == 2:
                                raise
                            time.sleep(0.002)
                if snapshot is None:  # pragma: no cover - defensive
                    raise RuntimeError("state snapshot unavailable")
                payload = json.dumps(snapshot, ensure_ascii=False, indent=1)
                backup = self.path.with_name(self.path.name + ".bak")
                try:
                    # Only refresh .bak with a file that still parses. After a
                    # recovery state.json is valid again, but if a bad write
                    # ever leaves it broken this keeps the last good copy
                    # instead of overwriting it with garbage.
                    if self.path.is_file() and self._is_readable_state(self.path):
                        shutil.copy2(self.path, backup)
                except Exception:
                    pass
                self._write_payload(payload)
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
