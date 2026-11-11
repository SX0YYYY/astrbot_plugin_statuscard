"""Regression tests for the v2.20.24 audit batch.

Every test drives the production path that changed. The batch focuses on:

* an offline retry ladder that never ended (15-minute attempts forever);
* the "cannot confirm the online state" warning leaving no event and no
  recovery, so the report could neither show it nor close it;
* unclosed alert rows of ONE flapping rule spending the whole unresolved
  budget and hiding newer outages from the report;
* a day with more than 5000 events under-counting its incidents without
  telling the operator;
* the cold dashboard report cache building once per concurrent request.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.collectors import now_in_timezone
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder, build_report_text
from astrbot_plugin_statuscard.utils import (
    OFFLINE_RETRY_BASE_SECONDS,
    OFFLINE_RETRY_SLOW_SECONDS,
    offline_retry_delay,
)


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


class ScriptedPlugin:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = None
        self.state = None

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append(text)
        return DeliveryResult(ok=True, ok_targets=list(targets))


def _make_engine(tmp_path, config=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {"alerts": {"targets": ["default:GroupMessage:1"]}}
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    engine = AlertEngine(plugin, state, cfg, store)
    return engine, state, store, plugin


def _make_monitor(tmp_path, items, config=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {
        "display": {"timezone": "Asia/Shanghai"},
        "monitors": {"enabled": True, "fail_threshold": 1, "items": items},
    }
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    monitor = ServiceMonitor(plugin, state, cfg, store, None)
    return monitor, state, store, plugin


def _reporter(tmp_path):
    reporter = DailyReporter(
        object(),
        ReportStore(tmp_path / "report.db"),
        {"display": {}, "daily_report": {"enabled": True}},
    )
    reporter._save_delivery_state = lambda: None  # type: ignore[attr-defined]
    return reporter


# ----------------------------------------------------------------------
# 1) offline retry delay: 15 minutes for a short reconnect, one hour later
# ----------------------------------------------------------------------


def test_offline_retry_delay_backs_off_after_four_attempts() -> None:
    assert [offline_retry_delay(n) for n in range(1, 5)] == [
        OFFLINE_RETRY_BASE_SECONDS
    ] * 4
    assert offline_retry_delay(5) == OFFLINE_RETRY_SLOW_SECONDS
    assert offline_retry_delay(96) == OFFLINE_RETRY_SLOW_SECONDS


def test_resource_alert_offline_retry_backs_off(tmp_path) -> None:
    engine, state, _store, _plugin = _make_engine(
        tmp_path, {"alerts": {"sustain_samples": 1, "cooldown_seconds": 0}}
    )
    rule = {
        "alerting": True,
        "alerted": False,
        "label": "CPU 使用率",
        "count": 1,
        "last_sent": 0,
        "retry_count": 1,
        "next_attempt": time.time() - 1,
    }
    state.data["alerts"] = {"offline": {"alerting": True}, "rules": {"cpu": rule}}

    async def fail_send(text, source="alert", kind="resource"):
        return DeliveryResult(ok=False)

    engine._send = fail_send
    delays = []
    for _ in range(5):
        rule["next_attempt"] = time.time() - 1
        asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))
        delays.append(rule["next_attempt"] - time.time())

    assert rule["offline_retry_count"] == 5
    assert rule["retry_count"] == 1  # the offline ladder spends no budget
    assert delays[0] < OFFLINE_RETRY_BASE_SECONDS + 60
    assert delays[4] > OFFLINE_RETRY_SLOW_SECONDS - 120

    async def ok_send(text, source="alert", kind="resource"):
        return DeliveryResult(ok=True)

    engine._send = ok_send
    rule["next_attempt"] = time.time() - 1
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))
    assert "offline_retry_count" not in rule


def test_monitor_alert_offline_retry_backs_off(tmp_path) -> None:
    item = {
        "__template_key": "http",
        "name": "API",
        "url": "http://127.0.0.1:9/health",
    }
    monitor, state, _store, _plugin = _make_monitor(tmp_path, [item])
    key = ServiceMonitor._item_key(monitor.items[0])
    state.data["alerts"] = {"offline": {"alerting": True}}
    state.data["monitors"] = {
        "items": {
            key: {
                "alerting": True,
                "alerted": True,
                "pending": True,
                "fails": 0,
                "since": time.time() - 60,
                "next_attempt": time.time() - 1,
                "name": "API",
                "target": "http://127.0.0.1:9/health",
            }
        }
    }

    async def fake_probe(_item):
        return False, "连接失败", "http://127.0.0.1:9/health"

    async def fail_send(_text):
        return DeliveryResult(ok=False)

    monitor._probe = fake_probe
    monitor._send = fail_send
    for _ in range(5):
        state.data["monitors"]["items"][key]["next_attempt"] = time.time() - 1
        asyncio.run(monitor._check_item(monitor.items[0]))

    st = state.data["monitors"]["items"][key]
    assert st["offline_retry_count"] == 5
    # The offline ladder spends no budget: the content retry counter is
    # never touched (the key stays absent, exactly as before this change).
    assert st.get("retry_count", 0) == 0
    assert st["next_attempt"] - time.time() > OFFLINE_RETRY_SLOW_SECONDS - 120


def test_monitor_alert_clears_the_backoff_after_a_delivery(tmp_path) -> None:
    """A delivered alert must reset the ladder for the next episode."""

    item = {
        "__template_key": "http",
        "name": "API",
        "url": "http://127.0.0.1:9/health",
    }
    monitor, state, _store, _plugin = _make_monitor(tmp_path, [item])
    key = ServiceMonitor._item_key(monitor.items[0])
    state.data["alerts"] = {"offline": {"alerting": True}}
    state.data["monitors"] = {
        "items": {
            key: {
                "alerting": False,
                "alerted": False,
                "pending": False,
                "fails": 1,
                "offline_retry_count": 9,
                "name": "API",
                "target": "http://127.0.0.1:9/health",
            }
        }
    }

    async def fake_probe(_item):
        return False, "连接失败", "http://127.0.0.1:9/health"

    async def ok_send(_text):
        return DeliveryResult(ok=True)

    monitor._probe = fake_probe
    monitor._send = ok_send
    asyncio.run(monitor._check_item(monitor.items[0]))

    st = state.data["monitors"]["items"][key]
    assert "offline_retry_count" not in st


def test_provider_watch_clears_the_backoff_after_a_delivery(
    tmp_path, monkeypatch
) -> None:
    """The LLM watch must reset the ladder when its alert lands."""

    engine, state, _store, _plugin = _make_engine(
        tmp_path,
        {
            "alerts": {
                "enabled": True,
                "provider_watch": {"enabled": True, "threshold": 1},
            }
        },
    )
    state.data["alerts"] = {
        "provider_watch": {
            "alerting": False,
            "alerted": False,
            "last_alert_ts": 0,
            "offline_retry_count": 7,
        }
    }
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda *a, **k: ((5, "failed", True, None)),
    )

    async def ok_send(text, source="alert", kind="resource"):
        return DeliveryResult(ok=True)

    engine._send = ok_send
    engine._provider_last_check = 0.0
    asyncio.run(engine._check_provider_quality())

    watch = state.data["alerts"]["provider_watch"]
    assert "offline_retry_count" not in watch


# ----------------------------------------------------------------------
# 2) daily/weekly report: the offline poll also backs off
# ----------------------------------------------------------------------


def test_daily_schedule_retry_offline_backs_off(tmp_path) -> None:
    reporter = _reporter(tmp_path)
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0

    delays = []
    for _ in range(5):
        assert reporter._schedule_retry("2026-11-05", transport_offline=True) is True
        delays.append(reporter._pending_retry[1] - time.time())

    assert reporter._offline_retry_count == 5
    assert delays[0] < OFFLINE_RETRY_BASE_SECONDS + 60
    assert delays[4] > OFFLINE_RETRY_SLOW_SECONDS - 120
    # The poll being throttled must not drop the queued report.
    assert reporter._pending_recovery_day == "2026-11-05"


def test_weekly_schedule_retry_offline_backs_off(tmp_path) -> None:
    reporter = _reporter(tmp_path)
    reporter._weekly_pending = None
    reporter._weekly_pending_report_day = None
    reporter._weekly_retry_count = 0
    reporter._weekly_pending_recovery_week = None
    reporter._weekly_pending_recovery_since = 0.0

    delays = []
    for _ in range(5):
        assert reporter._schedule_weekly_retry(transport_offline=True) is True
        delays.append(reporter._weekly_pending - time.time())

    assert reporter._weekly_offline_retry_count == 5
    assert delays[0] < OFFLINE_RETRY_BASE_SECONDS + 60
    assert delays[4] > OFFLINE_RETRY_SLOW_SECONDS - 120


def test_offline_backoff_counters_survive_a_restart(tmp_path) -> None:
    """A restart must not reset the ladder to the fast 15-minute step."""

    state = MemoryState()
    reporter = DailyReporter(
        SimpleNamespace(state=state),
        ReportStore(tmp_path / "report.db"),
        {"display": {}, "daily_report": {"enabled": True}},
    )
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0

    for _ in range(5):
        reporter._schedule_retry("2026-11-05", transport_offline=True)
    assert reporter._offline_retry_count == 5

    # Reload: the counters come back from the state file.
    reloaded = DailyReporter(
        SimpleNamespace(state=state),
        ReportStore(tmp_path / "report.db"),
        {"display": {}, "daily_report": {"enabled": True}},
    )
    reloaded._pending_retry = None
    reloaded._pending_retry_count = 0
    reloaded._pending_retry_day = None
    reloaded._pending_recovery_day = None
    reloaded._pending_recovery_since = 0.0
    reloaded._restore_delivery_state()

    assert reloaded._offline_retry_count == 5
    reloaded._schedule_retry("2026-11-05", transport_offline=True)
    assert reloaded._pending_retry[1] - time.time() > OFFLINE_RETRY_SLOW_SECONDS - 120


def test_a_delivered_report_resets_the_backoff(tmp_path) -> None:
    """After a successful send the next failure starts at the fast step."""

    reporter = _reporter(tmp_path)
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0
    for _ in range(5):
        reporter._schedule_retry("2026-11-05", transport_offline=True)
    assert reporter._offline_retry_count == 5

    reporter._clear_retry()

    assert reporter._offline_retry_count == 0
    reporter._schedule_retry("2026-11-06", transport_offline=True)
    assert reporter._pending_retry[1] - time.time() < OFFLINE_RETRY_BASE_SECONDS + 60


# ----------------------------------------------------------------------
# 3) "cannot confirm the online state" is an incident with a lifecycle
# ----------------------------------------------------------------------


def _drive_unknown(engine, monkeypatch, count):
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )

    class _Timeout(Exception):
        pass

    _Timeout.__name__ = "TimeoutError"

    async def failing_call(*args, **kwargs):
        raise _Timeout("timeout")

    monkeypatch.setattr(alerts_module, "_call_action", failing_call)
    for _ in range(count):
        asyncio.run(engine._check_offline())


def _online_call(monkeypatch):
    async def ok_call(*args, **kwargs):
        return {"online": True}

    monkeypatch.setattr(alerts_module, "_call_action", ok_call)


def test_unknown_state_records_alert_and_recovery(tmp_path, monkeypatch) -> None:
    engine, state, store, _plugin = _make_engine(tmp_path)
    engine.interval = 30
    engine._started_at = time.time() - 3600
    state.data["alerts"] = {}

    _drive_unknown(engine, monkeypatch, alerts_module.UNREACHABLE_ALERT_SAMPLES)
    day = now_in_timezone(engine.config).strftime("%Y-%m-%d")
    rows = store.list_events(day)
    alerts = [r for r in rows if r["name"] == "Bot 在线状态" and r["status"] == "alert"]
    assert len(alerts) == 1
    # Its own kind, so the report does not show it as another offline outage.
    assert alerts[0]["kind"] == "connection"

    _online_call(monkeypatch)
    asyncio.run(engine._check_offline())
    rows = store.list_events(day)
    recoveries = [
        r for r in rows if r["name"] == "Bot 在线状态" and r["status"] == "recovery"
    ]
    assert len(recoveries) == 1
    assert recoveries[0]["kind"] == "connection"
    unresolved = store.list_unresolved_alerts(int(time.time()) + 60, 0)
    assert all(row["name"] != "Bot 在线状态" for row in unresolved)


def test_unknown_state_resolving_to_offline_closes_event(tmp_path, monkeypatch) -> None:
    engine, state, store, _plugin = _make_engine(tmp_path)
    engine.interval = 30
    engine._started_at = time.time() - 3600
    state.data["alerts"] = {}

    _drive_unknown(engine, monkeypatch, alerts_module.UNREACHABLE_ALERT_SAMPLES)

    class _ApiNotAvailable(Exception):
        pass

    _ApiNotAvailable.__name__ = "ApiNotAvailable"

    async def offline_call(*args, **kwargs):
        raise _ApiNotAvailable("no client")

    monkeypatch.setattr(alerts_module, "_call_action", offline_call)
    asyncio.run(engine._check_offline())

    day = now_in_timezone(engine.config).strftime("%Y-%m-%d")
    rows = store.list_events(day)
    recoveries = [
        r for r in rows if r["name"] == "Bot 在线状态" and r["status"] == "recovery"
    ]
    assert len(recoveries) == 1
    assert recoveries[0]["kind"] == "connection"
    assert "已确认离线" in recoveries[0]["detail"]


# ----------------------------------------------------------------------
# 4) unresolved alerts: one row per rule, newest rules are not crowded out
# ----------------------------------------------------------------------


def test_unresolved_alerts_returns_one_row_per_rule(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    for index in range(60):
        store.add_event(
            1000 + index,
            "2026-10-25",
            "service",
            "flapping",
            "down",
            "alert",
            target="http://a",
        )
    for index in range(5):
        store.add_event(
            2000 + index, "2026-10-25", "service", f"other{index}", "down", "alert"
        )

    rows = store.list_unresolved_alerts(3000, 0, limit=50)

    assert len(rows) == 6
    flapping = [row for row in rows if row["name"] == "flapping"]
    assert len(flapping) == 1
    assert flapping[0]["ts"] == 1000
    assert {row["name"] for row in rows} == {"flapping"} | {
        f"other{index}" for index in range(5)
    }


# ----------------------------------------------------------------------
# 5) a day over the row cap must disclose that the count is a lower bound
# ----------------------------------------------------------------------


def test_anomaly_section_discloses_row_cap(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.add_event(
        1_760_000_000, "2026-11-05", "service", "API", "down", "alert"
    )
    store.count_events = lambda day: {"alert": 6000}  # type: ignore[method-assign]
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    view = builder.build(
        day="2026-11-05", history_days=7, span_days=1, sections=["anomaly"]
    )

    # The report reads the day itself and the previous day (cross-midnight
    # pairing), so both are over the cap.
    assert view["anomalies"]["data_truncated"] == 2000
    text = build_report_text(view)
    assert "可能有故障未被统计" in text


# ----------------------------------------------------------------------
# 6) the cold dashboard report cache must build once, not once per caller
# ----------------------------------------------------------------------


class _ReportPlugin:
    def __init__(self) -> None:
        self.config = {"display": {}, "daily_report": {"history_days": 7}}
        # A temporary directory, not a relative path: a fixed name wrote
        # the database into the plugin directory on every test run.
        self.report_store = ReportStore(
            Path(tempfile.mkdtemp(prefix="statuscard_page_")) / "report.db"
        )
        self.state = MemoryState()

    def audit_issues(self):
        return []


def test_cold_report_cache_builds_once_for_concurrent_callers(monkeypatch) -> None:
    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[int] = []

    class _SlowBuilder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            builds.append(1)
            # The real build is slow (it renders an image); the point is
            # that the other callers must not start their own.
            time.sleep(0.2)
            return {"day": "2026-11-05", "sections": []}

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _SlowBuilder)

    plugin = _ReportPlugin()
    api = PageApi(plugin)

    async def run() -> None:
        await asyncio.gather(*(api.reports() for _ in range(4)))

    asyncio.run(run())

    assert len(builds) == 1


def test_report_cache_serves_the_stale_copy_while_building(monkeypatch) -> None:
    """A warm cache keeps serving the stale copy instead of waiting."""

    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[int] = []

    class _Builder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            builds.append(1)
            return {"day": "2026-11-05", "sections": []}

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _Builder)
    plugin = _ReportPlugin()
    api = PageApi(plugin)

    asyncio.run(api.reports())
    assert len(builds) == 1
    asyncio.run(api.reports())
    assert len(builds) == 1


# ----------------------------------------------------------------------
# 7) the backoff belongs to ONE episode (reverse audit A, P2)
# ----------------------------------------------------------------------


def test_resource_backoff_resets_at_the_episode_boundary(tmp_path) -> None:
    """A new episode must not inherit the previous episode's slow step."""

    engine, state, _store, _plugin = _make_engine(
        tmp_path, {"alerts": {"sustain_samples": 1, "cooldown_seconds": 0}}
    )
    rule = {
        "alerting": False,
        "alerted": False,
        "label": "CPU 使用率",
        "count": 1,
        "last_sent": 0,
        "retry_count": 1,
        "next_attempt": time.time() - 1,
    }
    state.data["alerts"] = {"offline": {"alerting": True}, "rules": {"cpu": rule}}

    async def fail_send(text, source="alert", kind="resource"):
        return DeliveryResult(ok=False)

    engine._send = fail_send
    for _ in range(6):
        rule["next_attempt"] = time.time() - 1
        asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))
    assert rule["offline_retry_count"] == 6

    # The rule recovers: the episode ends.
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 10.0, 85.0, "%", "10.0%"))
    assert "offline_retry_count" not in rule

    # The next fault starts from the fast step again.
    rule["alerting"] = False
    rule["alerted"] = False
    rule["count"] = 1
    rule["last_sent"] = 0
    rule["next_attempt"] = time.time() - 1
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))
    assert rule["offline_retry_count"] == 1
    assert rule["next_attempt"] - time.time() < OFFLINE_RETRY_BASE_SECONDS + 60


