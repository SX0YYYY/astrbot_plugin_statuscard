"""Regression tests for the v2.20.17 fix batch.

Every test drives the production code that changed -- the aggregator, the
two alert state machines, the report scheduler, the report view, the page
API and the state store -- rather than re-implementing its logic.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import collectors as collectors_mod
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore

TZ = timezone(timedelta(hours=8))


# ----------------------------------------------------------------------
# small shared doubles
# ----------------------------------------------------------------------


class MemoryState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.saves = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def setdefault(self, key, default):
        return self.data.setdefault(key, default)

    @contextmanager
    def section(self, key, default=None):
        section = self.data.setdefault(key, default if default is not None else {})
        if not isinstance(section, dict):
            section = {}
            self.data[key] = section
        yield section

    def save(self) -> None:
        self.saves += 1


class FakePlugin:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append({"targets": list(targets), "text": text, "source": source})
        if self.fail:
            return DeliveryResult(ok=False, failed_targets=list(targets))
        return DeliveryResult(ok=True, ok_targets=list(targets))


def _alert_config() -> dict:
    return {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "recover_notify": True,
        },
        "monitors": {
            "enabled": True,
            "interval_seconds": 60,
            "fail_threshold": 1,
            "cooldown_seconds": 1800,
            "recover_notify": True,
            "targets": ["123456"],
            "items": [
                {
                    "name": "网站",
                    "type": "http",
                    "url": "https://example.com/health",
                    "enabled": True,
                },
                {
                    "name": "网站",
                    "type": "http",
                    "url": "https://example.org/health",
                    "enabled": True,
                },
            ],
        },
    }


def _rule_state(**overrides) -> dict:
    state = {
        "label": "内存使用率",
        "alerting": True,
        "alerted": True,
        "alert_since": time.time() - 120,
        "last_seen": time.time(),
        "last_sent": time.time() - 60,
        "count": 0,
        "awaiting_summary": True,
        "summary_buffered": True,
    }
    state.update(overrides)
    return state


# ----------------------------------------------------------------------
# A. the delivered-summary callback
# ----------------------------------------------------------------------


def test_aggregator_calls_on_delivered_with_the_batch() -> None:
    async def run() -> None:
        delivered: list[list[tuple[str, str]]] = []
        agg = AlertAggregator(window_seconds=3600, max_items=8)

        async def send(text: str):
            return True

        await agg.push(
            "首条",
            "r0",
            "first",
            send,
            kind="k",
            on_delivered=lambda items: delivered.append(list(items)),
        )
        await agg.push(
            "第二条",
            "r1",
            "second",
            send,
            kind="k",
            on_delivered=lambda items: delivered.append(list(items)),
        )
        await agg.flush("k")
        assert delivered == [[("第二条", "r1")]]

    asyncio.run(run())


def test_aggregator_does_not_call_on_delivered_when_send_fails() -> None:
    async def run() -> None:
        delivered: list = []
        abandoned: list = []
        agg = AlertAggregator(window_seconds=3600, max_items=8)

        async def send(text: str):
            return DeliveryResult(ok=False, failed_targets=["x"])

        await agg.push("首条", "r0", "first", send, kind="k")
        await agg.push(
            "第二条",
            "r1",
            "second",
            send,
            kind="k",
            on_abandon=lambda items: abandoned.append(list(items)),
            on_delivered=lambda items: delivered.append(list(items)),
        )
        # Two retries, then abandoned.
        for _ in range(3):
            await agg.flush("k")
        assert delivered == []
        assert abandoned, "the batch was never abandoned"

    asyncio.run(run())


def test_delivered_summary_markers_do_not_survive_a_reload() -> None:
    """A summary that reached the chat must not be treated as lost."""

    state = MemoryState()
    engine = AlertEngine(
        FakePlugin(), state, _alert_config(), report_store=None, aggregator=None
    )
    with state.section("alerts") as alerts:
        alerts.setdefault("rules", {})["ram"] = _rule_state()

    engine._on_summary_delivered([("内存使用率", "95%")])
    rule = state.data["alerts"]["rules"]["ram"]
    assert "awaiting_summary" not in rule
    assert "summary_buffered" not in rule
    assert rule["alerted"] is True

    # The reload repair must now leave the rule announced.
    engine._mark_stale_rules()
    rule = state.data["alerts"]["rules"]["ram"]
    assert rule["alerted"] is True
    assert not rule.get("summary_abandoned")


def test_same_named_rule_that_delivered_directly_is_not_touched() -> None:
    state = MemoryState()
    engine = AlertEngine(
        FakePlugin(), state, _alert_config(), report_store=None, aggregator=None
    )
    with state.section("alerts") as alerts:
        alerts.setdefault("rules", {})["ram"] = _rule_state(
            summary_buffered=False, awaiting_summary=False
        )

    engine._on_summary_delivered([("内存使用率", "95%")])
    rule = state.data["alerts"]["rules"]["ram"]
    # The marker belongs to a different (buffered) episode; this rule keeps
    # its own state untouched.
    assert rule["alerted"] is True


# ----------------------------------------------------------------------
# B. monitor/alert symmetry for awaiting_summary
# ----------------------------------------------------------------------


def _monitor(recover_notify: bool = True, aggregator=None) -> ServiceMonitor:
    config = _alert_config()
    config["monitors"]["recover_notify"] = recover_notify
    return ServiceMonitor(
        FakePlugin(),
        MemoryState(),
        config,
        report_store=None,
        aggregator=aggregator,
    )


def test_monitor_pops_awaiting_summary_even_when_notice_is_off() -> None:
    monitor = _monitor(recover_notify=False)
    item = monitor.items[0]
    key = monitor._item_key(item)
    now = time.time()
    with monitor.state.section("monitors") as monitors:
        monitors.setdefault("items", {})[key] = {
            "name": "网站",
            "target": item["url"],
            "alerting": True,
            "alerted": True,
            "since": now - 120,
            "awaiting_summary": True,
            "summary_buffered": True,
            "last_sent": 0.0,
            "fails": 0,
        }

    async def probe_ok(it):
        return True, "HTTP 200", it["url"]

    monitor._probe = probe_ok  # type: ignore[assignment]
    asyncio.run(monitor._check_item(item))
    state = monitor.state.data["monitors"]["items"][key]
    assert "awaiting_summary" not in state


def test_stale_awaiting_without_buffered_recovery_is_not_queued() -> None:
    """An old marker must not hand a delivered alert's recovery to a summary."""

    async def run() -> None:
        monitor = _monitor(recover_notify=True, aggregator=AlertAggregator(3600, 8))
        item = monitor.items[1]
        key = monitor._item_key(item)
        now = time.time()
        with monitor.state.section("monitors") as monitors:
            monitors.setdefault("items", {})[key] = {
                "name": "网站",
                "target": item["url"],
                "alerting": True,
                "alerted": True,
                "since": now - 120,
                "awaiting_summary": True,  # left behind by an older version
                "summary_buffered": False,
                "last_sent": 0.0,
                "fails": 0,
            }

        async def probe_ok(it):
            return True, "HTTP 200", it["url"]

        monitor._probe = probe_ok  # type: ignore[assignment]
        # A same-named item opens an aggregation window with an undelivered
        # summary; it must not capture this recovery.
        agg = monitor._aggregator

        async def send(text: str):
            return True

        await agg.push("网站", "HTTP 500", "other", send, kind="service")
        before = len(monitor.plugin.calls)
        await monitor._check_item(item)
        recoveries = [
            call for call in monitor.plugin.calls[before:] if "服务恢复" in call["text"]
        ]
        assert len(recoveries) == 1

    asyncio.run(run())


