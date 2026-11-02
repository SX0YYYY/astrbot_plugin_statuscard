"""Regression tests for the v2.20.18 fix batch.

Every test drives the production code that changed: the monitor item
normaliser/state keys, the maintenance-silence send path, the provider alert
state machine, the weekly/daily report recovery queues and the report view.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import _short_disk_error
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore

TZ = timezone(timedelta(hours=8))


# ----------------------------------------------------------------------
# shared doubles
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

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append({"targets": list(targets), "text": text, "source": source})
        return DeliveryResult(ok=True, ok_targets=list(targets))


def _monitor_config(items: list[dict], targets: list[str] | None = None) -> dict:
    return {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": True,
            "targets": ["123456"] if targets is None else targets,
            "platform_id": "default",
        },
        "monitors": {
            "enabled": True,
            "interval_seconds": 60,
            "fail_threshold": 1,
            "cooldown_seconds": 1800,
            "recover_notify": True,
            "targets": ["123456"] if targets is None else targets,
            "items": items,
        },
        "notify": {"rules": []},
    }


# ----------------------------------------------------------------------
# A. duplicate monitor entries / colliding state keys
# ----------------------------------------------------------------------


def test_identical_monitor_entries_are_ignored_once() -> None:
    item = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    monitor = ServiceMonitor(
        FakePlugin(),
        MemoryState(),
        _monitor_config([item, dict(item)]),
        report_store=None,
    )
    assert len(monitor.items) == 1
    assert len({monitor._item_key(one) for one in monitor.items}) == 1


def test_same_target_with_different_probe_gets_separate_state() -> None:
    first = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    second = dict(first, keyword="status: ok")
    monitor = ServiceMonitor(
        FakePlugin(), MemoryState(), _monitor_config([first, second]), report_store=None
    )
    assert len(monitor.items) == 2
    keys = [monitor._item_key(one) for one in monitor.items]
    assert len(set(keys)) == 2
    # Both colliding entries are suffixed, so reordering the config cannot
    # swap which probe owns which state slot.
    assert all("#" in key for key in keys)
    assert monitor._item_key(monitor.items[1]) == keys[1]

    # Reversing the list must not change the key of either probe.
    monitor_reordered = ServiceMonitor(
        FakePlugin(), MemoryState(), _monitor_config([second, first]), report_store=None
    )
    pairs = {one.get("keyword", ""): monitor._item_key(one) for one in monitor.items}
    pairs_reordered = {
        one.get("keyword", ""): monitor_reordered._item_key(one)
        for one in monitor_reordered.items
    }
    assert pairs == pairs_reordered


def test_omitted_and_explicit_default_timeout_is_one_item() -> None:
    base = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    explicit = dict(base, timeout_seconds=10, expected_status=0, verify_tls=True)
    monitor = ServiceMonitor(
        FakePlugin(),
        MemoryState(),
        _monitor_config([base, explicit]),
        report_store=None,
    )
    # Both probe identically; the copy must not get a second state slot.
    assert len(monitor.items) == 1


def test_monitor_without_targets_does_not_probe_or_record() -> None:
    """The audit message must match check_all's actual no-target behaviour."""

    item = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    monitor = ServiceMonitor(
        FakePlugin(),
        MemoryState(),
        _monitor_config([item], targets=[]),
        report_store=None,
    )
    probed: list[dict] = []

    async def probe(one):
        probed.append(one)
        return True, "HTTP 200", one["url"]

    monitor._probe = probe  # type: ignore[assignment]
    asyncio.run(monitor.check_all())
    assert probed == []
    assert (monitor.state.get("monitors", {}) or {}).get("last_run") is None

    issues = run_audit(_monitor_config([item], targets=[]))
    message = next(
        (issue.message for issue in issues if issue.code == "monitors_no_targets"),
        "",
    )
    assert "不会探测" in message
    assert "仍会探测" not in message


