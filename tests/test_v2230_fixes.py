"""Regression tests for the v2.20.30 tech-debt batch (C group).

C5  verify with a lost body must be rejected, not silently run monitors
C6  a transient write lock (SQLITE_BUSY) is not a broken report database
C10 status() reports the configured target, with the state key beside it
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore


def test_verify_rejects_a_missing_kind(monkeypatch) -> None:
    """A bridge that loses the JSON body used to run monitors silently."""

    async def request_value(name, default=None):
        return default

    monkeypatch.setattr(page_api_module, "_request_value", request_value)
    api = PageApi(SimpleNamespace(config={"display": {}}))
    payload = asyncio.run(api.verify())
    assert payload["ok"] is False
    assert payload["error_code"] == "verify_kind_missing"


def test_verify_rejects_an_unknown_kind(monkeypatch) -> None:
    async def request_value(name, default=None):
        return "everything"

    monkeypatch.setattr(page_api_module, "_request_value", request_value)
    api = PageApi(SimpleNamespace(config={"display": {}}))
    payload = asyncio.run(api.verify())
    assert payload["ok"] is False
    assert payload["error_code"] == "verify_kind_missing"


def test_probe_treats_a_write_lock_as_available(tmp_path, monkeypatch) -> None:
    """BUSY means another writer; the report path itself is read-only."""

    store = ReportStore(tmp_path / "report.db")
    assert store.available
    real_connect = store._connect

    class _BusyConnection:
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("database is locked")
            return SimpleNamespace(fetchone=lambda: (0,), fetchall=list)

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    calls = {"n": 0}

    def fake_connect():
        calls["n"] += 1
        if calls["n"] == 1:
            return _BusyConnection()
        return real_connect()

    monkeypatch.setattr(store, "_connect", fake_connect)
    assert store.probe() is True
    assert store.available is True


def test_probe_still_reports_a_read_only_database(tmp_path, monkeypatch) -> None:
    """A real read-only rejection must keep failing the probe."""

    store = ReportStore(tmp_path / "report.db")

    class _ReadOnlyConnection:
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return SimpleNamespace(fetchone=lambda: (0,), fetchall=list)

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(store, "_connect", lambda: _ReadOnlyConnection())
    assert store.probe() is False
    assert store.available is False


def test_status_reports_the_configured_target(tmp_path) -> None:
    """``target`` is what the operator configured; ``state_key`` is identity."""

    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "monitors": {
            "enabled": True,
            "items": [
                {
                    "type": "http",
                    "name": "文档",
                    "url": "https://h/x?token=SECRET",
                    "keyword": "ok",
                }
            ],
        },
    }
    monitor = ServiceMonitor(None, state, config)
    item = monitor.status()["items"][0]
    # The display target must not leak the internal query hash / state suffix.
    assert "#q" not in item["target"]
    assert "#q" in item["state_key"]
    assert item["target"].startswith("https://h/")
    assert "SECRET" not in item["target"]