# ----------------------------------------------------------------------
# C. maintenance silence boundaries
# ----------------------------------------------------------------------


def _silenced_monitor(monkeypatch, *, ok: bool) -> tuple[ServiceMonitor, dict, str]:
    monitor = _monitor()
    item = monitor.items[0]
    key = monitor._item_key(item)
    now = time.time()
    base = {
        "name": "网站",
        "target": item["url"],
        "alerting": False,
        "alerted": False,
        "since": 0,
        "silent_since": now - 600,
        "last_checked": now - 60,
        "fails": 0,
    }
    with monitor.state.section("monitors") as monitors:
        monitors.setdefault("items", {})[key] = base

    monkeypatch.setattr(monitor, "_is_silenced", lambda: False)

    async def probe(it):
        return (True, "HTTP 200", it["url"]) if ok else (False, "HTTP 500", it["url"])

    monitor._probe = probe  # type: ignore[assignment]
    return monitor, base, key


def test_fault_starting_inside_silence_keeps_its_real_start(monkeypatch) -> None:
    monitor, _, key = _silenced_monitor(monkeypatch, ok=False)
    asyncio.run(monitor._check_item(monitor.items[0]))
    state = monitor.state.data["monitors"]["items"][key]
    # The alert may have been pushed now, but the episode start is the
    # moment the fault began inside the window, not the push moment.
    assert time.time() - state["since"] > 500


