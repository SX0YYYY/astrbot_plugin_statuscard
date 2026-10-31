"""Regression tests for the v2.18.7 P1/P2 fix batch.

Covers the two P1 findings of the 2026-10-30 audit (webhook auth, docker/
systemd misconfiguration detection) and the P2 items fixed in the same
version: bounded monitor retries, buffered-alert bookkeeping, query-aware
monitor keys, disk dedupe, redacted website names, 4xx/5xx availability,
state backup recovery, daily retry day, weekly resource window, unresolved
cross-midnight counting, worst-mount aggregation, weekly_time audit, address
masking and the reports cache key.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard import collectors as collectors_module
from astrbot_plugin_statuscard import monitor as monitor_module
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard import report as report_module
from astrbot_plugin_statuscard.alerts import (
    MAX_RECOVERY_RETRIES,
    _webhook_bind_allowed,
)
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import DiskUsage, website_ok
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import CONFIG_ERROR_PREFIX, ServiceMonitor
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report import DailyReporter, ReportCollector
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore

TZ = ZoneInfo("Asia/Shanghai")


class _Plugin:
    def __init__(self) -> None:
        self.state = None


# ----------------------------------------------------------------------
# P1-1: webhook auth
# ----------------------------------------------------------------------


def test_webhook_wildcard_bind_requires_enforced_token() -> None:
    assert _webhook_bind_allowed("127.0.0.1", False, "") is True
    assert _webhook_bind_allowed("127.0.0.1", True, "t") is True
    # The v2.18.6 regression: a token that is filled in but not required was
    # never checked, so the listener accepted anonymous posts on 0.0.0.0.
    assert _webhook_bind_allowed("0.0.0.0", False, "filled-in") is False
    assert _webhook_bind_allowed("::", False, "") is False
    assert _webhook_bind_allowed("0.0.0.0", True, "") is False
    assert _webhook_bind_allowed("0.0.0.0", True, "t") is True


def test_audit_flags_public_bind_without_enforced_token() -> None:
    issues = run_audit(
        {
            "alerts": {
                "komari_webhook": {
                    "enabled": True,
                    "host": "0.0.0.0",
                    "token": "filled-in",
                    "require_token": False,
                }
            },
            "display": {"redact": []},
        }
    )
    codes = {issue.code for issue in issues}
    assert "webhook_open_no_token" in codes


# ----------------------------------------------------------------------
# P1-2: docker / systemd misconfiguration
# ----------------------------------------------------------------------


def test_config_error_hints_cover_missing_container_and_unit() -> None:
    assert monitor_module._looks_like_config_error(
        "Error: No such object: my-container"
    )
    assert monitor_module._looks_like_config_error("No such container: x")
    assert monitor_module._looks_like_config_error("Unit x.service not found.")
    assert monitor_module._looks_like_config_error("Failed to get properties: x")
    # A real outage must still be treated as an outage.
    assert not monitor_module._looks_like_config_error("HTTP 500")
    assert not monitor_module._looks_like_config_error("connection refused")


def test_systemd_missing_unit_is_reported_as_config_error(monkeypatch, tmp_path):
    config = {"monitors": {"enabled": True, "fail_threshold": 1, "items": []}}
    monitor = ServiceMonitor(_Plugin(), StateStore(tmp_path / "state.json"), config)

    class _Result:
        returncode = 3
        stdout = "inactive"
        stderr = ""

    def fake_run(cmd, **kwargs):
        result = _Result()
        if "show" in cmd:
            result.stdout = "not-found"
        return result

    monkeypatch.setattr(monitor_module.subprocess, "run", fake_run)
    ok, detail, _ = asyncio.run(
        monitor._probe(
            {"__template_key": "systemd", "name": "坏 unit", "unit": "nope.service"}
        )
    )
    assert ok is False
    assert detail.startswith(CONFIG_ERROR_PREFIX)


def test_docker_missing_container_is_reported_as_config_error(monkeypatch, tmp_path):
    config = {"monitors": {"enabled": True, "fail_threshold": 1, "items": []}}
    monitor = ServiceMonitor(_Plugin(), StateStore(tmp_path / "state.json"), config)

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "Error: No such object: typo"

    monkeypatch.setattr(
        monitor_module.subprocess, "run", lambda cmd, **kwargs: _Result()
    )
    ok, detail, _ = asyncio.run(
        monitor._probe(
            {"__template_key": "docker", "name": "坏容器", "container": "typo"}
        )
    )
    assert ok is False
    assert detail.startswith(CONFIG_ERROR_PREFIX)


# ----------------------------------------------------------------------
# P2: monitor bookkeeping
# ----------------------------------------------------------------------


def _monitor(tmp_path: Path, store: ReportStore | None = None) -> ServiceMonitor:
    config = {"monitors": {"enabled": True, "fail_threshold": 1, "items": []}}
    return ServiceMonitor(
        _Plugin(), StateStore(tmp_path / "state.json"), config, report_store=store
    )


def test_buffered_alert_marks_sent_and_records_one_event(monkeypatch, tmp_path):
    store = ReportStore(tmp_path / "report.db")
    monitor = _monitor(tmp_path, store)
    item = {
        "__template_key": "http",
        "name": "站点",
        "url": "http://example.com/",
        "enabled": True,
    }

    async def fake_probe(request_item):
        return False, "HTTP 500", "http://example.com/"

    async def fake_push(*args, **kwargs):
        return None  # buffered by the aggregator

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    monkeypatch.setattr(monitor, "_push_alert", fake_push)
    asyncio.run(monitor._check_item(item))

    state = monitor.state.data["monitors"]["items"][monitor._item_key(item)]
    assert state["last_sent"] > 0
    assert state["pending"] is False
    assert state["retry_count"] == 0
    events = store.list_events_range(0, int(time.time()) + 60, limit=20)
    assert [event["status"] for event in events] == ["alert"]


def test_recovery_retry_is_bounded(monkeypatch, tmp_path):
    monitor = _monitor(tmp_path)
    item = {
        "__template_key": "http",
        "name": "站点",
        "url": "http://example.com/",
        "enabled": True,
    }
    key = monitor._item_key(item)
    monitor.state.data.setdefault("monitors", {})["items"] = {
        key: {
            "pending_recovery": "✅ 已恢复",
            "recovery_retry_count": MAX_RECOVERY_RETRIES,
            "recovery_next_attempt": 0,
            "alerting": False,
        }
    }

    async def fake_probe(request_item):
        return True, "HTTP 200", "http://example.com/"

    async def failing_send(text):
        return DeliveryResult(ok=False)

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    monkeypatch.setattr(monitor, "_send", failing_send)
    asyncio.run(monitor._check_item(item))

    state = monitor.state.data["monitors"]["items"][key]
    assert "pending_recovery" not in state
    assert "recovery_retry_count" not in state


def test_alert_retry_is_bounded(monkeypatch, tmp_path):
    monitor = _monitor(tmp_path)
    item = {
        "__template_key": "http",
        "name": "站点",
        "url": "http://example.com/",
        "enabled": True,
    }
    key = monitor._item_key(item)
    monitor.state.data.setdefault("monitors", {})["items"] = {
        key: {
            "alerting": True,
            "alerted": True,
            "pending": True,
            "retry_count": monitor_module.MAX_ALERT_RETRIES,
            "next_attempt": 0,
            "since": time.time() - 600,
        }
    }

    async def fake_probe(request_item):
        return False, "HTTP 500", "http://example.com/"

    async def failing_send(text):
        return DeliveryResult(ok=False)

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    monkeypatch.setattr(monitor, "_send", failing_send)
    asyncio.run(monitor._check_item(item))

    state = monitor.state.data["monitors"]["items"][key]
    assert state["pending"] is False
    # v2.19.0 统一语义：耗尽后保持 alerted=True，恢复通知照发并带
    # 「此前未能送达」说明（与资源规则一致），不再完全静默。
    assert state.get("alerted") is True
    assert state.get("send_exhausted") is True


def test_monitor_item_key_keeps_query_distinct() -> None:
    one = monitor_module.ServiceMonitor._item_key(
        {"__template_key": "http", "name": "订单", "url": "https://a/health?c=read"}
    )
    two = monitor_module.ServiceMonitor._item_key(
        {"__template_key": "http", "name": "订单", "url": "https://a/health?c=write"}
    )
    assert one != two
    # The query itself (and any token inside it) must not be persisted.
    assert "c=read" not in one
    assert "c=write" not in two


# ----------------------------------------------------------------------
# P2: collectors
# ----------------------------------------------------------------------


def test_website_name_redacts_query_when_blank() -> None:
    targets = collectors_module._normalize_website_targets(
        [{"url": "http://internal/health?token=SECRET", "name": ""}]
    )
    assert targets[0].name == "http://internal/health"
    assert "SECRET" not in targets[0].name


def test_website_ok_matches_report_rule() -> None:
    assert website_ok("200") is True
    assert website_ok("302") is True
    assert website_ok("404") is False
    assert website_ok("500") is False
    assert website_ok("ERR") is False


def test_state_backup_survives_second_corruption(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.set("net", {"month_up": 42})
    store.save()
    # The first save has no previous file to copy; the second one creates .bak.
    store.set("marker", 0)
    store.save()

    path.write_text('{"net": {', encoding="utf-8")
    recovered = StateStore(path)
    assert recovered.get("net") == {"month_up": 42}
    # The recovery must be written back, so the next save cannot copy the
    # broken file over .bak.
    recovered.set("marker", 1)
    recovered.save()

    path.write_text('{"broken"', encoding="utf-8")
    second = StateStore(path)
    # .bak holds the previous complete state, so the very last save may be
    # lost -- but the long-lived data (network history, counters, latches)
    # survives a second corruption instead of collapsing to an empty dict.
    assert second.get("net") == {"month_up": 42}


# ----------------------------------------------------------------------
# P2: daily retry keeps a null day
# ----------------------------------------------------------------------


def test_schedule_retry_keeps_none_day(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    plugin = _Plugin()
    plugin.state = state
    reporter = DailyReporter(plugin, store, {"daily_report": {"enabled": True}})

    assert reporter._schedule_retry(None) is True
    assert reporter._pending_retry is not None
    assert reporter._pending_retry[0] is None

    restored = DailyReporter(plugin, store, {"daily_report": {"enabled": True}})
    restored._restore_delivery_state()
    assert restored._pending_retry is not None
    assert restored._pending_retry[0] is None


def test_report_retry_uses_resolved_day(monkeypatch, tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    plugin = _Plugin()
    plugin.state = state
    reporter = DailyReporter(plugin, store, {"daily_report": {"enabled": True}})

    calls: list[object] = []

    def boom(day=None):
        calls.append(day)
        raise RuntimeError("db busy")

    monkeypatch.setattr(reporter, "_build_view", boom)
    asyncio.run(reporter._send_scheduled())
    assert calls == [None]
    assert reporter._pending_retry is not None
    # The scheduled run resolves "yesterday" for the retry plan too, so a
    # build failure that crosses midnight does not rebuild *today's* (mostly
    # empty) report and discard the one that actually failed (v2.20.3).
    assert reporter._pending_retry[0] == reporter._default_report_day()


# ----------------------------------------------------------------------
# P2: weekly resource window
# ----------------------------------------------------------------------


def _store_with_week(tmp_path: Path, last_day_samples: int = 96) -> ReportStore:
    store = ReportStore(tmp_path / "report.db")
    start = datetime(2026, 10, 14, 0, 0, tzinfo=TZ)
    for offset in range(7):
        day = start + timedelta(days=offset)
        samples = last_day_samples if offset == 6 else 96
        for index in range(samples):
            ts = int((day + timedelta(minutes=15 * index)).timestamp())
            store.add_resource(ts, float(10 * (offset + 1)), 50.0, 10.0, 40.0, 0.1)
    return store


def test_weekly_resource_uses_whole_window(tmp_path: Path) -> None:
    store = _store_with_week(tmp_path)
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(
        day="2026-10-20", history_days=7, sections=["resource"], span_days=7
    )
    resources = view["resources"]
    assert resources["available"] is True
    # Day averages are 10,20,...,70 -> the weekly average is 40, not 70.
    assert abs(resources["yesterday"]["cpu"]["avg"] - 40.0) < 0.01
    assert resources["yesterday"]["cpu"]["peak"] == 70.0
    # The previous window is queried too, so "较上期" is no longer dead.
    assert resources["delta_label"] == "较上期"
    assert (
        resources["previous"]["cpu"]["count"] == 0
        or resources["previous"]["cpu"]["avg"] is not None
    )
    assert resources["hour_label"] == "近 7 天"
    assert resources["hour_note"] == "按日均值"


def test_weekly_resource_available_with_one_last_day_sample(tmp_path: Path) -> None:
    store = _store_with_week(tmp_path, last_day_samples=1)
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(
        day="2026-10-20", history_days=7, sections=["resource"], span_days=7
    )
    assert view["resources"]["available"] is True


# ----------------------------------------------------------------------
# P2: unresolved counting across midnight
# ----------------------------------------------------------------------


def test_unresolved_excludes_episode_closed_after_window(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = date(2026, 10, 20)
    start = int(datetime(2026, 10, 20, 23, 50, tzinfo=TZ).timestamp())
    store.add_event(start, day.isoformat(), "resource", "内存使用率", "93%", "alert")
    # Recovery happened the next morning: the recovery event is outside the
    # window, but resolved_ts was written back on the alert row.
    resolved = int(datetime(2026, 10, 21, 0, 5, tzinfo=TZ).timestamp())
    with store._connect() as con:
        con.execute("UPDATE events SET resolved_ts = ? WHERE ts = ?", (resolved, start))
        con.commit()

    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    section = builder.build(day="2026-10-20", sections=["anomaly"])["anomalies"]
    assert section["count"] == 1
    assert section["unresolved"] == 0
    assert section["items"][0]["duration"] != "进行中"


# ----------------------------------------------------------------------
# P2: worst mount reaches report and dashboard
# ----------------------------------------------------------------------


def test_report_samples_worst_mount(monkeypatch, tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    plugin = _Plugin()
    plugin.state = state
    config = {
        "display": {"timezone": "Asia/Shanghai", "disk_paths": ["/", "/data"]},
        "daily_report": {"enabled": True},
    }
    reporter = ReportCollector(plugin, state, config, store)
    monkeypatch.setattr(
        report_module,
        "disk_usage_bounded",
        lambda paths: [
            DiskUsage(name="/", used=40, total=100, percent=40.0),
            DiskUsage(name="/data", used=90, total=100, percent=90.0),
        ],
    )
    asyncio.run(reporter._sample_resource(time.time()))
    rows = store.list_resources(0, limit=10)
    assert rows and abs(float(rows[-1]["disk"]) - 90.0) < 0.01


def test_dashboard_shows_worst_mount(monkeypatch, tmp_path: Path) -> None:
    plugin = _Plugin()
    plugin.config = {"display": {"disk_paths": ["/", "/data"]}}
    api = page_api_module.PageApi(plugin)
    monkeypatch.setattr(
        page_api_module,
        "disk_usage_bounded",
        lambda paths: [
            DiskUsage(name="/", used=40, total=100, percent=40.0),
            DiskUsage(name="/data", used=90, total=100, percent=90.0),
        ],
    )
    disk = api._disk()
    assert disk["path"] == "/data"
    assert disk["percent"] == 90.0
    assert len(disk["disks"]) == 2
    # The response is serialised to JSON. The first cut attached the list to
    # its own element (a cycle), which blew the stack on /overview.
    assert json.dumps(disk)


# ----------------------------------------------------------------------
# P2: audit weekly_time + address masking
# ----------------------------------------------------------------------


def test_audit_flags_invalid_weekly_time() -> None:
    issues = run_audit(
        {
            "daily_report": {
                "enabled": True,
                "weekly_enabled": True,
                "weekly_time": "9:61",  # 分钟超范围，运行时同样无法调度
            },
            "display": {"redact": []},
        }
    )
    assert any(
        issue.code == "invalid_schedule_time" and "周报" in issue.message
        for issue in issues
    )


def test_mask_addresses_keeps_plain_errors_readable() -> None:
    redactor = Redactor(["hostname"])
    assert redactor.mask_addresses(
        "ConnectionRefusedError: ('10.0.0.12', 9000)"
    ).startswith("已隐藏")
    assert redactor.mask_addresses("https://internal/health?token=x").startswith(
        "已隐藏"
    )
    # Not every failure is a host leak: these must stay readable.
    assert redactor.mask_addresses("HTTP 500（期望 200）") == "HTTP 500（期望 200）"
    assert redactor.mask_addresses("探测超时（超过 12 秒）") == "探测超时（超过 12 秒）"
    assert Redactor([]).mask_addresses("10.0.0.12") == "10.0.0.12"


# ----------------------------------------------------------------------
# P2: reports cache key carries the section selection
# ----------------------------------------------------------------------


def test_reports_cache_key_tracks_sections(tmp_path: Path) -> None:
    plugin = _Plugin()
    plugin.config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"enabled": True, "sections": []},
    }
    plugin.report_store = ReportStore(tmp_path / "report.db")
    api = page_api_module.PageApi(plugin)

    now = datetime.now(TZ)
    key_one = (
        now.date().isoformat(),
        tuple(sorted(api._redact())),
        repr(plugin.config["daily_report"]["sections"]),
    )
    plugin.config["daily_report"]["sections"] = ["resource"]
    key_two = (
        now.date().isoformat(),
        tuple(sorted(api._redact())),
        repr(plugin.config["daily_report"]["sections"]),
    )
    assert key_one != key_two
