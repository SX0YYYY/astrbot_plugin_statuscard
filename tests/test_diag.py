from __future__ import annotations

import time
from pathlib import Path

from astrbot_plugin_statuscard.state import StateStore


class _DiagHost:
    """Minimal host exposing the diag helpers used by the plugin."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._diag: dict[str, float] = self._load_diag()

    def _load_diag(self) -> dict[str, float]:
        raw = self.state.get("diag", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return out

    def _save_diag(self) -> None:
        self.state.data["diag"] = dict(self._diag)
        self.state.save()


def test_diag_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    first = _DiagHost(StateStore(path))
    first._diag["last_render_ok"] = 1.0
    first._diag["last_render_ms"] = 1234.5
    first._diag["last_render_at"] = time.time()
    first._save_diag()

    # A reload creates a brand new host from the same file.
    second = _DiagHost(StateStore(path))
    assert second._diag["last_render_ms"] == 1234.5
    assert second._diag["last_render_ok"] == 1.0
    assert "last_render_at" in second._diag


def test_diag_save_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    host = _DiagHost(StateStore(path))
    host._diag["last_report_render_ok"] = 0.0
    host._save_diag()
    host._save_diag()
    assert StateStore(path).get("diag", {})["last_report_render_ok"] == 0.0


def test_diag_ignores_corrupt_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"diag": "not-a-dict"}', encoding="utf-8")
    host = _DiagHost(StateStore(path))
    assert host._diag == {}