def test_shrinking_a_collision_keeps_the_survivors_state_key() -> None:
    first = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    second = dict(first, keyword="status: ok")
    monitor = ServiceMonitor(
        FakePlugin(), MemoryState(), _monitor_config([first, second]), report_store=None
    )
    key = monitor._item_key(monitor.items[1])
    with monitor.state.section("monitors") as monitors:
        monitors.setdefault("items", {})[key] = {"alerting": True, "alerted": True}

    # The user deletes the other entry: the survivor must keep its suffixed
    # key, or the prune writes a fake "monitor removed" recovery and alerts
    # the same outage again.
    survivor = ServiceMonitor(
        FakePlugin(),
        monitor.state,
        _monitor_config([second]),
        report_store=None,
    )
    assert survivor._item_key(survivor.items[0]) == key
    assert key in (survivor.state.get("monitors", {}) or {}).get("items", {})


def test_legacy_shared_state_moves_to_the_first_split_entry() -> None:
    first = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    second = dict(first, keyword="status: ok")
    state = MemoryState()
    base = "http:站点:https://example.com/health"
    state.data["monitors"] = {"items": {base: {"alerting": True, "alerted": True}}}
    monitor = ServiceMonitor(
        FakePlugin(), state, _monitor_config([first, second]), report_store=None
    )
    items = state.data["monitors"]["items"]
    assert base not in items
    first_key = monitor._item_key(monitor.items[0])
    assert first_key in items
    assert items[first_key].get("alerting") is True


def test_monitor_send_is_suppressed_during_maintenance_silence() -> None:
    item = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    plugin = FakePlugin()
    monitor = ServiceMonitor(plugin, MemoryState(), _monitor_config([item]))
    monitor._is_silenced = lambda: True  # type: ignore[assignment]
    result = asyncio.run(monitor._send("聚合汇总"))
    # Not delivered: the caller must not mark the rule as notified.
    assert result.ok is False
    assert plugin.calls == []


def test_silenced_summary_is_abandoned_not_marked_delivered() -> None:
    from astrbot_plugin_statuscard.aggregator import AlertAggregator

    async def run() -> None:
        item = {
            "name": "站点",
            "type": "http",
            "url": "https://example.com/health",
            "enabled": True,
        }
        agg = AlertAggregator(window_seconds=3600, max_items=8)
        plugin = FakePlugin()
        monitor = ServiceMonitor(
            plugin,
            MemoryState(),
            _monitor_config([item]),
            report_store=None,
            aggregator=agg,
        )
        monitor._is_silenced = lambda: True  # type: ignore[assignment]
        key = monitor._item_key(monitor.items[0])
        with monitor.state.section("monitors") as monitors:
            monitors.setdefault("items", {})[key] = {
                "name": "站点",
                "target": item["url"],
                "alerting": True,
                "alerted": True,
                "summary_buffered": True,
                "awaiting_summary": True,
                "since": time.time() - 60,
                "last_sent": 0.0,
                "fails": 0,
            }

        await agg.push(
            "站点",
            "HTTP 500",
            "first",
            monitor._send,
            kind="service",
            on_abandon=monitor._on_summary_abandoned,
            on_delivered=monitor._on_summary_delivered,
        )
        await agg.push(
            "站点",
            "HTTP 500",
            "second",
            monitor._send,
            kind="service",
            on_abandon=monitor._on_summary_abandoned,
            on_delivered=monitor._on_summary_delivered,
        )
        # The summary cannot be delivered during the silence; after the
        # retries it is abandoned, which must withdraw "notified".
        for _ in range(3):
            await agg.flush("service")
        state = monitor.state.data["monitors"]["items"][key]
        assert state["alerted"] is False
        assert state.get("summary_abandoned") is True
        assert plugin.calls == []

    asyncio.run(run())


def test_monitor_clears_abandoned_summary_marker_after_healing() -> None:
    item = {
        "name": "站点",
        "type": "http",
        "url": "https://example.com/health",
        "enabled": True,
    }
    monitor = ServiceMonitor(FakePlugin(), MemoryState(), _monitor_config([item]))
    key = monitor._item_key(monitor.items[0])
    now = time.time()
    with monitor.state.section("monitors") as monitors:
        monitors.setdefault("items", {})[key] = {
            "name": "站点",
            "target": item["url"],
            "alerting": True,
            "alerted": True,
            "since": now - 60,
            "summary_abandoned": True,
            "last_sent": 0.0,
            "fails": 0,
        }

    async def probe_ok(one):
        return True, "HTTP 200", one["url"]

    monitor._probe = probe_ok  # type: ignore[assignment]
    asyncio.run(monitor._check_item(monitor.items[0]))
    state = monitor.state.data["monitors"]["items"][key]
    assert "summary_abandoned" not in state