def test_monitor_first_failure_uses_the_offline_ladder(tmp_path) -> None:
    """The FIRST failed alert must not retry after 30 seconds (reverse audit A)."""

    item = {
        "__template_key": "http",
        "name": "API",
        "url": "http://127.0.0.1:9/health",
    }
    monitor, state, _store, _plugin = _make_monitor(tmp_path, [item])
    key = ServiceMonitor._item_key(monitor.items[0])
    state.data["alerts"] = {"offline": {"alerting": True}}
    state.data["monitors"] = {"items": {}}

    async def fake_probe(_item):
        return False, "连接失败", "http://127.0.0.1:9/health"

    async def fail_send(_text):
        return DeliveryResult(ok=False, transport_offline=True)

    monitor._probe = fake_probe
    monitor._send = fail_send
    asyncio.run(monitor._check_item(monitor.items[0]))

    st = state.data["monitors"]["items"][key]
    assert st["offline_retry_count"] == 1
    assert st["next_attempt"] - time.time() > OFFLINE_RETRY_BASE_SECONDS - 120
    # The offline ladder spends no budget.
    assert st.get("retry_count", 0) == 0


def test_truncation_disclosure_mentions_missing_incidents(tmp_path) -> None:
    """The disclosure must say incidents can be missing, not just fewer."""

    store = ReportStore(tmp_path / "report.db")
    assert store.add_event(
        1_760_000_000, "2026-11-05", "service", "API", "down", "alert"
    )
    store.count_events = lambda day: {"alert": 6000}  # type: ignore[method-assign]
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    view = builder.build(
        day="2026-11-05", history_days=7, span_days=1, sections=["anomaly"]
    )

    text = build_report_text(view)
    assert "可能有故障未被统计" in text
    assert "故障时长也可能不准确" in text


