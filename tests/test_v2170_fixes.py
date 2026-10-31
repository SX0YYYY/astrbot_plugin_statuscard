"""Regression tests for the v2.17.0 fixes (three batches in one release)."""

from __future__ import annotations

import asyncio
import copy
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import astrbot_plugin_statuscard.collectors as collectors_module
import astrbot_plugin_statuscard.delivery as delivery_module
import astrbot_plugin_statuscard.sampler as sampler_module
import astrbot_plugin_statuscard.state as state_module
from astrbot_plugin_statuscard import page_api
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import resolve_targets
from astrbot_plugin_statuscard.delivery import DeliveryResult, DeliveryService
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.redactor import HIDDEN, Redactor
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.sampler import Sampler
from astrbot_plugin_statuscard.state import StateStore

TARGET = "default:GroupMessage:123456789"


class FakePlugin:
    def __init__(self, config):
        self.config = config
        self.texts: list[tuple[list[str], str]] = []
        self.images: list[tuple[list[str], str]] = []

    async def send_text(self, targets, text, source="unknown"):
        self.texts.append((list(targets), text))
        return DeliveryResult(ok=True, ok_targets=list(targets))

    async def send_image(self, targets, image, source="unknown"):
        self.images.append((list(targets), image))
        return DeliveryResult(ok=True, ok_targets=list(targets))

    async def build_status_image(self):
        return "https://example.com/status.jpg"

    async def build_summary_text(self):
        return "summary"


def _ok_send(sink: list[str]):
    async def send(text):
        sink.append(text)
        return SimpleNamespace(ok=True, uncertain_targets=[])

    return send


# ----------------------------------------------------------------------
# notification center gating
# ----------------------------------------------------------------------


def test_monitor_probes_when_only_notify_rules_have_target(tmp_path: Path) -> None:
    config = {
        "alerts": {"targets": [], "platform_id": "default"},
        "monitors": {
            "enabled": True,
            "targets": [],
            "items": [
                {
                    "__template_key": "http",
                    "name": "probe",
                    "url": "http://127.0.0.1:9/",
                    "enabled": True,
                }
            ],
        },
        "notify": {
            "rules": [{"target": TARGET, "kinds": ["service"], "enabled": True}]
        },
    }
    monitor = ServiceMonitor(
        FakePlugin(config), StateStore(tmp_path / "state.json"), config
    )
    calls: list[str] = []

    async def fake_check(item):
        calls.append(str(item.get("name")))

    monitor._check_item = fake_check
    asyncio.run(monitor.check_all())
    assert calls == ["probe"]


def test_daily_summary_uses_notify_rule_target(tmp_path: Path) -> None:
    config = {
        "alerts": {
            "targets": [],
            "enabled": True,
            "daily_summary": {"enabled": True, "with_image": True},
        },
        "notify": {
            "rules": [{"target": TARGET, "kinds": ["daily_image"], "enabled": True}]
        },
        "display": {"timezone": "Asia/Shanghai"},
    }
    plugin = FakePlugin(config)
    engine = AlertEngine(plugin, StateStore(tmp_path / "state.json"), config)
    assert asyncio.run(engine._send_daily_summary()) is True
    assert plugin.images == [([TARGET], "https://example.com/status.jpg")]


def test_offline_check_runs_when_notifications_disabled(tmp_path: Path) -> None:
    config = {
        "alerts": {"offline_notify": False, "enabled": False},
        "display": {"timezone": "Asia/Shanghai"},
    }
    engine = AlertEngine(
        FakePlugin(config), StateStore(tmp_path / "state.json"), config
    )
    called: list[int] = []

    async def fake_offline():
        called.append(1)

    async def noop(*args, **kwargs):
        return None

    engine._check_offline = fake_offline
    engine._evaluate = noop
    engine._check_provider_quality = noop
    asyncio.run(engine._sample_once())
    assert called == [1]


# ----------------------------------------------------------------------
# audit / verify / redaction
# ----------------------------------------------------------------------