def test_dirty_silent_since_does_not_abort_the_item(monkeypatch) -> None:
    monitor, _state, key = _silenced_monitor(monkeypatch, ok=False)
    with monitor.state.section("monitors") as monitors:
        monitors["items"][key]["silent_since"] = "oops"
    asyncio.run(monitor._check_item(monitor.items[0]))
    # The dirty value is consumed and the check still runs (a raised
    # ValueError would have left the item unchecked).
    assert "__no_such_field__" not in monitor.state.data["monitors"]["items"][key]


def test_silence_heal_clears_stale_so_next_recovery_is_announced(monkeypatch) -> None:
    monitor, _, key = _silenced_monitor(monkeypatch, ok=True)
    with monitor.state.section("monitors") as monitors:
        monitors["items"][key].update(
            {"alerting": True, "stale": True, "alerted": True}
        )
    asyncio.run(monitor._check_item(monitor.items[0]))
    state = monitor.state.data["monitors"]["items"][key]
    assert "stale" not in state


# ----------------------------------------------------------------------
# D. audit accepts the same silence formats as the runtime
# ----------------------------------------------------------------------


def test_audit_accepts_seconds_in_silence_until() -> None:
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {"enabled": False, "targets": []},
        "monitors": {
            "enabled": True,
            "silence_enabled": True,
            "silence_until": "2099-01-01 10:00:00",
            "targets": ["123456"],
            "items": [
                {
                    "name": "网站",
                    "type": "http",
                    "url": "https://example.com/health",
                    "enabled": True,
                }
            ],
        },
        "daily_report": {"enabled": False, "weekly_enabled": False},
    }
    codes = [issue.code for issue in run_audit(config)]
    assert "silence_time_invalid" not in codes


# ----------------------------------------------------------------------
# E. report view: orphan pairing, cross-window count, duration codes
# ----------------------------------------------------------------------


def _builder(tmp_path: Path) -> tuple[DailyReportBuilder, ReportStore]:
    store = ReportStore(tmp_path / "report.db")
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"sections": ["anomaly"]},
    }
    return DailyReportBuilder(store, config, db_path=tmp_path / "none.db"), store


def _add(store: ReportStore, when: datetime, status: str, name: str = "svc", **kw):
    store.add_event(
        int(when.timestamp()),
        when.date().isoformat(),
        "service",
        name,
        f"{status} {name}",
        status,
        **kw,
    )