def test_two_different_days_build_once_each(monkeypatch) -> None:
    """Two different days must not break the single-flight (audit B, P2)."""

    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[str] = []

    class _Builder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            day = kwargs.get("day") or ""
            builds.append(day)
            time.sleep(0.05)
            return {"day": day, "sections": []}

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _Builder)
    api = PageApi(_ReportPlugin())

    async def run() -> None:
        # Two different days, each with two concurrent callers.
        await asyncio.gather(api.reports(), api.reports(), api.reports(), api.reports())

    async def request_value(name, default=None):
        return default

    monkeypatch.setattr(page_api_module, "_request_value", request_value)
    asyncio.run(run())

    # At most one build per distinct day.
    assert len(builds) == len(set(builds))
    assert len(builds) <= 2


def test_a_failing_build_is_not_amplified(monkeypatch) -> None:
    """A failing build must not be retried once per concurrent caller."""

    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[int] = []

    class _FailingBuilder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            builds.append(1)
            raise RuntimeError("report.db is broken")

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _FailingBuilder)
    api = PageApi(_ReportPlugin())

    async def run() -> None:
        await asyncio.gather(*(api.reports() for _ in range(4)))

    asyncio.run(run())

    assert len(builds) == 1


def test_the_rule_index_does_not_slow_down_a_flapping_rule(tmp_path) -> None:
    """The pairing index must not make the flapping case SLOWER (audit B, P2).

    ``target`` is compared through ``COALESCE(target,'')``, so an index that
    contains it can only use the (kind, name) prefix -- which was slower
    than no index at all on a rule with thousands of open rows.
    """

    import sqlite3

    db = tmp_path / "stress.db"
    con = sqlite3.connect(str(db))
    con.execute(
        """CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
        day TEXT, kind TEXT, name TEXT, target TEXT, detail TEXT, status TEXT,
        resolved_ts INTEGER)"""
    )
    con.executemany(
        "INSERT INTO events (ts, day, kind, name, target, detail, status) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (1700000000 + index, "2026-11-01", "service", "flapping", "", "x", "alert")
            for index in range(4000)
        ],
    )
    con.commit()
    con.close()

    store = ReportStore(db)
    started = time.time()
    rows = store.list_unresolved_alerts(2**31, 0, limit=50)
    elapsed = time.time() - started

    assert len(rows) == 1
    # With the bad index the same query takes ~0.8 s; with the usable one
    # ~6 ms. The threshold sits between the two so the test really fails
    # when the index regresses.
    assert elapsed < 0.4, f"抖动规则的未闭合查询过慢: {elapsed:.2f}s"