def _notify_config() -> dict:
    return {
        "alerts": {
            "enabled": True,
            "targets": [],
            "daily_summary": {"enabled": True},
            "platform_id": "default",
        },
        "monitors": {"enabled": True, "targets": [], "items": []},
        "notify": {
            "rules": [
                {"target": TARGET, "kinds": ["resource"], "enabled": True},
                {"target": TARGET, "kinds": ["service"], "enabled": True},
                {"target": TARGET, "kinds": ["daily_image"], "enabled": True},
            ]
        },
    }


def test_audit_does_not_report_missing_targets_when_center_covers_them() -> None:
    codes = {issue.code for issue in run_audit(_notify_config())}
    assert "alerts_no_targets" not in codes
    assert "monitors_no_targets" not in codes
    assert "daily_summary_no_targets" not in codes


def test_audit_still_reports_missing_targets_without_rules() -> None:
    config = {
        "alerts": {
            "enabled": True,
            "targets": [],
            "daily_summary": {"enabled": True},
        },
        "monitors": {"enabled": True, "targets": [], "items": []},
    }
    codes = {issue.code for issue in run_audit(config)}
    assert {"alerts_no_targets", "monitors_no_targets"} <= codes
    assert "daily_summary_no_targets" in codes


def test_verify_targets_falls_back_per_kind(monkeypatch) -> None:
    """Only the kinds a rule covers use the rule; the rest keep their legacy
    target, exactly like the real send path. The old code skipped every
    legacy target as soon as one rule existed."""

    config = {
        "alerts": {"targets": ["default:GroupMessage:333"], "platform_id": "default"},
        "monitors": {"targets": ["default:GroupMessage:444"]},
        "daily_report": {"targets": ["default:GroupMessage:555"]},
        "notify": {
            "rules": [
                {
                    "target": "default:GroupMessage:111",
                    "kinds": ["resource"],
                    "enabled": True,
                },
                {
                    "target": "default:GroupMessage:222",
                    "kinds": ["resource"],
                    "enabled": False,
                },
            ]
        },
    }
    plugin = FakePlugin(config)
    api = page_api.PageApi(plugin)
    monkeypatch.setattr(page_api, "_query_value", lambda key, default=None: "targets")
    asyncio.run(api.verify())
    sent = plugin.texts[-1][0]
    assert sent[0] == "default:GroupMessage:111"
    assert set(sent) == {
        "default:GroupMessage:111",
        "default:GroupMessage:333",
        "default:GroupMessage:444",
        "default:GroupMessage:555",
    }
    assert "default:GroupMessage:222" not in sent


def test_network_redaction_hides_sysinfo_interface() -> None:
    view = {
        "network": {
            "up": 1,
            "down": 2,
            "up_total": 3,
            "down_total": 4,
            "month_up": 5,
            "month_down": 6,
            "interface": "eth0",
            "chart": {},
        },
        "system_info": [{"label": "Network", "value": "eth0"}],
        "overview": [],
        "process": {"top": []},
        "bot": {},
    }
    Redactor(["network"]).apply_card(view)
    assert view["network"]["interface"] == HIDDEN
    assert view["system_info"][0]["value"] == HIDDEN


def test_process_redaction_hides_pid(monkeypatch) -> None:
    config = {"display": {"page_redact": ["process"]}}
    api = page_api.PageApi(FakePlugin(config))
    monkeypatch.setattr(
        page_api,
        "process_info",
        lambda top_n: {
            "top": [{"name": "secret", "pid": 4242, "cpu_raw": 1.0, "mem_mb": 2.0}]
        },
    )
    rows = asyncio.run(api._latest_processes(5))
    assert rows[0]["name"] == HIDDEN
    assert rows[0]["pid"] is None


# ----------------------------------------------------------------------
# aggregator
# ----------------------------------------------------------------------