def test_over_long_cross_window_recovery_is_not_rendered_as_days(tmp_path) -> None:
    builder, store = _builder(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=TZ)
    _add(store, day - timedelta(days=3, hours=-9), "alert")  # orphan, no recovery
    _add(
        store,
        day.replace(hour=10, minute=0),
        "alert",
        resolved_ts=int(day.replace(hour=10, minute=5).timestamp()),
    )
    _add(store, day.replace(hour=10, minute=5), "recovery")

    view = builder.build(day=day.date(), history_days=7, sections=["anomaly"])
    items = view["anomalies"]["items"]
    recovered = [item for item in items if item["status_key"] == "recovered"]
    # The start is an orphan three days back; its exact duration cannot be
    # trusted, so the recovery row uses the same guard as the alert row
    # instead of printing "3天01:05:00" (v2.20.17).
    assert recovered and recovered[0]["duration"].startswith("跨窗口关闭")


def test_historical_null_rows_of_one_episode_are_not_split(tmp_path) -> None:
    """Pre-v2.12 rows have no resolved_ts; they must still pair as ONE fault."""

    builder, store = _builder(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=TZ)
    _add(store, day.replace(hour=9, minute=0), "alert")  # no resolved_ts
    _add(store, day.replace(hour=15, minute=0), "alert")  # no resolved_ts
    _add(store, day.replace(hour=15, minute=5), "recovery")

    view = builder.build(day=day.date(), history_days=7, sections=["anomaly"])
    anomalies = view["anomalies"]
    assert anomalies["count"] == 1
    assert len(anomalies["items"]) == 1
    assert anomalies["items"][0]["duration"] == "06:05:00"


def test_engine_stamped_orphan_keeps_the_baseline_shape(tmp_path) -> None:
    """``close_all=True`` stamps the orphan too; do not split that episode."""

    builder, store = _builder(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=TZ)
    _add(store, day.replace(hour=9, minute=0), "alert")
    _add(store, day.replace(hour=15, minute=0), "alert")
    recovery_ts = int(day.replace(hour=15, minute=5).timestamp())
    _add(store, day.replace(hour=15, minute=5), "recovery")
    # Production calls resolve_event(close_all=True): every still-open row of
    # the rule gets the recovery's timestamp, including the older orphan.
    store.resolve_event(recovery_ts, "svc", recovery_ts, kind="service", close_all=True)

    view = builder.build(day=day.date(), history_days=7, sections=["anomaly"])
    anomalies = view["anomalies"]
    assert anomalies["count"] == 1
    assert len(anomalies["items"]) == 1
    assert anomalies["items"][0]["duration"] == "06:05:00"


def test_cross_window_recovery_is_counted(tmp_path) -> None:
    builder, store = _builder(tmp_path)
    day = datetime(2026, 11, 1, tzinfo=TZ)
    _add(store, day - timedelta(minutes=10), "alert")
    _add(store, day.replace(hour=0, minute=10), "recovery")

    view = builder.build(day=day.date(), history_days=7, sections=["anomaly"])
    anomalies = view["anomalies"]
    assert anomalies["count"] == 1
    assert anomalies["items"][0]["duration"] == "00:20:00（窗口前开始）"


def test_duration_codes_cover_window_close_and_ongoing() -> None:
    from astrbot_plugin_statuscard.report_view import _duration_code

    assert _duration_code("跨窗口关闭（窗口前开始）") == "cross_window"
    assert _duration_code("进行中") == "ongoing"
    assert _duration_code("00:05:00（窗口前开始）") == "started_before_window"


# ----------------------------------------------------------------------
# F. scheduler fixes
# ----------------------------------------------------------------------


def _reporter(**attrs) -> DailyReporter:
    reporter = object.__new__(DailyReporter)
    reporter.config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {"targets": ["123456"], "platform_id": "default"},
        "daily_report": {
            "enabled": True,
            "time": "21:00",
            "targets": [],
            "weekly_time": "21:30",
            "weekly_enabled": True,
        },
    }
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0
    reporter._daily_given_up_day = None
    reporter._daily_pause_until = 0.0
    reporter._last_attempt_day = None
    reporter._last_sent_day = None
    reporter._weekly_pending_report_day = None
    reporter._weekly_pending = None
    reporter._weekly_retry_count = 0
    reporter._weekly_given_up_week = None
    reporter._weekly_pending_recovery_week = None
    reporter._weekly_pending_recovery_since = 0.0
    for key, value in attrs.items():
        setattr(reporter, key, value)
    return reporter