def test_the_truncation_notice_shows_without_any_rows(tmp_path) -> None:
    """The notice must appear even when the table is empty (audit B, P2).

    A day whose rows all fell over the cap ends up with an empty anomaly
    list. The disclosure used to live inside the "has rows" branch, so the
    report said "no incidents" while real ones had been dropped.
    """

    from astrbot_plugin_statuscard.t2i_renderer import build_report_html

    store = ReportStore(tmp_path / "report.db")
    # The rows all carry a status the report ignores, so the anomaly table
    # ends up empty while the day is far over the row cap.
    for index in range(5200):
        store.add_event(
            1_760_000_000 + index,
            "2026-11-05",
            "service",
            f"noise{index}",
            "x",
            "info",
        )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(
        day="2026-11-05", history_days=7, span_days=1, sections=["anomaly"]
    )
    anomalies = view["anomalies"]
    assert anomalies["data_truncated"] == 200
    assert anomalies["items"] == []

    html = build_report_html(view)
    assert "超出单日统计上限" in html


# ----------------------------------------------------------------------
# 8) the single-flight must not mask a broken database (audit C, P1)
# ----------------------------------------------------------------------


def test_a_broken_database_is_reported_even_with_a_warm_cache(
    tmp_path, monkeypatch
) -> None:
    """A cached payload must not hide that the database is broken.

    The database gate used to run AFTER the cache lookup, so for the whole
    60 s cache window a broken database looked like healthy stale data and
    the background repair never started (v2.20.24 audit).
    """

    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[int] = []
    probes: list[int] = []

    class _Builder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            builds.append(1)
            return {"day": "2026-11-05", "sections": []}

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _Builder)

    broken = {"value": False}

    async def fake_gate(self=None):
        probes.append(1)
        if broken["value"]:
            return {
                "ok": False,
                "error_code": "report_db_unavailable",
                "db_unavailable": True,
                "sections": [],
            }
        return None

    monkeypatch.setattr(PageApi, "_db_unavailable_payload", fake_gate)

    api = PageApi(_ReportPlugin())
    asyncio.run(api.reports())
    assert len(builds) == 1
    # The cache is now warm (60 s).
    broken["value"] = True
    asyncio.run(api.reports())
    # The gate ran again and the degraded marker was returned.
    assert len(probes) == 2


