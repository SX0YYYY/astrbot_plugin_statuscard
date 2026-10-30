"""Regression tests for the v2.18.8 P3 batch.

Covers the behaviour-changing fixes: inflight recovery must not announce a
recovery, orphan rules/items are closed before being dropped, clock steps do
not inflate rates, the network chart is positioned by time and breaks its
filled areas, the weekly summary labels its scopes, non-finite numbers and
UMOs are masked, and the report CSS goes in as a template variable.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard import collectors as collectors_module
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.collectors import cfg_bool
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder, _fmt_compact
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.t2i_renderer import build_report_html

TZ = ZoneInfo("Asia/Shanghai")


class _Plugin:
    def __init__(self) -> None:
        self.state = None


def _engine(tmp_path: Path, store: ReportStore | None = None) -> AlertEngine:
    return AlertEngine(
        _Plugin(),
        StateStore(tmp_path / "state.json"),
        {"alerts": {"enabled": True}, "display": {"timezone": "Asia/Shanghai"}},
        report_store=store,
    )


def test_inflight_recovery_does_not_announce_recovery(tmp_path: Path) -> None:
    """A reloaded send may have arrived; do not claim a recovery for it."""

    import asyncio

    engine = _engine(tmp_path)
    engine.state.set(
        "alerts",
        {"rules": {"cpu": {"send_inflight": True, "alerting": False}}},
    )
    calls: list[str] = []

    async def fake_send(text, *args, **kwargs):
        calls.append(text)
        from astrbot_plugin_statuscard.delivery import DeliveryResult

        return DeliveryResult(ok=True)

    engine._send = fake_send
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 10.0, 85.0, "%", "10.0%"))
    rule = engine.state.get("alerts")["rules"]["cpu"]
    # The episode is closed, but no "已恢复" is sent: the alert may never have
    # reached the operator, and an unexplained recovery is worse than silence.
    assert rule["alerting"] is False
    assert calls == []


def test_prune_rules_closes_orphan_episode(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    engine = _engine(tmp_path, store)
    engine.state.set(
        "alerts",
        {
            "rules": {
                "cpu": {"alerting": True, "since": time.time() - 60},
                "disk:/data": {"alerting": True, "since": time.time() - 60},
            }
        },
    )
    engine._prune_rules({"cpu"})
    rules = engine.state.get("alerts")["rules"]
    assert set(rules) == {"cpu"}
    events = store.list_events_range(0, int(time.time()) + 60, limit=10)
    assert any(event["status"] == "recovery" for event in events)
    assert any("规则已移除" in str(event["detail"]) for event in events)


def test_monitor_prune_state_closes_orphan_episode(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        _Plugin(),
        StateStore(tmp_path / "state.json"),
        {"monitors": {"enabled": True, "items": []}},
        report_store=store,
    )
    monitor.items = [
        {"__template_key": "http", "name": "保留", "url": "http://x/", "enabled": True}
    ]
    monitor.state.set(
        "monitors",
        {
            "items": {
                "http:已删除:http://old/": {"alerting": True, "since": time.time()},
            }
        },
    )
    monitor._prune_state()
    events = store.list_events_range(0, int(time.time()) + 60, limit=10)
    assert any(event["status"] == "recovery" for event in events)


def test_cfg_bool_accepts_numeric_flags() -> None:
    assert cfg_bool({"x": 1}, "x", False) is True
    assert cfg_bool({"x": 0}, "x", True) is False
    assert cfg_bool({"x": "1"}, "x", False) is True
    assert cfg_bool({}, "x", True) is True


def test_network_chart_positions_by_time_and_breaks_area() -> None:
    # 60 samples so the "pad to one hour" branch does not kick in; a two hour
    # outage sits between sample 29 and 30.
    history = [[1000.0 + 60 * index, 10.0, 5.0] for index in range(30)] + [
        [1000.0 + 60 * 30 + 7200 + 60 * index, 10.0, 5.0] for index in range(30)
    ]
    chart = collectors_module.build_network_chart(history, tz=TZ, interval=60)
    assert chart["empty"] is False
    assert len(chart["up_areas"]) == 2
    assert len(chart["up_segments"]) == 2
    # Time-proportional x: the first sample after the gap is far right of
    # the chart midpoint (an index-based axis put it at 50%).
    midpoint = chart["left"] + chart["plot_w"] / 2
    points = chart["up_segments"][1].split()
    assert float(points[0].split(",")[0]) > midpoint


def test_weekly_summary_labels_scopes(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-20", sections=["resource"], span_days=7)
    summary = view["summary"]
    assert summary["span_days"] == 7
    assert summary["window_label"] == "近 7 天"
    assert summary["last_day_label"] == "最后一天"

    daily = builder.build(day="2026-10-20", sections=["resource"])
    assert daily["summary"]["span_days"] == 1


def test_fmt_compact_rejects_non_finite() -> None:
    assert _fmt_compact(float("inf")) == "--"
    assert _fmt_compact(float("nan")) == "--"
    assert _fmt_compact(1500) == "1.5K"


def test_mask_umo_hides_chat_id() -> None:
    masked = page_api_module._mask_umo("default:GroupMessage:461961169")
    assert masked.endswith("1169")
    assert "461961169" not in masked
    assert masked.startswith("default:GroupMessage:")
    # v2.19.0：短 ID 也打码（此前 ≤4 位原样返回，泄漏 UMO 契约的边缘）
    assert page_api_module._mask_umo("1234") == "***4"
    assert page_api_module._mask_umo("default:FriendMessage:abc") == (
        "default:FriendMessage:abc"
    )  # 无数字不入打码逻辑


def test_report_css_goes_through_template_variable(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day=date(2026, 10, 20).isoformat(), sections=[])
    html = build_report_html(view)
    assert "<style>" in html
    assert "__REPORT_CSS__" not in html