def test_missed_midnight_window_is_backfilled(monkeypatch) -> None:
    reporter = _reporter()
    now = datetime(2026, 11, 3, 0, 30, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    reporter._last_attempt_day = "2026-11-01"
    reporter._last_sent_day = "2026-11-01"  # last delivered report
    # The report due on 11-02 21:00 is 11-01; pretend it was never sent.
    reporter._last_sent_day = "2026-10-31"
    assert reporter._daily_missed_window_day() == "2026-11-01"


def test_missed_window_expires_after_eight_and_a_half_hours(monkeypatch) -> None:
    reporter = _reporter()
    now = datetime(2026, 11, 3, 7, 0, tzinfo=TZ)  # 10h after the 11-02 slot
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    reporter._last_attempt_day = "2026-11-01"
    reporter._last_sent_day = "2026-10-31"
    assert reporter._daily_missed_window_day() is None


def test_weekly_retry_pins_the_slot_report_day(monkeypatch) -> None:
    reporter = _reporter()
    now = datetime(2026, 11, 2, 0, 20, tzinfo=TZ)  # Monday after the slot
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    reporter._weekly_pending_report_day = None
    assert reporter._schedule_weekly_retry() is True
    # Sunday 11-01 21:30 slot -> report day Saturday 10-31, never Monday's
    # "yesterday" (Sunday) after a midnight reload.
    assert reporter._weekly_pending_report_day == "2026-10-31"


def test_give_up_clears_the_offline_recovery_pin() -> None:
    reporter = _reporter()
    reporter._pending_recovery_day = "2026-11-01"
    reporter._pending_recovery_since = time.time()
    reporter._pending_retry_count = 3
    reporter._pending_retry_day = "2026-11-01"
    assert reporter._schedule_retry("2026-11-01") is False
    assert reporter._daily_given_up_day == "2026-11-01"
    assert reporter._pending_recovery_day is None


def test_backfill_is_attempted_once_per_report_day(monkeypatch) -> None:
    reporter = _reporter()
    now = datetime(2026, 11, 3, 0, 30, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    reporter._last_attempt_day = "2026-11-01"
    reporter._last_sent_day = "2026-10-31"
    assert reporter._daily_missed_window_day() == "2026-11-01"
    reporter._daily_backfill_day = "2026-11-01"
    # A transport outage keeps the report queued; this process must not
    # rebuild and re-render it every 15 minutes until 05:30 (v2.20.17).
    assert reporter._daily_missed_window_day() is None


def test_backfill_skips_an_empty_section_selection(monkeypatch) -> None:
    reporter = _reporter()
    now = datetime(2026, 11, 3, 0, 30, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    reporter._last_attempt_day = "2026-11-01"
    reporter._last_sent_day = "2026-10-31"
    reporter._sections = list  # type: ignore[assignment]
    assert reporter._daily_missed_window_day() is None


def test_backfill_offline_failure_does_not_start_the_15_minute_poll() -> None:
    reporter = _reporter()
    reporter._save_delivery_state = lambda: None  # type: ignore[assignment]
    assert (
        reporter._schedule_retry(
            "2026-11-01", transport_offline=True, poll_offline=False
        )
        is True
    )
    assert reporter._pending_retry is None
    assert reporter._pending_recovery_day == "2026-11-01"

    # The normal 21:00 slot keeps the historical fallback polling.
    reporter._pending_recovery_day = None
    assert reporter._schedule_retry("2026-11-01", transport_offline=True) is True
    assert reporter._pending_retry is not None


# ----------------------------------------------------------------------
# G. page API
# ----------------------------------------------------------------------


class _ApiPlugin:
    def __init__(self, store: ReportStore, config: dict | None = None) -> None:
        self.report_store = store
        self.config = config or {
            "display": {"timezone": "Asia/Shanghai", "page_redact": ["hostname"]},
            "monitors": {"enabled": True, "items": []},
        }
        self._issues: list = []

    def audit_issues(self):
        return self._issues


def _event_rows(store: ReportStore, count: int, day: str = "2026-11-01") -> None:
    base = int(datetime(2026, 11, 1, 0, 0, tzinfo=TZ).timestamp())
    for index in range(count):
        store.add_event(base + index, day, "service", f"svc{index}", "x", "alert")


def test_events_truncation_keeps_the_newest_row(tmp_path, monkeypatch) -> None:
    store = ReportStore(tmp_path / "report.db")
    _event_rows(store, 2005)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.page_api.now_in_timezone",
        lambda cfg=None: datetime(2026, 11, 1, 23, 0, tzinfo=TZ),
    )
    api = PageApi(_ApiPlugin(store))
    payload = asyncio.run(api.events())
    assert payload["counts_truncated"] is True
    # Items are newest-first; the truncation must keep the newest row.
    assert payload["items"][0]["name"] == "svc2004"


def test_reports_reject_an_ancient_day(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    api = PageApi(_ApiPlugin(store))
    # ``0001-01-01`` passes date.fromisoformat but used to overflow the
    # previous-window arithmetic and return "构建失败".
    assert api._valid_report_day("0001-01-01") is False
    assert api._valid_report_day("2026-11-01") is True


def test_audit_messages_are_masked_on_the_dashboard(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _ApiPlugin(store)
    plugin._issues = [
        SimpleNamespace(
            level="warning",
            code="monitor_item_config",
            message="监测项「https://nas.local/health」缺少 url 配置",
            as_dict=lambda: {
                "level": "warning",
                "code": "monitor_item_config",
                "message": "监测项「https://nas.local/health」缺少 url 配置",
            },
        )
    ]
    api = PageApi(plugin)
    masked = api._mask_audit(plugin.audit_issues())
    assert "nas.local" not in masked[0]["message"]


# ----------------------------------------------------------------------
# H. collector / lifecycle
# ----------------------------------------------------------------------


def test_disk_probes_use_daemon_threads(monkeypatch) -> None:
    """v2.20.30 replaced the never-shut-down executor with daemon threads.

    A non-daemon worker stuck in statvfs was joined at interpreter exit, so
    one hung NFS mount made every AstrBot restart wait for the systemd stop
    timeout. The probe thread must be a daemon and the caller must still get
    a normal result.
    """

    created: list[bool] = []

    class FakeThread:
        def __init__(self, target, args, name, daemon):
            created.append(bool(daemon))
            self._target = target
            self._args = args

        def start(self) -> None:
            self._target(*self._args)

    collectors_mod._DISK_FAIL_COUNT.clear()
    collectors_mod._DISK_DEAD_UNTIL.clear()
    monkeypatch.setattr(collectors_mod, "_DISK_FAIL_COUNT", {})
    monkeypatch.setattr(collectors_mod, "_DISK_DEAD_UNTIL", {})
    monkeypatch.setattr(collectors_mod.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        collectors_mod,
        "_disk_usage_one",
        lambda name: collectors_mod.DiskUsage(name=name, used=1, total=2, percent=50.0),
    )
    result = collectors_mod.disk_usage_bounded(["/healthy"])
    assert created == [True]
    assert result[0].percent == 50.0
    assert "/healthy" not in collectors_mod._DISK_FAIL_COUNT


def test_hung_disk_probe_returns_within_the_timeout(monkeypatch) -> None:
    """A worker that never finishes must not hold the caller."""

    def hang(name: str):
        time.sleep(30)
        return collectors_mod.DiskUsage(
            name=name, used=None, total=None, percent=None, exception="读取超时"
        )

    collectors_mod._DISK_FAIL_COUNT.clear()
    collectors_mod._DISK_DEAD_UNTIL.clear()
    monkeypatch.setattr(collectors_mod, "_DISK_FAIL_COUNT", {})
    monkeypatch.setattr(collectors_mod, "_DISK_DEAD_UNTIL", {})
    monkeypatch.setattr(collectors_mod, "_disk_usage_one", hang)
    started = time.monotonic()
    result = collectors_mod.disk_usage_bounded(["/hung"], timeout=0.05)
    assert time.monotonic() - started < 1.0
    assert result[0].exception == "读取超时"


def test_state_store_close_blocks_late_saves(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.set("marker", "new")
    store.save()
    before = path.read_text(encoding="utf-8")
    store.close()
    store.set("marker", "stale")
    store.save()
    assert path.read_text(encoding="utf-8") == before


def test_daily_send_with_empty_targets_skips_the_build(monkeypatch) -> None:
    reporter = _reporter()
    reporter._sections = lambda: ["resource"]  # type: ignore[assignment]
    reporter._targets = lambda kind="daily": []  # type: ignore[assignment]
    reporter._record = lambda *args, **kwargs: None  # type: ignore[assignment]
    reporter._clear_retry = lambda **kwargs: None  # type: ignore[assignment]
    reporter._save_delivery_state = lambda: None  # type: ignore[assignment]
    reporter.store = SimpleNamespace(probe=lambda: True, ensure_ready=lambda: True)
    called = {"build": 0}

    def build(*args, **kwargs):
        called["build"] += 1
        return {}

    reporter._build_view = build  # type: ignore[assignment]
    asyncio.run(reporter._send_scheduled("2026-11-01"))
    assert called["build"] == 0


def test_sampler_switches_away_from_an_interface_that_lost_its_link(
    tmp_path, monkeypatch
) -> None:
    from astrbot_plugin_statuscard.sampler import Sampler

    state = StateStore(tmp_path / "state.json")
    now = time.time()
    state.set(
        "net",
        {
            "interface": "eth0",
            "last_ts": now - 60,
            "last_sent": 1000,
            "last_recv": 2000,
            "month": time.strftime("%Y-%m"),
            "month_up": 0,
            "month_down": 0,
        },
    )
    sampler = Sampler(state, config={"display": {"timezone": "Asia/Shanghai"}})

    counters = {
        "eth0": SimpleNamespace(bytes_sent=1000, bytes_recv=2000),
        "wlan0": SimpleNamespace(bytes_sent=5000, bytes_recv=9000),
    }
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.sampler.psutil.net_io_counters",
        lambda pernic=False: (
            counters if pernic else SimpleNamespace(bytes_sent=6000, bytes_recv=11000)
        ),
    )
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.sampler.psutil.net_if_stats",
        lambda: {
            "eth0": SimpleNamespace(isup=False),
            "wlan0": SimpleNamespace(isup=True),
        },
    )
    sampler._sample_network(now)
    net = state.get("net")
    assert net["interface"] == "wlan0"


def test_large_avatar_is_downscaled_before_embedding(tmp_path) -> None:
    from io import BytesIO

    from astrbot_plugin_statuscard.collectors import fetch_avatar_bytes
    from PIL import Image

    upload_dir = tmp_path / "files" / "avatar"
    upload_dir.mkdir(parents=True)
    path = upload_dir / "big.png"
    # Noise makes the PNG genuinely large; a flat colour compresses to a few
    # KB and would not exercise the shrink path.
    Image.effect_noise((1200, 1200), 100).convert("RGB").save(path)
    assert path.stat().st_size > 200_000
    config = {
        "avatar": {
            "avatar_source": "upload",
            "avatar_upload_files": ["files/avatar/big.png"],
        }
    }
    payload = asyncio.run(fetch_avatar_bytes({}, config, data_dir=tmp_path))
    assert payload is not None
    assert len(payload) < path.stat().st_size
    with Image.open(BytesIO(payload)) as image:
        assert image.width <= 512