def test_the_handoff_slot_never_evicts_the_key_it_just_wrote(
    tmp_path, monkeypatch
) -> None:
    """A full handoff dictionary must not evict the newest payload."""

    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[int] = []

    class _Builder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            builds.append(1)
            return {"day": "2026-11-05", "sections": []}

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _Builder)
    api = PageApi(_ReportPlugin())

    # Pre-fill the handoff dictionary so it is already over the limit, with
    # the key this request will use at the FIRST position (re-assigning an
    # existing key keeps its slot).
    cache_key = (
        page_api_module.now_in_timezone(api.plugin.config).date().isoformat(),
        tuple(sorted(api._redact())),
        repr(page_api_module.cfg_get(api.plugin.config, "daily_report.sections", None)),
        7,
        "",
    )
    api._reports_build_results[cache_key] = {"day": "old"}
    for index in range(8):
        api._reports_build_results[("old", index)] = {"day": index}

    asyncio.run(api.reports())

    assert api._reports_build_results.get(cache_key) is not None
    assert len(builds) == 1


def test_a_timeout_does_not_start_a_second_build(tmp_path, monkeypatch) -> None:
    """A build that runs too long must not be joined by a second one."""

    from astrbot_plugin_statuscard import page_api as page_api_module
    from astrbot_plugin_statuscard.page_api import PageApi

    builds: list[int] = []

    class _SlowBuilder:
        def __init__(self, store, config, db_path=None, redact=None):
            pass

        def build(self, **kwargs):
            builds.append(1)
            time.sleep(0.4)
            return {"day": "2026-11-05", "sections": []}

    monkeypatch.setattr(page_api_module, "DailyReportBuilder", _SlowBuilder)

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout=None):
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(page_api_module.asyncio, "wait_for", fast_wait_for)
    api = PageApi(_ReportPlugin())

    async def run() -> None:
        await asyncio.gather(api.reports(), api.reports())

    asyncio.run(run())

    assert len(builds) == 1


def test_the_report_state_survives_an_infinity_counter(tmp_path) -> None:
    """``int(float('inf'))`` raises OverflowError, not ValueError."""

    state = MemoryState()
    state.data["report_delivery"] = {
        "schema": 2,
        "offline_retry_count": float("inf"),
        "weekly_offline_retry_count": float("inf"),
    }
    reporter = DailyReporter(
        SimpleNamespace(state=state),
        ReportStore(tmp_path / "report.db"),
        {"display": {}, "daily_report": {"enabled": True}},
    )
    reporter._restore_delivery_state()

    assert reporter._offline_retry_count == 0
    assert reporter._weekly_offline_retry_count == 0
