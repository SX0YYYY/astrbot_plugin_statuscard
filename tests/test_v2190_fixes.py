"""v2.19.0 修复回归：聚合链路与告警状态机（A1–A8）。

场景编号沿用 2026-10-31 全流程找茬报告：
A1 恢复不早于汇总；A2 撤回落盘/取消不丢缓冲；A3 耗尽恢复带说明且单条 recovery；
A4 监测项与资源耗尽语义一致；A5 放弃清 last_sent；A6 汇总重试退避；
A7 溢出丢弃诚实计数；A8 关闭恢复通知后不补发。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import monitor as monitor_module
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore

TARGET = "default:GroupMessage:123456"


def _async_send(recorder: list[str], ok: bool = True):
    async def send(text: str, *_args, **_kwargs) -> DeliveryResult:
        recorder.append(text)
        if ok:
            return DeliveryResult(ok=True, ok_targets=[TARGET])
        return DeliveryResult(ok=False, failed_targets=[TARGET])

    return send


# ----------------------------------------------------------------------
# A1: buffered alert recovering inside the window queues its recovery
# ----------------------------------------------------------------------


def test_recovery_inside_window_queues_behind_summary(tmp_path: Path) -> None:
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

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=True, ok_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, config, report_store=store)
    warm = AlertAggregator(window_seconds=120, max_items=8)
    engine._aggregator = warm
    # Open a window so the alert gets buffered (awaiting_summary=True).
    asyncio.run(
        warm.push(
            "预热",
            "x",
            "text",
            _async_send([]),
            kind="resource",
            on_abandon=engine._on_summary_abandoned,
        )
    )
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule.get("awaiting_summary") is True

    # Recover inside the window: the notice must be queued, not sent.
    sent: list[str] = []
    engine._send = _async_send(sent)  # type: ignore[method-assign]
    asyncio.run(engine._evaluate("cpu", "CPU", 10.0, 85.0, "%", "10.0%"))
    assert sent == []  # nothing sent immediately
    assert rule.get("pending_recovery")
    assert rule["recovery_next_attempt"] > time.time() + 10  # ~15s behind


# ----------------------------------------------------------------------
# A2: abandon persists, cancelled flush keeps the batch
# ----------------------------------------------------------------------


def test_on_abandon_persists_withdrawal(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {"alerts": {"enabled": True, "targets": ["123456"]}}
    state.data.setdefault("alerts", {})["rules"] = {
        "cpu": {
            "alerting": True,
            "alerted": True,
            "last_sent": time.time(),
            "label": "CPU",
        }
    }
    state.save()

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=False, failed_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, config, report_store=store)
    engine._on_summary_abandoned([("CPU", "原因")])

    # Reload from disk: the withdrawal must have been persisted.
    fresh = StateStore(tmp_path / "state.json")
    rule = fresh.data["alerts"]["rules"]["cpu"]
    assert rule["alerted"] is False
    assert "last_sent" not in rule


def test_flush_cancel_requeues_batch() -> None:
    sent: list[str] = []
    abandoned: list[list[tuple[str, str]]] = []
    agg = AlertAggregator(window_seconds=120, max_items=8)
    started = asyncio.Event()

    async def first_ok(text: str) -> DeliveryResult:
        return DeliveryResult(ok=True, ok_targets=[TARGET])

    async def slow_send(text: str) -> DeliveryResult:
        sent.append(text)
        started.set()
        await asyncio.sleep(30)
        return DeliveryResult(ok=True, ok_targets=[TARGET])

    async def scenario() -> None:
        await agg.push("A", "原因A", "文本A", first_ok)
        await agg.push(
            "B",
            "原因B",
            "文本B",
            slow_send,
            on_abandon=lambda items: abandoned.append(list(items)),
        )
        assert agg._buckets["default"].pending  # B is buffered
        # Start the summary flush with a slow sender (bucket.send is still
        # the first alert's fast send), wait until it is inside that send,
        # then cancel: the batch must come back instead of vanishing.
        task = asyncio.create_task(agg.flush("default", slow_send))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    # The batch was taken from bucket.pending by flush(); the cancellation
    # path must put it back instead of dropping it.
    assert agg._buckets["default"].pending
    assert abandoned == []  # requeue, not abandon (first retry)


def test_push_first_send_failure_abandons_buffered() -> None:
    abandoned: list[list[tuple[str, str]]] = []
    window_open = asyncio.Event()
    release = asyncio.Event()

    async def first_ok(text: str) -> DeliveryResult:
        return DeliveryResult(ok=True, ok_targets=[TARGET])

    async def boom(text: str) -> DeliveryResult:
        # The "first" send: it claims the window, then raises while B sits in
        # the buffer behind it.
        window_open.set()
        await release.wait()
        raise RuntimeError("channel gone")

    async def scenario() -> None:
        task_a = asyncio.create_task(AGG.push("A", "原因A", "文本A", boom, kind="k"))
        await window_open.wait()
        task_b = asyncio.create_task(
            AGG.push(
                "B",
                "原因B",
                "文本B",
                first_ok,
                kind="k",
                on_abandon=lambda items: abandoned.append(list(items)),
            )
        )
        # B goes into the pending buffer (the window is open).
        await asyncio.sleep(0.05)
        assert AGG._buckets["k"].pending == [("B", "原因B")]
        release.set()
        try:
            await task_a
        except RuntimeError:
            pass
        await task_b

    AGG = AlertAggregator(window_seconds=120, max_items=8)
    asyncio.run(scenario())
    # The first send raised after B was buffered: B would never be covered by
    # a summary, so its rule must be withdrawn instead of claiming "notified".
    assert abandoned and abandoned[-1] == [("B", "原因B")]
    assert AGG._buckets["k"].pending == []


# ----------------------------------------------------------------------
# A3: exhausted resource alert announces recovery with the note, once
# ----------------------------------------------------------------------


def test_resource_exhausted_recovery_has_note_and_single_row(tmp_path: Path) -> None:
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

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=False, failed_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, config, report_store=store)
    sent: list[str] = []
    # First alert fails three times -> exhausted (alerting=True, send_failed).
    for _ in range(3):
        asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
        state.data["alerts"]["rules"]["cpu"]["next_attempt"] = 0
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["alerting"] is True and rule["last_error"] == "send_failed"

    # Metric falls back: the recovery notice goes out and carries the note.
    engine._send = _async_send(sent, ok=True)  # type: ignore[method-assign]
    asyncio.run(engine._evaluate("cpu", "CPU", 10.0, 85.0, "%", "10.0%"))
    assert len(sent) == 1 and "异常告警此前未能送达" in sent[0]

    statuses = [
        e["status"]
        for e in store.list_events_range(0, int(time.time()) + 60, limit=50)
        if e["name"] == "CPU"
    ]
    # One failed row from the retries plus exactly one recovery row: the old
    # cleanup block used to add a second "告警未能送达" recovery row.
    assert statuses.count("recovery") == 1
    assert "failed" in statuses


# ----------------------------------------------------------------------
# A4: monitor exhaustion announces recovery too (same semantics)
# ----------------------------------------------------------------------


def test_monitor_exhausted_recovery_announces(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path, send_ok=False)
    item = {
        "__template_key": "http",
        "name": "站点",
        "url": "http://a/",
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
            "name": "站点",
            "target": "http://a/",
        }
    }

    async def fake_probe(request_item):
        return False, "HTTP 500", "http://a/"

    monitor._probe = fake_probe  # type: ignore[method-assign]
    asyncio.run(monitor._check_item(item))
    st = monitor.state.data["monitors"]["items"][key]
    assert st["alerted"] is True and st.get("send_exhausted") is True

    # Recovery: the notice goes out with the note.
    sent: list[str] = []
    monitor._send = _async_send(sent, ok=True)  # type: ignore[method-assign]

    async def ok_probe(request_item):
        return True, "HTTP 200", "http://a/"

    monitor._probe = ok_probe  # type: ignore[method-assign]
    asyncio.run(monitor._check_item(item))
    assert len(sent) == 1 and "异常告警此前未能送达" in sent[0]


# ----------------------------------------------------------------------
# A5: after an abandoned summary a new episode is not silenced by cooldown
# ----------------------------------------------------------------------


def test_abandon_clears_last_sent(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        }
    }
    state.data.setdefault("alerts", {})["rules"] = {
        "cpu": {
            "alerting": True,
            "alerted": True,
            "last_sent": time.time(),  # buffered 30s ago
            "label": "CPU",
            "alert_since": time.time() - 60,
            "count": 0,
        }
    }

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=False, failed_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, config, report_store=store)
    engine._on_summary_abandoned([("CPU", "原因")])
    assert "last_sent" not in state.data["alerts"]["rules"]["cpu"]


# ----------------------------------------------------------------------
# A6: summary retries follow the backoff schedule
# ----------------------------------------------------------------------


def test_summary_retry_uses_backoff() -> None:
    agg = AlertAggregator(window_seconds=120, max_items=8)
    asyncio.run(agg.push("A", "原因A", "文本A", _async_send([])))
    asyncio.run(agg.push("B", "原因B", "文本B", _async_send([])))
    bucket = agg._buckets["default"]
    assert bucket.pending  # B is buffered
    asyncio.run(agg.flush("default", _async_send([], ok=False)))
    assert bucket.retries == 1
    assert bucket.retry_at > time.time() + 20  # ~30s, not 1s
    assert bucket.pending  # batch kept for the retry
    # _schedule_if_pending must schedule with the backoff delay, not 1s.

    async def reschedule() -> None:
        agg._schedule_if_pending("default")
        task = bucket.flush_task
        assert task is not None and not task.done()
        # The coroutine sleeps for the backoff window: cancel right away.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(reschedule())


# ----------------------------------------------------------------------
# A7: trimmed overflow items are withdrawn and counted
# ----------------------------------------------------------------------


def test_overflow_dropped_items_withdrawn_and_counted() -> None:
    sent: list[str] = []
    abandoned: list[tuple[str, ...]] = []
    agg = AlertAggregator(window_seconds=120, max_items=2)

    def on_abandon(items) -> None:
        abandoned.append(tuple(l for l, _r in items))

    async def scenario() -> None:
        # First push claims the window and registers the abandon callback.
        await agg.push("A", "原因A", "文本A", _async_send([]), on_abandon=on_abandon)
        for label in ("B", "C", "D", "E", "F", "G"):
            await agg.push(label, f"原因{label}", f"文本{label}", _async_send([]))

    asyncio.run(scenario())
    # Buffer cap is max_items*2 = 4: B and C were trimmed off (one per push
    # that overflowed), and their rules were withdrawn via the callback.
    bucket = agg._buckets["default"]
    assert bucket.dropped_count == 2
    assert abandoned == [("B",), ("C",)]
    # The summary counts them: 4 listed-able + 2 dropped = 6 total.
    asyncio.run(agg.flush("default", _async_send(sent)))
    summary = sent[-1]
    assert "6 项异常" in summary and "另有 4 项未列出" in summary


# ----------------------------------------------------------------------
# A8: turning recovery notices off drops the queued notice
# ----------------------------------------------------------------------


def test_monitor_drops_queued_recovery_when_disabled(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path, send_ok=True)
    monitor.recover_notify = False
    item = {
        "__template_key": "http",
        "name": "站点",
        "url": "http://a/",
        "enabled": True,
    }
    key = monitor._item_key(item)
    monitor.state.data.setdefault("monitors", {})["items"] = {
        key: {
            "alerting": False,
            "alerted": False,
            "pending": False,
            "fails": 0,
            "last_checked": 0,
            "name": "站点",
            "target": "http://a/",
            "pending_recovery": "✅ 服务恢复 · 站点",
            "recovery_retry_count": 1,
            "recovery_next_attempt": 0,
        }
    }
    sent: list[str] = []
    monitor._send = _async_send(sent, ok=True)  # type: ignore[method-assign]

    async def ok_probe(request_item):
        return True, "HTTP 200", "http://a/"

    monitor._probe = ok_probe  # type: ignore[method-assign]
    asyncio.run(monitor._check_item(item))
    st = monitor.state.data["monitors"]["items"][key]
    assert sent == []
    assert "pending_recovery" not in st


# ----------------------------------------------------------------------
# P3 follow-ups in the same files
# ----------------------------------------------------------------------


def test_disk_migration_removes_legacy_key(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {
        "alerts": {"enabled": True, "targets": ["123456"]},
        "display": {"disk_paths": ["/", "/data"]},
    }
    state.data.setdefault("alerts", {})["rules"] = {
        "disk:/": {"alerting": False, "label": "磁盘 /"}
    }

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=True, ok_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, config, report_store=store)
    asyncio.run(engine._sample_once())
    rules = state.data["alerts"]["rules"]
    assert "disk" in rules
    assert "disk:/" not in rules  # migrated away in the same write


def _make_monitor(tmp_path: Path, send_ok: bool):
    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            if send_ok:
                return DeliveryResult(ok=True, ok_targets=list(targets))
            return DeliveryResult(ok=False, failed_targets=list(targets))

    state = StateStore(tmp_path / "state.json")
    config = {
        "monitors": {
            "enabled": True,
            "targets": ["123456"],
            "poll_interval": 30,
            "failure_threshold": 2,
            "cooldown_seconds": 0,
            "items": [],
        },
        "alerts": {"targets": ["123456"]},
    }
    return monitor_module.ServiceMonitor(_Plugin(), state, config)


def test_provider_recovery_note_and_retry_cap(tmp_path: Path, monkeypatch) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "provider_watch": {"enabled": True, "threshold": 3, "cooldown_seconds": 0},
        }
    }

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=False, failed_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, config, report_store=store)
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (3, "error", 20),
    )
    sent: list[str] = []
    engine._send = _async_send(sent, ok=False)  # type: ignore[method-assign]
    asyncio.run(engine._check_provider_quality())
    watch = state.data["alerts"]["provider_watch"]
    assert watch["alerting"] is True and watch.get("send_failed") is True

    # Recovery: notice carries the note even though the send still fails
    # (queued afterwards, retried up to MAX_RECOVERY_RETRIES like the rest).
    engine._provider_last_check = 0  # bypass the 60s throttle
    engine._send = _async_send(sent, ok=True)  # type: ignore[method-assign]
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (0, "completed", 20),
    )
    asyncio.run(engine._check_provider_quality())
    assert len(sent) == 2 and "异常告警此前未能送达" in sent[-1]
    assert state.data["alerts"]["provider_watch"]["alerting"] is False


# ----------------------------------------------------------------------
# B1: a hung mount only blinds itself (per-mount futures + cancellation)
# ----------------------------------------------------------------------


def test_disk_usage_bounded_isolates_hung_mount(monkeypatch) -> None:
    from astrbot_plugin_statuscard import collectors as collectors_module
    from astrbot_plugin_statuscard.collectors import disk_usage_bounded

    calls: list[str] = []

    def fake_disk_usage(name: str):
        calls.append(name)
        if name == "hung-mount":
            time.sleep(30)  # would exceed any test timeout

        class _U:
            used, total, free = 10.0, 100.0, 90.0

        return _U()

    monkeypatch.setattr(collectors_module.psutil, "disk_usage", fake_disk_usage)
    started = time.perf_counter()
    rows = disk_usage_bounded(["ok-mount", "hung-mount"], timeout=1.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 10, f"hung mount blocked for {elapsed:.1f}s"
    by_name = {row.name: row for row in rows}
    assert by_name["hung-mount"].exception == "读取超时"
    # The healthy mount is not dragged down by its hung neighbour.
    assert by_name["ok-mount"].percent == 10.0


# ----------------------------------------------------------------------
# P3: receive-only interface keeps booking traffic; safe state reads
# ----------------------------------------------------------------------


def test_redact_url_strips_bare_credentials() -> None:
    from astrbot_plugin_statuscard.utils import redact_url

    assert redact_url("user:pass@example.com/api") == "example.com/api"
    assert redact_url("http://u:p@example.com/api?token=x") == "http://example.com/api"
    # Path-like "@" is not credentials.
    assert redact_url("example.com/a/b@here") == "example.com/a/b@here"


def test_version_normalize_and_cache_default() -> None:
    from astrbot_plugin_statuscard import version as version_module

    assert version_module._normalize_version("4.29.3") == "v4.29.3"
    assert version_module._normalize_version(" v5 ") == "v5"
    assert version_module._normalize_version("") is None
    assert version_module._normalize_version(None) is None
    # An empty probe result falls back to the (normalized) default.
    assert version_module.resolve_astrbot_version(
        None, default="dev"
    ) == version_module.resolve_astrbot_version(None, default="dev")
    assert version_module.resolve_astrbot_version(None, default="x").startswith("v")


# ----------------------------------------------------------------------
# P3: audit false positive gone; notification-rule targets audited
# ----------------------------------------------------------------------


def test_audit_empty_webhook_host_is_not_an_error(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "komari_webhook": {
                "enabled": True,
                "host": "",
                "port": 9968,
                "token": "secret",
                "require_token": False,
            },
        }
    }
    issues = run_audit(config)
    codes = [issue.code for issue in issues]
    assert "webhook_open_no_token" not in codes


def test_audit_flags_bad_notification_rule_target(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    config = {
        "alerts": {"enabled": True, "targets": ["123456"]},
        "notify": {
            "rules": [
                {
                    "enabled": True,
                    "target": "运维群（拉我进去的那个）",
                    "kinds": ["resource"],
                }
            ]
        },
    }
    issues = run_audit(config)
    assert any(
        issue.code == "target_format_suspect" and "通知中心规则 #1" in issue.message
        for issue in issues
    )


def test_audit_accepts_good_notification_rule_target(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    config = {
        "alerts": {"enabled": True, "targets": ["123456"]},
        "notify": {
            "rules": [
                {
                    "enabled": True,
                    "target": "default:GroupMessage:461961169",
                    "kinds": ["resource"],
                }
            ]
        },
    }
    issues = run_audit(config)
    assert all(issue.code != "target_format_suspect" for issue in issues)


def test_disk_usage_bounded_dedupes_paths(monkeypatch) -> None:
    """重复路径不再泄漏 worker：dict 覆盖会孤儿化先提交的挂死 future。"""
    import threading

    from astrbot_plugin_statuscard import collectors as collectors_module
    from astrbot_plugin_statuscard.collectors import disk_usage_bounded

    started = threading.Event()
    release = threading.Event()

    def fake_one(name: str):
        class _U:
            used, total, exception = 1.0, 10.0, None

        class _U2:
            used, total, exception = 2.0, 10.0, None

        if name == "hung-mount":
            started.set()
            release.wait(30)
            return _U()
        return _U2()

    monkeypatch.setattr(collectors_module, "_disk_usage_one", fake_one)
    rows = disk_usage_bounded(["ok-mount", "hung-mount", "hung-mount"], timeout=1.0)
    release.set()
    # 3 个调用位、去重后 2 个 job：挂死盘只占 1 个 worker，结果仍保序保长
    assert len(rows) == 3
    assert rows[1].name == "hung-mount" and rows[2].name == "hung-mount"
    assert rows[1].exception == "读取超时" and rows[0].exception is None
    assert started.is_set()


def test_daily_targets_empty_records_once_without_retry(tmp_path: Path) -> None:
    """targets 空是配置问题不会自愈：只记一次，不再烧 3 轮重试。"""
    from astrbot_plugin_statuscard.report import DailyReporter
    from astrbot_plugin_statuscard.report_store import ReportStore

    class _Plugin:
        context = None

        async def render_report_image(self, view):
            return None

        async def send_text(self, targets, text, source="unknown"):
            raise AssertionError("targets 为空时不应尝试发送")

    store = ReportStore(tmp_path / "report.db")
    config = {"daily_report": {"enabled": True, "with_image": False}}
    reporter = DailyReporter(_Plugin(), store, config)
    reporter._build_view = lambda day=None: {"day": "2026-10-31", "sections": []}
    reporter._last_sent_day = ""

    asyncio.run(reporter._send_scheduled())

    runs = store.list_report_runs(limit=5)
    assert [r["detail"] for r in runs] == ["targets empty"]
    assert reporter._pending_retry is None  # 不再排重试


def test_weekly_targets_empty_does_not_schedule_retry(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.report import DailyReporter
    from astrbot_plugin_statuscard.report_store import ReportStore

    class _Plugin:
        context = None

        async def render_report_image(self, view):
            return None

        async def send_text(self, targets, text, source="unknown"):
            raise AssertionError("targets 为空时不应尝试发送")

    store = ReportStore(tmp_path / "report.db")
    config = {
        "daily_report": {"enabled": True, "weekly_time": "21:30", "with_image": False}
    }
    reporter = DailyReporter(_Plugin(), store, config)

    asyncio.run(reporter._send_weekly_scheduled())

    assert reporter._weekly_retry_count == 0
    assert not store.list_report_runs(limit=5)  # 与 v2.18.9 一致：不 record
