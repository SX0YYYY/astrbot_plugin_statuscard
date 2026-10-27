"""Regression tests for the v2.13.4 chart time-axis fix."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore


def _page_api(tmp_path: Path, config: dict) -> page_api_module.PageApi:
    plugin = SimpleNamespace(
        config=config,
        report_store=ReportStore(tmp_path / "report.db"),
        state=StateStore(tmp_path / "state.json"),
    )
    return page_api_module.PageApi(plugin)


def _patch_request(monkeypatch, query: dict | None = None) -> None:
    monkeypatch.setattr(page_api_module, "_request", SimpleNamespace(query=query or {}))
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)


def test_history_returns_window_bounds(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {}})
    now = int(time.time())
    assert api.plugin.report_store.add_resource(now - 600, cpu=10.0)
    _patch_request(monkeypatch, {"range": "7d"})

    payload = asyncio.run(api.history())

    assert payload["range"] == "7d"
    assert payload["window_end"] - payload["window_start"] == 7 * 86400
    assert (
        payload["window_start"] <= payload["points"][0]["ts"] <= payload["window_end"]
    )


def test_history_window_matches_each_range(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {}})
    assert api.plugin.report_store.add_resource(int(time.time()) - 300, cpu=1.0)
    expected = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    for key, seconds in expected.items():
        _patch_request(monkeypatch, {"range": key})
        payload = asyncio.run(api.history())
        assert payload["window_end"] - payload["window_start"] == seconds