def test_aggregator_keeps_kinds_separate() -> None:
    async def main():
        agg = AlertAggregator(window_seconds=60, max_items=8)
        resource: list[str] = []
        service: list[str] = []
        send_resource = _ok_send(resource)
        send_service = _ok_send(service)
        await agg.push("r1", "x", "resource first", send_resource, kind="resource")
        await agg.push("s1", "x", "service first", send_service, kind="service")
        await agg.push("r2", "x", "resource second", send_resource, kind="resource")
        await agg.push("s2", "x", "service second", send_service, kind="service")
        await agg.flush("resource")
        await agg.flush("service")
        await agg.stop()
        return resource, service

    resource, service = asyncio.run(main())
    assert any("r2: x" in item for item in resource)
    assert not any("s2: x" in item for item in resource)
    assert any("s2: x" in item for item in service)
    assert not any("r2: x" in item for item in service)


def test_aggregator_reschedules_after_slow_summary() -> None:
    async def main():
        agg = AlertAggregator(window_seconds=1, max_items=8)
        sent: list[str] = []

        async def send(text):
            sent.append(text)
            if text.startswith("⚠️ 告警汇总"):
                await asyncio.sleep(1.2)
            return SimpleNamespace(ok=True, uncertain_targets=[])

        await agg.push("A", "x", "first", send)
        await asyncio.sleep(0.2)
        await agg.push("B", "x", "buffered", send)
        await asyncio.sleep(1.0)  # flush starts, sleeps 1.2s
        await agg.push("C", "x", "opens new window", send)
        await asyncio.sleep(0.2)
        await agg.push("D", "x", "new buffered", send)
        await asyncio.sleep(2.6)
        await agg.stop()
        return sent

    sent = asyncio.run(main())
    assert any("D: x" in item for item in sent), sent


def test_aggregator_stop_cancels_flush() -> None:
    async def main():
        agg = AlertAggregator(window_seconds=60, max_items=8)
        await agg.push("A", "x", "first", _ok_send([]))
        task = agg._buckets["default"].flush_task
        assert task is not None and not task.done()
        await agg.stop()
        assert task.cancelled() or task.done()
        return agg._buckets["default"].flush_task

    assert asyncio.run(main()) is None


# ----------------------------------------------------------------------
# reports
# ----------------------------------------------------------------------


