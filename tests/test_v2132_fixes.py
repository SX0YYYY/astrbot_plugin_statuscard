"""Regression tests for the v2.13.2 Web/API/content correctness batch."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import local_card
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore


def _page_api(tmp_path: Path, config: dict) -> page_api_module.PageApi:
    plugin = SimpleNamespace(
        config=config,
        report_store=ReportStore(tmp_path / "report.db"),
        state=StateStore(tmp_path / "state.json"),
    )
    return page_api_module.PageApi(plugin)


def test_snapshots_redacted_under_qq_stats(tmp_path: Path) -> None:
    api = _page_api(tmp_path, {"display": {"page_redact": ["qq_stats"]}})
    assert api.plugin.report_store.record_snapshot(
        "2026-10-25", 22, 8, 2119, 50000, True, 1
    )
    rows = api._snapshots_for_page(api.plugin)
    assert rows
    assert "friends" not in rows[0]
    assert rows[0]["day"] == "2026-10-25"


def test_history_invalid_range_normalized(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {}})
    api.plugin.report_store.add_resource(int(time.time()), cpu=1.0)
    monkeypatch.setattr(
        page_api_module,
        "_request",
        SimpleNamespace(query={"range": "bogus"}),
    )
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)
    payload = asyncio.run(api.history())
    assert payload["range"] == "24h"


def test_overview_processes_include_pid(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {}})
    monkeypatch.setattr(
        page_api_module,
        "process_info",
        lambda top_n: {
            "top": [
                {
                    "name": "python",
                    "pid": 4321,
                    "cpu_raw": 1.5,
                    "mem_mb": 12.0,
                }
            ]
        },
    )
    rows = asyncio.run(api._latest_processes(3))
    assert rows[0]["pid"] == 4321
    assert rows[0]["name"] == "python"


def test_audit_zero_threshold_is_described_as_disabled() -> None:
    issues = run_audit({"alerts": {"enabled": True, "cpu_threshold": 0}})
    zero = [issue for issue in issues if issue.code == "alert_threshold_zero"]
    assert zero
    assert "不生效" in zero[0].message


class _OkPlugin:
    async def send_text(self, targets, text, source="unknown") -> bool:
        return True


def test_recovery_clears_last_error(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        }
    }
    engine = AlertEngine(_OkPlugin(), state, config)
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    rule["last_error"] = "send_failed"
    asyncio.run(engine._evaluate("cpu", "CPU", 10.0, 85.0, "%", "10.0%"))
    assert rule["last_error"] == ""


def test_monitor_item_key_redacts_credentials(tmp_path: Path) -> None:
    monitor = ServiceMonitor(
        _OkPlugin(), StateStore(tmp_path / "state.json"), {"monitors": {}}
    )
    key = monitor._item_key(
        {
            "__template_key": "http",
            "name": "内网站点",
            "url": "https://user:secret@example.com/status?token=abc",
        }
    )
    assert "secret" not in key
    assert "token=abc" not in key
    assert "example.com" in key


def test_first_resource_ts_and_unresolved_keeps_oldest(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.add_resource(100, cpu=1.0)
    assert store.add_resource(300, cpu=2.0)
    assert store.first_resource_ts() == 100

    for index in range(60):
        assert store.add_event(
            1000 + index, "2026-10-25", "service", f"n{index}", "d", "alert"
        )
    rows = store.list_unresolved_alerts(2000, 0, limit=50)
    assert len(rows) == 50
    assert rows[0]["ts"] == 1000  # earliest kept, not the newest 50


def test_has_cjk_detects_tofu() -> None:
    class _Mask:
        def getbbox(self):
            return None

    class _Font:
        def getmask(self, text):
            return _Mask()

    assert local_card._has_cjk(_Font()) is False
    assert local_card._has_cjk(object()) is True
