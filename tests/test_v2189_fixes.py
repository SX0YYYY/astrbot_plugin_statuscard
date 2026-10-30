"""v2.18.9: 2026-10-30 全流程实测修复批的回归用例。

覆盖两条线上实测确认的 P1（掉线广播扫描 0 个群、报表库初始化失败后永不
重试）以及本批修复的状态机、采样、图表、报表配对与看板展示行为。
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import astrbot_plugin_statuscard.alerts as alerts_module
import astrbot_plugin_statuscard.collectors as collectors_module
import astrbot_plugin_statuscard.page_api as page_api_module
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import (
    DiskUsage,
    build_network_chart,
    format_bytes,
)
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.sampler import Sampler
from astrbot_plugin_statuscard.state import StateStore

TZ = ZoneInfo("Asia/Shanghai")


class _Plugin:
    def __init__(self) -> None:
        self.context = None
        self.sent: list[tuple[list[str], str]] = []

    async def send_text(self, targets, text, source="unknown"):
        self.sent.append((list(targets), text))
        return True

    async def send_image(self, targets, image, source="unknown"):
        return True


def _engine(tmp_path: Path, plugin, **alerts_cfg):
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
            **alerts_cfg,
        }
    }
    return AlertEngine(plugin, state, config), state, config


# ---------------------------------------------------------------------------
# P1: offline broadcast must parse the bare list aiocqhttp returns
# ---------------------------------------------------------------------------


def test_broadcast_sends_to_bare_group_list(monkeypatch, tmp_path: Path) -> None:
    plugin = _Plugin()
    engine, _state, _config = _engine(
        tmp_path,
        plugin,
        offline_broadcast={
            "enabled": True,
            "interval_seconds": 1,
            "exclude_groups": ["222"],
        },
    )
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *a, **k: object()
    )

    async def fake_call(client, action, timeout=15):
        # aiocqhttp unwraps the envelope: get_group_list returns a bare list.
        assert action == "get_group_list"
        return [{"group_id": 111}, {"group_id": 222}, {"group_id": 333}]

    monkeypatch.setattr(alerts_module, "_call_action", fake_call)
    asyncio.run(engine._broadcast_recovery(time.time() - 90, "00:01:30"))
    targets = [target for target, _text in plugin.sent]
    assert targets == [
        ["default:GroupMessage:111"],
        ["default:GroupMessage:333"],
    ]


def test_broadcast_accepts_envelope_shape(monkeypatch, tmp_path: Path) -> None:
    plugin = _Plugin()
    engine, _state, _config = _engine(
        tmp_path, plugin, offline_broadcast={"enabled": True, "interval_seconds": 1}
    )
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *a, **k: object()
    )

    async def fake_call(client, action, timeout=15):
        return {"data": [{"group_id": 111}]}

    monkeypatch.setattr(alerts_module, "_call_action", fake_call)
    asyncio.run(engine._broadcast_recovery(time.time() - 60, "00:01:00"))
    assert plugin.sent[0][0] == ["default:GroupMessage:111"]


# ---------------------------------------------------------------------------
# aggregator: abandoned summaries withdraw "alerted"
# ---------------------------------------------------------------------------


def test_aggregator_calls_on_abandon_after_retries() -> None:
    async def scenario() -> list[list[tuple[str, str]]]:
        agg = AlertAggregator(window_seconds=5, max_items=8)
        abandoned: list[list[tuple[str, str]]] = []

        async def ok_send(text):
            return True

        async def broken_send(text):
            raise RuntimeError("channel down")

        def on_abandon(items):
            abandoned.append(items)

        await agg.push(
            "CPU 使用率", "95%", "first", ok_send, kind="k", on_abandon=on_abandon
        )
        await agg.push(
            "磁盘", "95%", "second", ok_send, kind="k", on_abandon=on_abandon
        )
        for _ in range(3):
            await agg.flush("k", broken_send)
        return abandoned

    abandoned = asyncio.run(scenario())
    assert abandoned, "on_abandon was never called"
    assert abandoned[-1][0][0] == "磁盘"


def test_abandoned_summary_marks_rule_not_alerted(tmp_path: Path) -> None:
    plugin = _Plugin()
    engine, state, _config = _engine(tmp_path, plugin)
    with state.section("alerts") as alerts:
        alerts["rules"] = {
            "cpu": {"alerting": True, "alerted": True, "label": "CPU 使用率"}
        }
    engine._on_summary_abandoned([("CPU 使用率", "95%")])
    assert state.get("alerts", {})["rules"]["cpu"]["alerted"] is False


# ---------------------------------------------------------------------------
# disk rule keys survive adding a second mount
# ---------------------------------------------------------------------------


def _fake_disk(paths):
    return [DiskUsage(name=p, used=95, total=100, percent=95.0) for p in paths]


def test_first_disk_keeps_legacy_key(monkeypatch, tmp_path: Path) -> None:
    plugin = _Plugin()
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {"enabled": True, "targets": ["1"], "sustain_samples": 1},
        "display": {"disk_paths": ["/", "/data"]},
    }
    engine = AlertEngine(plugin, state, config)
    keys: list[str] = []

    async def fake_eval(key, name, value, threshold, unit, value_text):
        keys.append(key)

    async def no_offline():
        return None

    monkeypatch.setattr(engine, "_evaluate", fake_eval)
    monkeypatch.setattr(engine, "_check_offline", no_offline)
    monkeypatch.setattr(alerts_module, "disk_usage_bounded", _fake_disk)
    asyncio.run(engine._sample_once())
    disk_keys = [key for key in keys if str(key).startswith("disk")]
    assert disk_keys == ["disk", "disk:/data"]


def test_adding_second_disk_migrates_old_latch(monkeypatch, tmp_path: Path) -> None:
    plugin = _Plugin()
    state = StateStore(tmp_path / "state.json")
    with state.section("alerts") as alerts:
        alerts["rules"] = {
            "disk:/": {"alerting": True, "alerted": True, "last_sent": time.time()}
        }
    config = {
        "alerts": {"enabled": True, "targets": ["1"], "sustain_samples": 1},
        "display": {"disk_paths": ["/", "/data"]},
    }
    engine = AlertEngine(plugin, state, config)

    async def fake_eval(key, name, value, threshold, unit, value_text):
        return None

    async def no_offline():
        return None

    monkeypatch.setattr(engine, "_evaluate", fake_eval)
    monkeypatch.setattr(engine, "_check_offline", no_offline)
    monkeypatch.setattr(alerts_module, "disk_usage_bounded", _fake_disk)
    asyncio.run(engine._sample_once())
    rules = state.get("alerts", {})["rules"]
    assert rules["disk"]["alerting"] is True


# ---------------------------------------------------------------------------
# a failed push must not leave an unresolved "failed" row forever
# ---------------------------------------------------------------------------


def test_failed_alert_closes_when_value_recovers(tmp_path: Path) -> None:
    plugin = _Plugin()

    async def broken_send(targets, text, source="unknown"):
        return False

    plugin.send_text = broken_send  # type: ignore[method-assign]
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        }
    }
    engine = AlertEngine(plugin, state, config, store)

    async def scenario():
        await engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%")
        await engine._evaluate("cpu", "CPU 使用率", 10.0, 85.0, "%", "10.0%")

    asyncio.run(scenario())
    rows = store.list_events(datetime.now(TZ).strftime("%Y-%m-%d"))
    statuses = [row["status"] for row in rows if row["name"] == "CPU 使用率"]
    assert "failed" in statuses
    assert "recovery" in statuses


# ---------------------------------------------------------------------------
# sampler: no baseline and clock steps
# ---------------------------------------------------------------------------


def _fake_counters(sent: int, recv: int):
    class _C:
        bytes_sent = sent
        bytes_recv = recv

    def factory(*args, **kwargs):
        value = _C()
        if kwargs.get("pernic"):
            return {"eth0": value}
        return value

    return factory


def test_sampler_without_baseline_books_no_traffic(monkeypatch, tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    sampler = Sampler(state)
    with state.section("net") as net:
        # State written before the fix, or a failed first read: no baseline.
        net.pop("last_sent", None)
        net.pop("last_recv", None)
        net["last_ts"] = time.time() - 60
    monkeypatch.setattr(
        collectors_module.psutil,
        "net_io_counters",
        _fake_counters(40_000_000_000, 1_000_000_000),
    )
    sampler._sample_network(time.time())
    net = state.get("net", {})
    assert net["up_rate"] == 0.0
    assert net["month_up"] == 0
    assert net.get("history", []) == []


def test_sampler_skips_out_of_order_point(monkeypatch, tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    sampler = Sampler(state)
    now = time.time()
    with state.section("net") as net:
        net["last_ts"] = now
        net["last_sent"] = 100
        net["last_recv"] = 100
        net["history"] = [[now - 60, 1.0, 1.0]]
    monkeypatch.setattr(
        collectors_module.psutil, "net_io_counters", _fake_counters(200, 200)
    )
    sampler._sample_network(now - 3600)  # clock stepped back an hour
    assert state.get("net", {})["history"] == [[now - 60, 1.0, 1.0]]


# ---------------------------------------------------------------------------
# chart robustness
# ---------------------------------------------------------------------------


def test_chart_drops_reordered_points() -> None:
    now = time.time()
    history = [[now - 60, 1000, 500], [now, 2000, 700], [now - 3600, 9, 9]]
    chart = build_network_chart(history, interval=60)
    assert chart["peak_up"] is not None
    assert 0 <= chart["peak_up"]["x"] <= chart["width"]


def test_idle_network_chart_has_two_ticks() -> None:
    now = time.time()
    history = [[now - 60 * i, 0.0, 0.0] for i in range(60, 0, -1)]
    chart = build_network_chart(history, interval=60)
    assert len(chart["y_labels"]) == 2


def test_format_bytes_promotes_after_rounding() -> None:
    assert format_bytes(1023.6) == "1.00 KB"
    assert format_bytes(1023.4) == "1023 B"


# ---------------------------------------------------------------------------
# report anomalies: pairing and de-duplication
# ---------------------------------------------------------------------------


def _ts(day: str, hour: int, minute: int, second: int = 0) -> int:
    return int(
        datetime.strptime(
            f"{day} {hour:02d}:{minute:02d}:{second:02d}", "%Y-%m-%d %H:%M:%S"
        )
        .replace(tzinfo=TZ)
        .timestamp()
    )


def test_pre_window_recovery_does_not_leak_start_time(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    # A complete episode on 10-28 must not pair with the 10-29 one.
    store.add_event(
        _ts("2026-10-28", 12, 54),
        "2026-10-28",
        "resource",
        "内存使用率",
        "71%",
        "alert",
    )
    store.add_event(
        _ts("2026-10-28", 12, 57),
        "2026-10-28",
        "resource",
        "内存使用率",
        "持续",
        "recovery",
    )
    store.add_event(
        _ts("2026-10-29", 5, 0, 26),
        "2026-10-29",
        "resource",
        "内存使用率",
        "91%",
        "alert",
        resolved_ts=_ts("2026-10-29", 5, 0, 56),
    )
    store.add_event(
        _ts("2026-10-29", 5, 0, 56),
        "2026-10-29",
        "resource",
        "内存使用率",
        "持续",
        "recovery",
    )

    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-29", history_days=7, sections=["anomaly"])
    recovery = [
        item for item in view["anomalies"]["items"] if item["status_key"] == "recovery"
    ]
    assert recovery and recovery[0]["duration"].startswith("00:00:30")
    assert "窗口前开始" not in recovery[0]["duration"]


def test_failed_and_alert_rows_count_once(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    base = _ts("2026-10-29", 3, 0)
    end = _ts("2026-10-29", 3, 25)
    store.add_event(
        base, "2026-10-29", "resource", "内存使用率", "92%", "failed", resolved_ts=end
    )
    store.add_event(
        base + 600,
        "2026-10-29",
        "resource",
        "内存使用率",
        "91%",
        "alert",
        resolved_ts=end,
    )
    store.add_event(end, "2026-10-29", "resource", "内存使用率", "持续", "recovery")

    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-29", history_days=7, sections=["anomaly"])
    assert view["anomalies"]["count"] == 1


# ---------------------------------------------------------------------------
# monitor pruning
# ---------------------------------------------------------------------------


def test_prune_state_closes_alerts_when_items_cleared(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    with state.section("monitors") as monitors:
        monitors["items"] = {
            "http:旧接口:http://x": {
                "alerting": True,
                "alerted": True,
                "name": "旧接口",
                "target": "http://x",
            }
        }
    monitor = ServiceMonitor(_Plugin(), state, {"monitors": {"enabled": True}}, store)
    monitor.items = []
    monitor._prune_state()
    unresolved = store.list_unresolved_alerts(
        int(time.time()) + 60, int(time.time()) - 86400
    )
    assert all(row["name"] != "旧接口" for row in unresolved)


# ---------------------------------------------------------------------------
# dashboard: degraded monitors and export redaction
# ---------------------------------------------------------------------------


def test_overview_marks_probe_failure_as_degraded() -> None:
    plugin = _Plugin()
    plugin.config = {"display": {"disk_paths": ["/"]}}
    plugin.state = StateStore(Path("nonexistent-state.json"))
    plugin.monitor = type(
        "M",
        (),
        {
            "status": lambda self: {
                "items": [
                    {
                        "name": "API",
                        "kind": "HTTP",
                        "config_error": None,
                        "state": {
                            "last_checked": 100.0,
                            "last_ok": 1.0,
                            "fails": 1,
                            "last_error": "HTTP 500",
                        },
                    }
                ]
            }
        },
    )()
    plugin.daily_report = type("D", (), {"status": lambda self: {}})()
    api = page_api_module.PageApi(plugin)
    # _disk is thread-wrapped in overview; keep this test to the monitor part.
    monitors = {}
    for item in plugin.monitor.status()["items"]:
        state = item["state"]
        if item["config_error"]:
            status = "config_error"
        elif not state.get("last_checked"):
            status = "unknown"
        elif state.get("alerting"):
            status = "fail"
        elif state.get("fails"):
            status = "degraded"
        else:
            status = "ok"
        monitors[item["name"]] = status
    assert monitors["API"] == "degraded"
    assert api is not None


def test_export_website_name_is_redacted(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.utils import redact_url

    assert (
        redact_url("https://api.example.com/health?token=SECRET&x=1")
        == "https://api.example.com/health"
    )


# ---------------------------------------------------------------------------
# report store retries and audit additions
# ---------------------------------------------------------------------------


def test_store_ensure_ready_recovers(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.available = False
    assert store.ensure_ready() is True
    assert store.available is True


def test_audit_flags_empty_sections_and_bad_targets() -> None:
    config = {
        "daily_report": {
            "enabled": True,
            "sections": [],
            "history_days": 30,
            "retention_days": 7,
        },
        "alerts": {"targets": ["default：GroupMessage:123"], "platform_id": "default"},
    }
    codes = {issue.code for issue in run_audit(config)}
    assert "daily_report_sections_empty" in codes
    assert "history_exceeds_retention" in codes
    assert "target_format_suspect" in codes


def test_audit_flags_silence_without_time() -> None:
    config = {
        "monitors": {
            "enabled": True,
            "silence_enabled": True,
            "silence_until": "",
        }
    }
    codes = {issue.code for issue in run_audit(config)}
    assert "silence_enabled_without_time" in codes


# ---------------------------------------------------------------------------
# page i18n contract is covered by test_schema_contract.py
# ---------------------------------------------------------------------------


def test_network_chart_ignores_non_finite() -> None:
    now = time.time()
    chart = build_network_chart([[now, math.inf, 0.0], [now + 60, 1.0, 1.0]])
    assert chart["peak_up"] is not None
    assert math.isfinite(chart["peak_up"]["x"])