# ----------------------------------------------------------------------
# B. provider alert duplicate after a cooldown retry
# ----------------------------------------------------------------------


class ScriptedPlugin:
    def __init__(self, results: list[DeliveryResult]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append(text)
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(ok=True, ok_targets=list(targets))


def _provider_engine(tmp_path, plugin) -> AlertEngine:
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "provider_watch": {
                "enabled": True,
                "threshold": 3,
                "cooldown_seconds": 300,
            },
        },
    }
    return AlertEngine(
        plugin,
        StateStore(tmp_path / "state.json"),
        config,
        report_store=ReportStore(tmp_path / "report.db"),
    )


def test_provider_cooldown_retry_drops_the_stale_queued_alert(
    tmp_path, monkeypatch
) -> None:
    plugin = ScriptedPlugin(
        [
            DeliveryResult(ok=False, transport_offline=True),
            DeliveryResult(ok=True, ok_targets=["123456"]),
        ]
    )
    engine = _provider_engine(tmp_path, plugin)
    failing = lambda db_path, limit: (7, "error", 20)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", failing)

    asyncio.run(engine._check_provider_quality())
    watch = (engine.state.get("alerts", {}) or {}).get("provider_watch", {})
    assert watch.get("pending_alert_text")
    assert watch.get("alerted") is False

    # The cooldown has elapsed but the queued retry is not due yet: the fresh
    # cooldown-retry alert must supersede the queued copy.
    watch["last_alert_ts"] = time.time() - 400
    watch["next_attempt"] = time.time() + 900
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert len(plugin.calls) == 2
    assert not watch.get("pending_alert_text")
    assert not watch.get("next_attempt")

    # Even if the old retry deadline now arrives, nothing stale is left.
    watch["next_attempt"] = time.time() - 1
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert len(plugin.calls) == 2


# ----------------------------------------------------------------------
# C. report recovery queues
# ----------------------------------------------------------------------


def _reporter(**attrs) -> DailyReporter:
    reporter = object.__new__(DailyReporter)
    reporter.config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": False,
            "targets": ["123456"],
            "platform_id": "default",
        },
        "daily_report": {
            "enabled": True,
            "time": "21:00",
            "targets": [],
            "sections": ["resource", "anomaly", "qq", "token"],
            "weekly_time": "21:30",
            "weekly_enabled": True,
        },
    }
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0
    reporter._channel_probe_failures = 0
    reporter._daily_given_up_day = None
    reporter._daily_pause_until = 0.0
    reporter._daily_backfill_day = None
    reporter._daily_backfill_inflight = False
    reporter._last_attempt_day = None
    reporter._last_attempt_wall_day = None
    reporter._last_sent_day = None
    reporter._weekly_pending_report_day = None
    reporter._weekly_pending = None
    reporter._weekly_retry_count = 0
    reporter._weekly_attempt_week = None
    reporter._weekly_given_up_week = None
    reporter._weekly_pending_recovery_week = None
    reporter._weekly_pending_recovery_since = 0.0
    reporter._weekly_last_sent_week = None
    reporter._weekly_last_sent_slot = None
    for key, value in attrs.items():
        setattr(reporter, key, value)
    return reporter


def test_stale_weekly_recovery_is_dropped_instead_of_sending_an_arbitrary_week(
    monkeypatch,
) -> None:
    reporter = _reporter(_weekly_pending_recovery_week="2026-W44")
    now = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    assert reporter.notify_offline_recovery() is False
    assert reporter._weekly_pending is None
    assert reporter._weekly_pending_recovery_week is None
    assert reporter._weekly_pending_report_day is None


def test_weekly_recovery_within_window_rebuilds_the_missing_report_day(
    monkeypatch,
) -> None:
    reporter = _reporter(_weekly_pending_recovery_week="2026-W44")
    now = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    assert reporter.notify_offline_recovery() is True
    # The reload dropped the report day but the ISO week names the Saturday.
    assert reporter._weekly_pending_report_day == "2026-10-31"
    assert reporter._weekly_pending is not None


