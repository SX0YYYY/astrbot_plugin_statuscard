"""Regression tests for the v2.20.22 audit batch.

Every test drives the production path that changed. The batch focuses on
messages that were silently dropped, redaction gaps, report limits that hid
real incidents, and two framework-semantics mistakes:

* a long offline episode marked "stale" after a reload suppressed the only
  "connection is back" notice;
* a queued LLM alert was discarded when ``recover_notify`` was off;
* monitor names in chat text bypassed the hostname redaction rule;
* deleting one duplicate monitor closed the surviving duplicate's alert row;
* a malformed message-stats file disabled the receive counter for every group;
* ``should_call_llm(False)`` was a no-op (the framework flag is inverted).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard import sampler as sampler_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import now_in_timezone
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi, _monitor_status
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore

TZ = timezone(timedelta(hours=8))


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


def _make_engine(tmp_path, config=None, plugin=None):
    state = MemoryState()
    plugin = plugin or ScriptedPlugin()
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


# ----------------------------------------------------------------------
# 1) offline: a restart during a >24h outage must still announce recovery
# ----------------------------------------------------------------------


class _ToggleClient:
    def __init__(self) -> None:
        self.online = False

    async def call_action(self, action, **kwargs):
        return {"online": self.online}


def test_offline_long_outage_survives_restart_stale_mark(tmp_path, monkeypatch) -> None:
    engine, state, _store, plugin = _make_engine(tmp_path)
    client = _ToggleClient()
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: client
    )
    now = time.time()
    offline = state.setdefault("alerts", {})["offline"] = {
        "alerting": True,
        "alerted": True,
        "since": now - 2 * 86400,
        "last_sent": now - 2 * 86400,
        "count": 0,
        # The detector was running until a moment before the restart. The
        # old code never wrote this field for offline, so every restart
        # during a >24h outage looked like "alerts were disabled" and the
        # recovery notice was suppressed (v2.20.22).
        "last_seen": now - 60,
    }

    engine._mark_stale_rules()
    assert not offline.get("stale")

    asyncio.run(engine._check_offline())  # still offline: fresh sample
    assert offline["last_seen"] >= now

    client.online = True
    asyncio.run(engine._check_offline())
    assert any("Bot 连接已恢复" in text for text in plugin.calls)


def test_offline_disabled_for_a_day_still_suppresses_recovery(
    tmp_path, monkeypatch
) -> None:
    """Preserve the stale contract: re-enabling alerts must not replay a
    recovery for an outage that ended while nothing was being sampled."""

    engine, state, _store, plugin = _make_engine(tmp_path)
    client = _ToggleClient()
    client.online = True
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: client
    )
    now = time.time()
    offline = state.setdefault("alerts", {})["offline"] = {
        "alerting": True,
        "alerted": True,
        "since": now - 2 * 86400,
        "last_sent": now - 2 * 86400,
        "count": 0,
        # No last_seen at all: the alert engine was off for the whole gap.
    }

    engine._mark_stale_rules()
    assert offline.get("stale") is True
    asyncio.run(engine._check_offline())
    assert not any("Bot 连接已恢复" in text for text in plugin.calls)


# ----------------------------------------------------------------------
# 2) provider: recover_notify=false must not swallow a queued outage alert
# ----------------------------------------------------------------------


class _OfflineThenOnlinePlugin(ScriptedPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append(text)
        if self.fail_next:
            self.fail_next = False
            return DeliveryResult(
                ok=False, failed_targets=list(targets), transport_offline=True
            )
        return DeliveryResult(ok=True, ok_targets=list(targets))


def test_provider_queued_alert_is_sent_when_recover_notify_is_off(
    tmp_path, monkeypatch
) -> None:
    plugin = _OfflineThenOnlinePlugin()
    engine, _state, _store, _plugin = _make_engine(
        tmp_path,
        {
            "alerts": {
                "enabled": True,
                "recover_notify": False,
                "provider_watch": {
                    "enabled": True,
                    "threshold": 3,
                    "cooldown_seconds": 300,
                },
            }
        },
        plugin,
    )
    samples = iter([(5, "failed", True), (0, "completed", True)])
    monkeypatch.setattr(
        alerts_module, "provider_recent_streak", lambda *a, **k: next(samples)
    )

    engine._provider_last_check = 0.0
    asyncio.run(engine._check_provider_quality())  # alert; transport down
    engine._provider_last_check = 0.0
    asyncio.run(engine._check_provider_quality())  # recovered

    assert len(plugin.calls) == 2
    assert "LLM 服务连续失败" in plugin.calls[-1]
    assert "已恢复" not in plugin.calls[-1]


# ----------------------------------------------------------------------
# 3) a fresh resource alert clears the stale recovery suppression
# ----------------------------------------------------------------------


def test_resource_realert_clears_stale_and_recovery_is_sent(
    tmp_path, monkeypatch
) -> None:
    engine, state, _store, plugin = _make_engine(
        tmp_path,
        {"alerts": {"enabled": True, "sustain_samples": 1, "cooldown_seconds": 0}},
    )
    now = time.time()
    rule = state.setdefault("alerts", {}).setdefault("rules", {}).setdefault("cpu", {})
    rule.update(
        {
            "alerting": True,
            "alerted": False,
            "stale": True,
            "alert_since": now - 2 * 86400,
            "count": 0,
            "last_sent": now - 100,
            "next_attempt": 0,
        }
    )
    monkeypatch.setattr(alerts_module, "cached_cpu_percent", lambda: 99.0)

    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 99.0, 85.0, "%", "99.0%"))
    assert rule.get("stale") is None
    assert any("系统告警" in text for text in plugin.calls)

    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 10.0, 85.0, "%", "10.0%"))
    assert any("已恢复" in text for text in plugin.calls)


# ----------------------------------------------------------------------
# 4) audit: a bare-string ``kinds`` must not produce a false legacy warning
# ----------------------------------------------------------------------


def test_audit_accepts_string_kinds_without_false_legacy_warning() -> None:
    issues = run_audit(
        {
            "notify": {
                "rules": [
                    {
                        "enabled": True,
                        "target": "default:GroupMessage:1",
                        "kinds": "service",
                    }
                ]
            },
            "monitors": {
                "enabled": True,
                "targets": ["default:GroupMessage:2"],
                "items": [],
            },
        }
    )
    assert not any(issue.code == "legacy_module_targets" for issue in issues)


def test_audit_accepts_tuple_kinds_without_false_legacy_warning() -> None:
    issues = run_audit(
        {
            "notify": {
                "rules": [
                    {
                        "enabled": True,
                        "target": "default:GroupMessage:1",
                        "kinds": ("service",),
                    }
                ]
            },
            "monitors": {
                "enabled": True,
                "targets": ["default:GroupMessage:2"],
                "items": [],
            },
        }
    )
    assert not any(issue.code == "legacy_module_targets" for issue in issues)


# ----------------------------------------------------------------------
# 5) monitor chat text honours the hostname rule
# ----------------------------------------------------------------------


def test_monitor_alert_and_recovery_mask_the_item_name(tmp_path) -> None:
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    config = {
        "display": {"redact": ["hostname"]},
        "monitors": {
            "enabled": True,
            "fail_threshold": 1,
            "cooldown_seconds": 0,
            "targets": ["default:GroupMessage:1"],
        },
    }
    monitor = ServiceMonitor(plugin, state, config, store, None)
    item = {
        "__template_key": "http",
        "name": "https://10.9.9.9:8443/admin?token=secret",
        "url": "http://127.0.0.1:9/health",
        "timeout_seconds": 1,
    }
    monitor.items = [item]
    healthy = False

    async def fake_probe(_item):
        return (healthy, "连接被拒绝", "http://127.0.0.1:9/health")

    monitor._probe = fake_probe  # type: ignore[assignment]
    asyncio.run(monitor.check_all())
    healthy = True
    asyncio.run(monitor.check_all())

    assert plugin.calls
    assert all("10.9.9.9" not in text for text in plugin.calls)
    assert any("已隐藏" in text for text in plugin.calls)


# ----------------------------------------------------------------------
# 6) pruning a duplicate must not close the surviving duplicate's alert
# ----------------------------------------------------------------------


def test_prune_duplicate_does_not_close_surviving_alert(tmp_path) -> None:
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        plugin,
        state,
        {"monitors": {"enabled": True}, "display": {}},
        store,
        None,
    )
    item = {
        "__template_key": "http",
        "name": "API",
        "url": "http://127.0.0.1:9/health",
        "keyword": "ok",
        "__key_suffix": "aaaa1111",
    }
    monitor.items = [item]
    base = monitor._item_key(item).split("#", 1)[0]
    now = int(time.time())
    # The event day follows the configured time zone, not the machine's UTC
    # date: a hard-coded "2026-11-05" fails as soon as the suite runs after
    # midnight in that zone (same trap as v2.20.21's CI fix).
    today = now_in_timezone(monitor.config).strftime("%Y-%m-%d")
    for suffix in ("aaaa1111", "bbbb2222"):
        store.add_event(
            now,
            today,
            "service",
            "API",
            "健康检查失败",
            "alert",
            "http://127.0.0.1:9/health",
        )
        state.setdefault("monitors", {}).setdefault("items", {})[f"{base}#{suffix}"] = {
            "alerting": True,
            "alerted": True,
            "since": now - 10,
            "name": "API",
            "target": "http://127.0.0.1:9/health",
        }

    monitor._prune_state()

    rows = store.list_events(today)
    assert rows
    assert all(row.get("resolved_ts") is None for row in rows)


def test_prune_does_not_treat_names_with_hash_as_duplicates(tmp_path) -> None:
    """A name may contain "#"; only our 8-hex suffix is a state-key suffix."""

    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        plugin,
        state,
        {"monitors": {"enabled": True}, "display": {}},
        store,
        None,
    )
    survivor = {
        "__template_key": "docker",
        "name": "CPU #2",
        "container": "web",
    }
    removed_key = "docker:CPU #1:web"
    monitor.items = [survivor]
    state.setdefault("monitors", {}).setdefault("items", {})[removed_key] = {
        "alerting": True,
        "alerted": True,
        "since": time.time() - 10,
        "name": "CPU #1",
        "target": "web",
    }

    monitor._prune_state()

    rows = store.list_events(now_in_timezone(monitor.config).strftime("%Y-%m-%d"))
    assert any(
        row.get("status") == "recovery" and row.get("name") == "CPU #1" for row in rows
    )


# ----------------------------------------------------------------------
# 7) hostname redaction uses token boundaries, not substrings
# ----------------------------------------------------------------------


def test_hostname_redaction_does_not_hide_unrelated_words() -> None:
    redactor = Redactor(["hostname"])
    redactor._own_hostname = "app"
    assert redactor.mask_name("Apple Mirror") == "Apple Mirror"
    assert redactor.mask_name("Application") == "Application"
    assert redactor.mask_name("App") == "已隐藏"
    assert (
        redactor.mask_address_tokens("Application startup failed")
        == "Application startup failed"
    )
    redactor._own_hostname = "db1"
    assert redactor.mask_name("db10 replica") == "db10 replica"


# ----------------------------------------------------------------------
# 8) one malformed message-stats file must not disable every group
# ----------------------------------------------------------------------


def test_one_bad_message_stats_file_does_not_disable_all(tmp_path, monkeypatch) -> None:
    groups = tmp_path / "groups"
    groups.mkdir()
    (groups / "good.json").write_text(
        json.dumps(
            {
                "users": [{"message_count": 100}, {"message_count": 50}],
                "last_updated": "2026-11-05 10:00",
            }
        ),
        encoding="utf-8",
    )
    (groups / "bad.json").write_text('{"users": null}', encoding="utf-8")
    monkeypatch.setattr(sampler_module, "_message_stats_dir", lambda: groups)

    total, latest = sampler_module.read_message_stats_total()
    assert total == 150
    assert latest == "2026-11-05 10:00"


def test_all_malformed_message_stats_returns_unavailable(tmp_path, monkeypatch) -> None:
    groups = tmp_path / "groups"
    groups.mkdir()
    (groups / "bad.json").write_text('{"users": null}', encoding="utf-8")
    monkeypatch.setattr(sampler_module, "_message_stats_dir", lambda: groups)

    # Not "zero messages": a malformed directory must not become a 0
    # baseline that makes the next day count the whole history.
    assert sampler_module.read_message_stats_total() == (None, None)


# ----------------------------------------------------------------------
# 9) a failed state write cleans up its temporary file
# ----------------------------------------------------------------------


def test_state_write_failure_leaves_no_tmp_file(tmp_path, monkeypatch) -> None:
    store = StateStore(tmp_path / "state.json")
    store.set("key", "value")

    def boom(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", boom)
    store.save()

    assert not list(tmp_path.glob("*.tmp"))


def test_report_store_rate_limits_repeated_ready_warnings(tmp_path, caplog) -> None:
    # Make the parent path a file so ``mkdir(parents=True)`` keeps failing.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    store = ReportStore(blocker / "report.db")
    caplog.set_level(logging.WARNING, logger="astrbot")
    for _ in range(5):
        assert store.ensure_ready() is False
    warnings = [
        record for record in caplog.records if "仍不可用" in record.getMessage()
    ]
    assert len(warnings) == 1


# ----------------------------------------------------------------------
# 10) dashboard status: an inconclusive probe is not green
# ----------------------------------------------------------------------


def test_dashboard_status_marks_inconclusive_probe_unknown() -> None:
    now = time.time()
    assert (
        _monitor_status(
            {"last_checked": now, "probe_error_at": now, "last_ok": now - 10}, None
        )
        == "unknown"
    )
    assert (
        _monitor_status(
            {"last_checked": now, "probe_error_at": now - 10, "last_ok": now}, None
        )
        == "ok"
    )
    assert _monitor_status({"last_checked": now, "fails": 1}, None) == "degraded"


# ----------------------------------------------------------------------
# 11) report: the lookback follows event retention and averages stay finite
# ----------------------------------------------------------------------


def test_anomaly_lookback_uses_event_retention(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = date(2026, 11, 2)
    start = int(datetime.combine(day, datetime.min.time(), tzinfo=TZ).timestamp())
    old = start - 40 * 86400
    store.add_event(
        old, "2026-09-23", "service", "long-outage", "仍然失败", "alert", "t"
    )
    builder = DailyReportBuilder(
        store, {"display": {"timezone": "Asia/Shanghai"}, "daily_report": {}}
    )
    section = builder._anomaly_section(day, day - timedelta(days=1))
    assert section["unresolved"] == 1
    assert any("long-outage" in str(item.get("name")) for item in section["items"])


def test_metric_stats_degrades_to_dash_on_overflow() -> None:
    stats = DailyReportBuilder._metric_stats([{"cpu": 1e308}, {"cpu": 1e308}], "cpu")
    assert stats["avg"] is None
    assert stats["avg_text"] == "--"
    assert stats["peak"] is None
    assert stats["peak_text"] == "--"


# ----------------------------------------------------------------------
# 12) weekly retry budget is scoped to the ISO week
# ----------------------------------------------------------------------


class _Config(dict):
    pass


def _reporter_shell() -> DailyReporter:
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _Config({"display": {"timezone": "Asia/Shanghai"}})
    reporter._weekly_pending = None
    reporter._weekly_pending_report_day = "2026-11-07"
    reporter._weekly_retry_count = 3
    reporter._weekly_retry_week = "2026-W44"
    reporter._weekly_given_up_week = None
    reporter._weekly_pending_recovery_week = None
    reporter._weekly_pending_recovery_since = 0.0
    reporter._save_delivery_state = lambda: None  # type: ignore[assignment]
    reporter._weekly_slot_before = lambda _now: datetime(  # type: ignore[assignment]
        2026, 11, 15, 21, 30, tzinfo=TZ
    )
    reporter._week_key = lambda _day: "2026-W45"  # type: ignore[assignment]
    return reporter


def test_weekly_retry_budget_resets_in_a_new_week() -> None:
    reporter = _reporter_shell()
    assert reporter._schedule_weekly_retry(store_unavailable=True) is True
    assert reporter._weekly_retry_count == 0
    assert reporter._weekly_retry_week == "2026-W45"


# ----------------------------------------------------------------------
# 13) a database outage moves the queued report to the newest due day
# ----------------------------------------------------------------------


def test_store_outage_queues_the_newest_due_report_day() -> None:
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _Config({"display": {"timezone": "Asia/Shanghai"}})
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0
    reporter._save_delivery_state = lambda: None  # type: ignore[assignment]
    reporter._default_report_day = lambda: "2026-11-01"  # type: ignore[assignment]
    reporter._latest_due_report_day = lambda: "2026-11-04"  # type: ignore[assignment]

    assert reporter._schedule_retry(None, store_unavailable=True) is True
    assert reporter._pending_recovery_day == "2026-11-04"


def test_orphan_recovery_pin_does_not_block_post_midnight_backfill(
    monkeypatch,
) -> None:
    """A dropped stale retry may leave its report-day pin behind.

    That pin is a marker, not a live queue: treating it as "a report is
    already queued" made the 00:30 restart skip the newest due report.
    """

    from astrbot_plugin_statuscard import report as report_module

    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _Config({"display": {"timezone": "Asia/Shanghai"}})
    reporter._last_attempt_day = "2026-11-05"
    reporter._pending_retry = None
    reporter._pending_recovery_day = "2026-11-05"  # orphan marker
    reporter._last_sent_day = None
    reporter._daily_given_up_day = None
    reporter._daily_backfill_day = None
    reporter._targets = lambda: ["default:GroupMessage:1"]  # type: ignore[assignment]
    reporter._sections = lambda: ["resource"]  # type: ignore[assignment]
    reporter._latest_due_report_day = lambda: "2026-11-06"  # type: ignore[assignment]
    reporter._time_text = lambda: "21:00"  # type: ignore[assignment]
    monkeypatch.setattr(
        report_module,
        "now_in_timezone",
        lambda _config=None: datetime(2026, 11, 8, 0, 30, tzinfo=TZ),
    )

    assert reporter._daily_missed_window_day() == "2026-11-06"


def test_orphan_recovery_pin_does_not_block_evening_catchup() -> None:
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _Config({"display": {"timezone": "Asia/Shanghai"}})
    reporter._last_attempt_day = "2026-11-07"  # the report due this evening
    reporter._last_sent_day = "2026-11-06"
    reporter._pending_retry = None
    reporter._pending_recovery_day = "2026-11-05"  # orphan marker
    reporter._daily_given_up_day = None
    reporter._daily_db_rebuilt_day = None
    reporter._sections = lambda: ["resource"]  # type: ignore[assignment]

    assert reporter._daily_catch_up_allowed("2026-11-08") is True


def test_scheduled_send_ignores_a_report_day_older_than_last_sent() -> None:
    """A backwards wall-clock jump must not resend an older report."""

    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _Config({"display": {"timezone": "Asia/Shanghai"}})
    reporter._last_sent_day = "2026-11-01"
    reporter._last_attempt_day = None
    reporter._last_attempt_wall_day = None
    reporter._pending_recovery_day = None
    reporter._daily_backfill_inflight = False
    reporter._with_image = lambda: False  # type: ignore[assignment]
    reporter._targets = lambda: ["default:GroupMessage:1"]  # type: ignore[assignment]
    reporter._sections = lambda: ["resource"]  # type: ignore[assignment]
    reporter._store_ready = lambda: True  # type: ignore[assignment]
    reporter._report_window_lost = lambda *_a, **_k: False  # type: ignore[assignment]
    reporter._build_view = lambda *_a, **_k: {"day": "2026-10-31"}  # type: ignore[assignment]
    reporter._save_delivery_state = lambda: None  # type: ignore[assignment]
    reporter._record = lambda *_a, **_k: None  # type: ignore[assignment]
    cleared: list[bool] = []
    reporter._clear_retry = lambda *_a, **_k: cleared.append(True)

    asyncio.run(reporter._send_scheduled("2026-10-31"))
    assert cleared == [True]


# ----------------------------------------------------------------------
# 14) disk paths also honour the hostname rule on the card and dashboard
# ----------------------------------------------------------------------


def test_card_and_disk_path_mask_address_tokens() -> None:
    redactor = Redactor(["hostname"])
    view = {"disks": [{"path": "//192.168.1.5/share"}]}
    redacted = redactor.apply_card(view)
    assert "192.168.1.5" not in redacted["disks"][0]["path"]
    assert "share" in redacted["disks"][0]["path"]


def test_host_port_does_not_leave_a_dangling_port() -> None:
    redactor = Redactor(["hostname"])
    masked = redactor.mask_address_tokens("连接 internal.example.com:8080 失败")
    assert ":8080" not in masked
    assert "（已隐藏地址）" in masked


# ----------------------------------------------------------------------
# 15) verify results follow the configured order, not Task hash order
# ----------------------------------------------------------------------


class _VerifyMonitor:
    def __init__(self, count: int = 6) -> None:
        self.items = [
            {"__template_key": "http", "name": f"target-{index}", "url": "x"}
            for index in range(count)
        ]

    async def _probe(self, item):
        index = int(str(item["name"]).rsplit("-", 1)[1])
        # Later items finish first, so an unordered ``done`` set cannot
        # accidentally match the configured order.
        await asyncio.sleep((len(self.items) - index) * 0.01)
        return True, "ok", ""

    def _targets_for(self, kind="service"):
        return ["default:GroupMessage:1"]


class _VerifyPlugin:
    def __init__(self) -> None:
        self.config = {
            "display": {},
            "monitors": {"enabled": True, "items": []},
        }
        self.monitor = _VerifyMonitor()
        self.state = MemoryState()

    def audit_issues(self):
        return []


def test_verify_results_keep_the_configured_order(monkeypatch) -> None:
    async def request_value(name, default=None):
        return "monitors"

    monkeypatch.setattr(page_api_module, "_request_value", request_value)
    api = PageApi(_VerifyPlugin())
    payload = asyncio.run(api.verify())
    assert payload["ok"] is True
    names = [item["name"] for item in payload["results"]]
    assert names == [f"target-{index}" for index in range(6)]


# ----------------------------------------------------------------------
# 16) an unavailable hostname must not take /overview down
# ----------------------------------------------------------------------


def test_page_host_survives_gethostname_failure(monkeypatch) -> None:
    def boom():
        raise RuntimeError("no hostname in this sandbox")

    monkeypatch.setattr(socket, "gethostname", boom)
    api = PageApi(SimpleNamespace(config={"display": {}}))
    assert api._host() == ""