def _token_db(path: Path, rows: list[tuple]) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE provider_stats (
            created_at TEXT,
            status TEXT,
            provider_model TEXT,
            token_input_other INTEGER,
            token_input_cached INTEGER,
            token_output INTEGER
        )
        """
    )
    con.executemany("INSERT INTO provider_stats VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_token_success_rate_excludes_aborted(tmp_path: Path) -> None:
    db = _token_db(
        tmp_path / "rate.db",
        [
            ("2026-10-07 01:00:00", "completed", "m", 1, 0, 0),
            ("2026-10-07 02:00:00", "completed", "m", 1, 0, 0),
            ("2026-10-07 03:00:00", "completed", "m", 1, 0, 0),
            ("2026-10-07 04:00:00", "aborted", "m", 1, 0, 0),
        ],
    )
    builder = DailyReportBuilder(None, {"display": {"timezone": "Asia/Shanghai"}})
    builder._readonly_db = lambda: sqlite3.connect(db)
    section = builder._token_section(date(2026, 10, 7), 7)
    assert section["success_text"] == "100.0%"


def test_token_truncation_marks_oldest_day_as_gap(tmp_path: Path) -> None:
    rows = []
    for index in range(1000):
        rows.append(
            (
                f"2026-10-01 {index // 60 % 24:02d}:{index % 60:02d}:00",
                "completed",
                "m",
                1,
                0,
                0,
            )
        )
    for index in range(20000):
        day = 2 + index % 6
        rows.append(
            (
                f"2026-10-{day:02d} {index // 60 % 24:02d}:{index % 60:02d}:00",
                "completed",
                "m",
                1,
                0,
                0,
            )
        )
    db = _token_db(tmp_path / "trunc.db", rows)
    builder = DailyReportBuilder(None, {"display": {"timezone": "Asia/Shanghai"}})
    builder._readonly_db = lambda: sqlite3.connect(db)
    section = builder._token_section(date(2026, 10, 7), 7)
    chart = section["trend"]
    left = float(chart["left"])
    first_segment = chart["series"][0]["segments"][0]
    assert float(first_segment.split(",", 1)[0]) > left
    assert "仅统计最近部分" in section["note"]


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def list_resources(self, start_ts, limit=100000):
        return [row for row in self.rows if row["ts"] >= start_ts][:limit]

    def first_resource_ts(self):
        return self.rows[0]["ts"] if self.rows else None


def test_weekly_resource_trend_includes_report_day() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    rows = []
    for day in range(17, 25):
        for hour in range(0, 24, 2):
            rows.append(
                {
                    "ts": int(datetime(2026, 10, day, hour, tzinfo=tz).timestamp()),
                    "cpu": 10.0,
                    "ram": 20.0,
                    "swap": 1.0,
                    "disk": 30.0,
                    "load1": 0.1,
                }
            )
    builder = DailyReportBuilder(
        FakeStore(rows), {"display": {"timezone": "Asia/Shanghai"}}
    )
    section = builder._resource_section(date(2026, 10, 24), date(2026, 10, 17), 7)
    labels = [item["text"] for item in section["charts"]["daily"]["x_labels"]]
    assert "10-23" in labels and "10-24" in labels
    assert "10-17" not in labels


def test_weekly_guard_uses_weekly_sections() -> None:
    reporter = object.__new__(DailyReporter)
    reporter.config = {"daily_report": {"sections": [], "weekly_sections": ["token"]}}
    reporter.store = SimpleNamespace(available=False, ensure_ready=lambda: False)
    called: list[int] = []

    async def fake_notify(targets):
        called.append(1)

    reporter._notify_store_unavailable = fake_notify
    asyncio.run(reporter._send_weekly_scheduled())
    # The old guard used daily sections and returned before this point.
    assert called == [1]


def test_unresolved_includes_failed_only_incident(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = int(time.time())
    store.add_event(now - 300, "2026-10-28", "offline", "Bot 连接", "掉线", "failed")
    store.add_event(now - 200, "2026-10-28", "resource", "CPU 使用率", "95%", "alert")
    rows = store.list_unresolved_alerts(now + 10, now - 1000)
    names = {row["name"] for row in rows}
    assert "Bot 连接" in names and "CPU 使用率" in names


def test_list_events_keeps_newest_when_limited(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    for ts in (100, 200, 300):
        store.add_event(ts, "2026-10-28", "service", f"n{ts}", "x", "alert")
    rows = store.list_events("2026-10-28", limit=2)
    assert [row["ts"] for row in rows] == [200, 300]


# ----------------------------------------------------------------------
# probes / collectors / delivery
# ----------------------------------------------------------------------


def test_blank_required_field_is_config_error(tmp_path: Path) -> None:
    monitor = ServiceMonitor(
        FakePlugin({}), StateStore(tmp_path / "state.json"), {"monitors": {"items": []}}
    )
    items = monitor._normalize_items(
        [{"__template_key": "http", "name": "blank", "url": "   ", "enabled": True}]
    )
    assert "config_error" in items[0]


def test_empty_website_list_disables_probe() -> None:
    assert collectors_module._normalize_website_targets([]) == []
    assert collectors_module._normalize_website_targets(None)


def test_sampler_prime_survives_counter_failure(monkeypatch, tmp_path: Path) -> None:
    def boom():
        raise RuntimeError("no counters")

    monkeypatch.setattr(sampler_module.psutil, "net_io_counters", boom)
    sampler = Sampler(StateStore(tmp_path / "state.json"))
    sampler._prime()  # must not raise
    # No zero baseline is invented when the first read fails: the next
    # successful sample would otherwise book every byte since boot as one
    # interval (a fake multi-GB/s spike and a polluted month counter).
    assert "last_sent" not in sampler.state.get("net", {})
    assert "last_recv" not in sampler.state.get("net", {})


def test_local_image_skips_onebot_direct(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    async def fake_direct(client, target, segments):
        calls.append("direct")
        raise AssertionError("local image must not use the direct path")

    monkeypatch.setattr(delivery_module, "send_direct", fake_direct)
    sent: list[object] = []

    async def send_message(target, chain):
        sent.append((target, chain))
        return True

    context = SimpleNamespace(send_message=send_message)
    service = DeliveryService(
        context,
        StateStore(tmp_path / "state.json"),
        lambda: object(),
        lambda payload: payload,
    )
    result = asyncio.run(
        service.send(
            ["default:GroupMessage:1"],
            {"image": str(tmp_path / "card.png")},
            kind="image",
            source="test",
        )
    )
    assert calls == []
    assert result.ok and len(sent) == 1


def test_daily_catch_up_needs_previous_marker() -> None:
    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._last_sent_day = None
    reporter._last_attempt_day = None
    reporter._last_attempt_wall_day = None
    reporter._daily_given_up_day = None
    assert reporter._daily_catch_up_allowed("2026-10-28") is False
    reporter._last_attempt_day = "2026-10-27"
    reporter._last_attempt_wall_day = "2026-10-27"
    assert reporter._daily_catch_up_allowed("2026-10-28") is True
    reporter._last_attempt_day = "2026-10-28"
    reporter._last_attempt_wall_day = "2026-10-28"
    assert reporter._daily_catch_up_allowed("2026-10-28") is False


def test_daily_catch_up_uses_wall_day_not_report_day() -> None:
    """A cross-midnight retry must not mark *today* as attempted.

    ``_last_attempt_day`` names the report day (yesterday for the scheduled
    run), so the catch-up guard has to consult the calendar day of the last
    attempt instead. Using the report day re-armed the catch-up and made the
    report loop re-send all night long.
    """

    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._last_sent_day = None
    reporter._daily_given_up_day = None
    # The retry ran after midnight but delivered yesterday's report.
    reporter._last_attempt_day = "2026-10-30"
    reporter._last_attempt_wall_day = "2026-10-31"
    assert reporter._daily_catch_up_allowed("2026-10-31") is False
    # A fresh day is still allowed.
    assert reporter._daily_catch_up_allowed("2026-11-01") is True


def test_daily_given_up_blocks_catch_up() -> None:
    """After the retry budget is exhausted the day must not restart."""

    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._last_sent_day = None
    reporter._last_attempt_day = "2026-10-30"
    reporter._last_attempt_wall_day = "2026-10-30"
    reporter._daily_given_up_day = "2026-10-31"
    assert reporter._daily_catch_up_allowed("2026-10-31") is False


def test_state_save_retries_snapshot(monkeypatch, tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.data["race"] = {index: index for index in range(100)}
    real_deepcopy = copy.deepcopy
    calls = {"count": 0}

    def flaky(value):
        if calls["count"] == 0:
            calls["count"] += 1
            raise RuntimeError("dictionary changed size during iteration")
        return real_deepcopy(value)

    monkeypatch.setattr(state_module.copy, "deepcopy", flaky)
    store.save()
    assert calls["count"] == 1
    assert (tmp_path / "state.json").is_file()


def test_package_failure_uses_short_retry_ttl(monkeypatch, tmp_path: Path) -> None:
    collectors_module._pkg_cache.clear()
    monkeypatch.setattr(collectors_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(collectors_module, "_run_command", lambda *args, **kwargs: None)
    # pip is counted by scanning *.dist-info now, not by running pip.
    monkeypatch.setattr(collectors_module, "_pip_package_count", lambda: None)
    state = StateStore(tmp_path / "state.json")
    assert collectors_module._package_counts(state) == (None, None)
    stamp = collectors_module._pkg_cache["pkg_ts"]
    assert stamp < time.time() - 3600


def test_notify_target_resolution_is_per_kind() -> None:
    config = {
        "notify": {"rules": [{"target": TARGET, "kinds": ["service"], "enabled": True}]}
    }
    assert resolve_targets(config, "service") == [TARGET]
    assert resolve_targets(config, "resource") is None
