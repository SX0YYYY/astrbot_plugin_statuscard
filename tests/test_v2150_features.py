"""Regression tests for the v2.15.0 feature batch."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import (
    DailyReportBuilder,
    detect_resource_gaps,
)
from astrbot_plugin_statuscard.state import StateStore

TARGET = "default:GroupMessage:461961169"


def _report_config() -> dict:
    return {
        "daily_report": {
            "enabled": True,
            "targets": [TARGET],
            "sections": ["resource"],
            "with_image": True,
        }
    }


class _OkPlugin:
    """Plugin double that records text sends and returns clean receipts."""

    def __init__(self) -> None:
        self.texts: list[tuple[list[str], str, str]] = []
        self.images: list[tuple[list[str], str, str]] = []
        self.context = None

    async def render_report_image(self, view: dict) -> str:
        return "/tmp/report.png"

    async def send_image(
        self, targets: list[str], url: str, source: str = "unknown"
    ) -> DeliveryResult:
        self.images.append((list(targets), url, source))
        return DeliveryResult(ok=True, ok_targets=list(targets))

    async def send_text(
        self, targets: list[str], text: str, source: str = "unknown"
    ) -> DeliveryResult:
        self.texts.append((list(targets), text, source))
        return DeliveryResult(ok=True, ok_targets=list(targets))


class _RecordingBuilder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"day": "2026-10-28", "sections": kwargs.get("sections")}


# ----------------------------------------------------------------------
# E: weekly sections
# ----------------------------------------------------------------------


def test_weekly_sections_follow_daily_by_default(tmp_path: Path) -> None:
    reporter = DailyReporter(
        _OkPlugin(), ReportStore(tmp_path / "r.db"), _report_config()
    )
    assert reporter._weekly_sections() == reporter._sections()


def test_weekly_sections_independent(tmp_path: Path) -> None:
    config = _report_config()
    config["daily_report"]["sections"] = ["resource", "anomaly"]
    config["daily_report"]["weekly_sections"] = ["website", "process"]
    reporter = DailyReporter(_OkPlugin(), ReportStore(tmp_path / "r.db"), config)
    assert reporter._weekly_sections() == ["website", "process"]


def test_weekly_preview_uses_weekly_sections(tmp_path: Path) -> None:
    config = _report_config()
    config["daily_report"]["weekly_sections"] = ["token"]
    plugin = _OkPlugin()
    reporter = DailyReporter(plugin, ReportStore(tmp_path / "r.db"), config)
    reporter.builder = _RecordingBuilder()  # type: ignore[assignment]

    asyncio.run(reporter.preview("default:FriendMessage:10000", span_days=7))

    assert reporter.builder.calls[-1]["sections"] == ["token"]
    assert plugin.images  # 预览图片正常发出


# ----------------------------------------------------------------------
# B: sample backfill
# ----------------------------------------------------------------------


def test_backfill_upserts_missing_points(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    plugin = _OkPlugin()
    collector = _make_collector(plugin, state, store)
    now = time.time()
    state.set(
        "resource_cache",
        {
            "points": [
                {"ts": int(now) - 90000, "cpu": 1.0, "ram": 2.0, "swap": 0.0},
                {"ts": int(now) - 60, "cpu": 3.0, "ram": 4.0, "swap": 1.0},
            ]
        },
    )
    collector._backfill_from_cache()
    rows = store.list_resources(int(now) - 86400, limit=100)
    assert [row["ts"] for row in rows] == [int(now) - 60]
    assert rows[0]["cpu"] == 3.0
    # 超出 24 小时缓存的点不回填
    assert all(row["ts"] != int(now) - 90000 for row in rows)
    # Idempotent: a second pass must not duplicate anything.
    collector._backfill_from_cache()
    rows = store.list_resources(int(now) - 86400, limit=100)
    assert len(rows) == 1


def _make_collector(plugin: Any, state: Any, store: Any) -> Any:
    from astrbot_plugin_statuscard.report import ReportCollector

    return ReportCollector(plugin, state, {"daily_report": {}}, store)


# ----------------------------------------------------------------------
# C: downtime gaps
# ----------------------------------------------------------------------


def test_detect_resource_gaps_threshold() -> None:
    base = 1_700_000_000
    stamps = [base, base + 300, base + 600, base + 5000, base + 5300]
    gaps = detect_resource_gaps(stamps, 300)
    assert gaps == [{"start": base + 600, "end": base + 5000}]


def test_daily_resource_section_reports_gaps(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 10, 28, 12, 0, 0)
    first = int(day.timestamp())
    store.add_resource(first, 1.0, 10.0, 0.0, None, None)
    store.add_resource(first + 20000, 2.0, 20.0, 0.0, None, None)
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-28", sections=["resource"])
    resources = view["resources"]
    assert resources["gaps"]
    assert resources["gaps_text"] and "停机盲区" in resources["coverage_note"]


# ----------------------------------------------------------------------
# D: alert aggregation
# ----------------------------------------------------------------------


def _async_send(recorder: list[str], fail_first: bool = False):
    state = {"n": 0}

    async def send(text: str) -> DeliveryResult:
        state["n"] += 1
        recorder.append(text)
        if fail_first and state["n"] == 1:
            return DeliveryResult(ok=False, failed_targets=[TARGET])
        return DeliveryResult(ok=True, ok_targets=[TARGET])

    return send


def test_aggregator_first_send_then_buffer() -> None:
    sent: list[str] = []
    agg = AlertAggregator(window_seconds=120, max_items=8)
    first = asyncio.run(agg.push("A", "原因A", "文本A", _async_send(sent)))
    assert first is not None and sent == ["文本A"]
    second = asyncio.run(agg.push("B", "原因B", "文本B", _async_send(sent)))
    assert second is None and sent == ["文本A"]


async def _drain(agg: AlertAggregator, sent: list[str]) -> None:
    await agg.flush(_async_send(sent))


def test_aggregator_flush_posts_summary() -> None:
    sent: list[str] = []
    agg = AlertAggregator(window_seconds=120, max_items=8)
    asyncio.run(agg.push("A", "原因A", "文本A", _async_send(sent)))
    asyncio.run(agg.push("B", "原因B", "文本B", _async_send(sent)))
    asyncio.run(_drain(agg, sent))
    assert len(sent) == 2
    assert "2 项异常" in sent[-1] and "A: 原因A" in sent[-1]
    assert not agg.active


def test_aggregator_disabled_sends_everything() -> None:
    sent: list[str] = []
    agg = AlertAggregator(window_seconds=0, max_items=8)
    for label in ("A", "B"):
        result = asyncio.run(
            agg.push(label, f"原因{label}", f"文本{label}", _async_send(sent))
        )
        assert result is not None
    assert sent == ["文本A", "文本B"]


def test_resource_alert_buffered_records_alert_event(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        }
    }
    plugin = _FailingAlertPlugin()
    engine = alerts_module.AlertEngine(plugin, state, config, report_store=store)
    # 预热聚合窗口，使下一条进入缓冲
    warm = AlertAggregator(window_seconds=120, max_items=8)
    engine._aggregator = warm
    asyncio.run(warm.push("预热", "x", "text", _async_send([])))

    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))

    rules = state.data["alerts"]["rules"]["cpu"]
    assert rules["alerting"] is True
    events = store.list_events_range(0, int(time.time()) + 60, limit=50)
    statuses = [e["status"] for e in events if e["name"] == "CPU"]
    assert statuses == ["alert"]


class _FailingAlertPlugin:
    """Alert sends always fail, like the v2.13.0 give-up test double."""

    def __init__(self) -> None:
        self.context = None

    async def send_text(
        self, targets: list[str], text: str, source: str = "unknown"
    ) -> DeliveryResult:
        return DeliveryResult(ok=False, failed_targets=list(targets))


# ----------------------------------------------------------------------
# F1: offline broadcast
# ----------------------------------------------------------------------


class _BroadcastClient:
    async def call_action(self, action: str, **kwargs: Any) -> Any:
        assert action == "get_group_list"
        return {"data": [{"group_id": 111}, {"group_id": 222}, {"group_id": 333}]}


def _broadcast_config(extra: dict | None = None) -> dict:
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "platform_id": "default",
            "offline_broadcast": {
                "enabled": True,
                "exclude_groups": ["222"],
                "interval_seconds": 1,
                "max_groups": 100,
            },
        }
    }
    if extra:
        config["alerts"]["offline_broadcast"].update(extra)
    return config


def test_offline_broadcast_disabled_by_default(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    plugin = _OkPlugin()
    engine = alerts_module.AlertEngine(plugin, state, {"alerts": {}})
    asyncio.run(engine._broadcast_recovery(time.time() - 60, "1 分钟"))
    assert plugin.texts == []


def test_offline_broadcast_skips_excluded_groups(tmp_path: Path, monkeypatch) -> None:
    state = StateStore(tmp_path / "state.json")
    plugin = _OkPlugin()
    engine = alerts_module.AlertEngine(plugin, state, _broadcast_config())
    monkeypatch.setattr(
        alerts_module,
        "_get_aiocqhttp_client",
        lambda context, platform_id, self_id=None: _BroadcastClient(),
    )

    async def fake_call_action(client: Any, action: str, **kwargs: Any) -> Any:
        return await client.call_action(action, **kwargs)

    monkeypatch.setattr(alerts_module, "_call_action", fake_call_action)
    asyncio.run(engine._broadcast_recovery(time.time() - 120, "2 分钟"))

    targets = [item[0][0] for item in plugin.texts]
    assert targets == ["default:GroupMessage:111", "default:GroupMessage:333"]
    text = plugin.texts[0][1]
    assert "掉线时间" in text and "恢复时间" in text and "离线时长" in text


# ----------------------------------------------------------------------
# F2: notification center routing
# ----------------------------------------------------------------------


def test_alerts_send_kind_routing(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    plugin = _OkPlugin()
    config = {
        "alerts": {"enabled": True, "targets": ["999"]},
        "notify": {
            "rules": [
                {"enabled": True, "target": "111", "kinds": ["resource"]},
                {"enabled": True, "target": "222", "kinds": ["offline"]},
            ]
        },
    }
    engine = alerts_module.AlertEngine(plugin, state, config)
    asyncio.run(engine._send("资源告警", kind="resource"))
    asyncio.run(engine._send("掉线通知", kind="offline"))
    assert plugin.texts[0][0] == ["default:GroupMessage:111"]
    assert plugin.texts[1][0] == ["default:GroupMessage:222"]


def test_report_targets_weekly_routing(tmp_path: Path) -> None:
    config = _report_config()
    config["notify"] = {
        "rules": [
            {"enabled": True, "target": "333", "kinds": ["weekly"]},
        ]
    }
    reporter = DailyReporter(_OkPlugin(), ReportStore(tmp_path / "r.db"), config)
    assert reporter._targets("weekly") == ["default:GroupMessage:333"]
    # 日报无规则匹配 → 回退旧三处 targets
    assert reporter._targets("daily") == ["default:GroupMessage:461961169"]


def test_audit_flags_notify_center() -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    issues = run_audit(
        {
            "notify": {
                "rules": [
                    {
                        "enabled": True,
                        "target": "123456",
                        "kinds": ["resource"],
                    }
                ]
            }
        }
    )
    assert any(issue.code == "notify_center_active" for issue in issues)
    issues2 = run_audit(
        {"notify": {"rules": [{"enabled": True, "target": "123456", "kinds": []}]}}
    )
    assert any(issue.code == "notify_rule_invalid" for issue in issues2)