def test_weekly_offline_recovery_does_not_rebuild_while_disconnected(
    monkeypatch,
) -> None:
    reporter = _reporter(
        plugin=ChannelPlugin(False),
        _weekly_pending_recovery_week="2026-W44",
        _weekly_pending_report_day="2026-10-31",
    )
    now = datetime(2026, 11, 2, 3, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    built: list[int] = []
    reporter._build_view = lambda *a, **k: built.append(1)  # type: ignore[assignment]
    asyncio.run(reporter._send_weekly_scheduled("2026-10-31", "2026-11-01 21:30"))
    assert built == []
    assert reporter._weekly_pending is not None


def test_stale_weekly_send_clears_the_marker_and_stops(monkeypatch) -> None:
    reporter = _reporter(
        plugin=ChannelPlugin(True),
        _weekly_pending_recovery_week="2026-W44",
        _weekly_pending_report_day="2026-10-31",
    )
    now = datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
    monkeypatch.setattr(
        "astrbot_plugin_statuscard.report.now_in_timezone", lambda cfg=None: now
    )
    built: list[int] = []
    reporter._build_view = lambda *a, **k: built.append(1)  # type: ignore[assignment]
    asyncio.run(reporter._send_weekly_scheduled("2026-10-31", "2026-11-01 21:30"))
    assert built == []
    assert reporter._weekly_pending_recovery_week is None
    assert reporter._weekly_pending is None


class _Client:
    def __init__(self, reachable: bool) -> None:
        self.reachable = reachable

    async def call_action(self, action, **kwargs):
        if not self.reachable:
            raise RuntimeError("ApiNotAvailable")
        return {"online": True}


class ChannelPlugin:
    def __init__(self, reachable: bool) -> None:
        self.client = _Client(reachable)

    def _onebot_client(self):
        return self.client


class ActionFailed(Exception):
    pass


class _RaisingClient:
    def __init__(self, exc):
        self.exc = exc

    async def call_action(self, action, **kwargs):
        raise self.exc("boom")


class RaisingPlugin:
    def __init__(self, exc):
        self.exc = exc

    def _onebot_client(self):
        return _RaisingClient(self.exc)


class NoClientPlugin:
    def _onebot_client(self):
        return None


def test_channel_probe_treats_a_host_without_a_client_as_reachable() -> None:
    reporter = _reporter(plugin=NoClientPlugin())
    assert asyncio.run(reporter._channel_reachable()) is True


def test_channel_probe_treats_action_failed_as_reachable() -> None:
    # OneBot answered status=failed: the transport is up (same rule as the
    # alert engine's offline check).
    reporter = _reporter(plugin=RaisingPlugin(ActionFailed))
    assert asyncio.run(reporter._channel_reachable()) is True


def test_three_ambiguous_probe_failures_let_one_real_send_through() -> None:
    class _Store:
        def probe(self):
            return True

        def ensure_ready(self):
            return True

    reporter = _reporter(
        plugin=RaisingPlugin(RuntimeError), _daily_backfill_inflight=True
    )
    reporter.store = _Store()  # type: ignore[assignment]
    built: list[int] = []

    def build(*args, **kwargs):
        built.append(1)
        raise RuntimeError("stop after build")

    reporter._build_view = build  # type: ignore[assignment]
    for _ in range(3):
        asyncio.run(reporter._send_scheduled("2026-11-01"))
    # The first two probes only queue; the third releases one real attempt.
    assert built == [1]
    assert reporter._channel_probe_failures == 0


def test_backfill_offline_skips_the_build_and_keeps_a_poll_when_alerts_are_off() -> (
    None
):
    reporter = _reporter(
        plugin=ChannelPlugin(False),
        _daily_backfill_inflight=True,
    )
    built: list[int] = []
    reporter._build_view = lambda *a, **k: built.append(1)  # type: ignore[assignment]
    asyncio.run(reporter._send_scheduled("2026-11-01"))
    assert built == []
    assert reporter._pending_recovery_day == "2026-11-01"
    # The channel-probed poll stays as the fallback; while offline it does
    # not rebuild the report (the probe returns before _build_view).
    assert reporter._pending_retry is not None


def test_backfill_offline_keeps_the_poll_even_when_alerts_are_on() -> None:
    reporter = _reporter(plugin=ChannelPlugin(False), _daily_backfill_inflight=True)
    reporter.config["alerts"]["enabled"] = True
    reporter._build_view = lambda *a, **k: (_ for _ in ()).throw(AssertionError)  # type: ignore[assignment]
    asyncio.run(reporter._send_scheduled("2026-11-01"))
    assert reporter._pending_recovery_day == "2026-11-01"
    # The reconnect hook is only an accelerator; the poll is still the
    # fallback and must not rebuild while the channel probe fails.
    assert reporter._pending_retry is not None


def test_backfill_with_a_live_channel_still_builds() -> None:
    class _Store:
        def probe(self):
            return True

        def ensure_ready(self):
            return True

    reporter = _reporter(plugin=ChannelPlugin(True), _daily_backfill_inflight=True)
    reporter.store = _Store()  # type: ignore[assignment]
    built: list[int] = []

    def build(*args, **kwargs):
        built.append(1)
        raise RuntimeError("stop after build")

    reporter._build_view = build  # type: ignore[assignment]
    asyncio.run(reporter._send_scheduled("2026-11-01"))
    assert built == [1]


# ----------------------------------------------------------------------
# D. display details
# ----------------------------------------------------------------------


def test_disk_errors_are_short_enough_for_the_card() -> None:
    assert _short_disk_error(None) is None
    assert _short_disk_error("读取超时") == "读取超时"
    assert (
        _short_disk_error(
            "[Errno 13] Permission denied: '/mnt/very-long-nfs-mount/data'"
        )
        == "无权限读取"
    )
    assert _short_disk_error("[Errno 2] No such file or directory: '/mnt/x'") == (
        "挂载点不存在"
    )
    # A path containing "timeout" must not turn a permission error into one.
    assert (
        _short_disk_error("[Errno 13] Permission denied: '/mnt/timeout-backup'")
        == "无权限读取"
    )
    long_text = "x" * 60
    assert _short_disk_error(long_text) == "x" * 23 + "…"


def test_summary_text_prefers_a_readable_disk(monkeypatch) -> None:
    from astrbot_plugin_statuscard import main as main_mod

    view = {
        "bot": {
            "nick": "bot",
            "friends": 1,
            "groups": 2,
            "members": 3,
            "recv": 4,
            "sent": 5,
            "recv_scope": "scope",
            "sent_scope": "scope",
        },
        "overview": [{"value": "x"}, {"value": "host"}],
        "resources": [
            {"title": "CPU", "percent": 1.0, "desc": "cpu"},
            {"title": "RAM", "percent": 2.0, "desc": "ram"},
            {"title": "SWAP", "percent": 3.0, "desc": "swap"},
        ],
        "disks": [
            {
                "path": "/mnt/bad",
                "used": "--",
                "total": "--",
                "percent_text": "--",
                "error": "无权限读取",
            },
            {
                "path": "/",
                "used": "45.0 GB",
                "total": "68.0 GB",
                "percent_text": "66.0%",
                "error": None,
            },
        ],
        "network": {
            "up": "1 KB/s",
            "down": "2 KB/s",
            "month_up": "3 GB",
            "month_down": "4 GB",
        },
        "process": {"counts": {"all": 1, "running": 1, "sleeping": 0}},
    }

    async def fake_collect(**kwargs):
        return {"view": view}

    monkeypatch.setattr(main_mod, "collect_all", fake_collect)
    plugin = object.__new__(main_mod.StatusCardPlugin)
    plugin.config = {"display": {"timezone": "Asia/Shanghai"}}
    plugin.context = None
    plugin.state = None
    text = asyncio.run(plugin.build_summary_text())
    assert "66.0%" in text
    assert "/mnt/bad" not in text


def test_report_averages_ignore_negative_rows_like_the_chart() -> None:
    stats = DailyReportBuilder._metric_stats([{"cpu": -5.0}, {"cpu": 20.0}], "cpu")
    # A negative percentage is corrupt data, not a zero reading; dropping it
    # keeps the average on the valid samples instead of pulling it down.
    assert stats["avg"] == 20.0
    assert stats["peak"] == 20.0
    assert stats["count"] == 1
