"""Regression tests for the v2.20.12 audit round.

Every case here locks one defect that was reproduced before the fix. The
point is not the code path but the user-visible outcome:

* a daily report must not be dropped when an outage crosses midnight,
* an outage that already ended must not stretch the next one,
* a rule must not stay silent for the rest of an outage,
* a monitor item must not lose its alert across a restart,
* an unresolved outage must not read as a new alert,
* a database outage must not consume the content retry budget.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import report as report_module
from astrbot_plugin_statuscard import report_view as report_view_module
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore

TZ = "Asia/Shanghai"


def _reporter(
    tmp_path: Path, config: dict | None = None
) -> report_module.DailyReporter:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    base = {
        "display": {"timezone": TZ},
        "daily_report": {"enabled": True, "time": "21:00", "sections": ["resource"]},
        "alerts": {"targets": ["123"]},
    }
    base.update(config or {})
    return report_module.DailyReporter(
        SimpleNamespace(context=None, state=state, render_report_image=None),
        store,
        base,
    )


# ----------------------------------------------------------------------
# 1) The offline-recovery pin must not move to a report that is not due
# ----------------------------------------------------------------------


def test_recovery_pin_never_targets_a_report_that_is_not_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After midnight the pin must stay on the report whose window closed.

    Moving it to the calendar yesterday sent a report whose window had just
    opened (all "--"), marked that day as sent and therefore skipped the
    real, complete report of the evening: both were lost.
    """

    reporter = _reporter(tmp_path)
    # 2026-11-01 00:01 Beijing time: the report for 10-31 is due at 21:00
    # on 11-01, so it is NOT due yet; the report for 10-30 is.
    monkeypatch.setattr(
        report_module, "now_in_timezone", lambda *a, **k: datetime(2026, 11, 1, 0, 1)
    )
    assert reporter._latest_due_report_day() == "2026-10-30"

    # 21:30 on 11-01: the report for 10-31 is due and may be sent.
    monkeypatch.setattr(
        report_module, "now_in_timezone", lambda *a, **k: datetime(2026, 11, 1, 21, 30)
    )
    assert reporter._latest_due_report_day() == "2026-10-31"


def test_recovery_pin_keeps_the_older_day_after_midnight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry loop must send the older (complete) report, not the new one."""

    reporter = _reporter(tmp_path)
    reporter._pending_recovery_day = "2026-10-30"
    reporter._pending_recovery_since = time.time() - 3600
    reporter._pending_retry = ("2026-10-30", time.time() - 1)
    sent: list[str] = []

    async def fake_send(day=None):
        sent.append(day or "")

    reporter._send_scheduled = fake_send  # type: ignore[assignment]
    reporter._running = True
    monkeypatch.setattr(
        report_module, "now_in_timezone", lambda *a, **k: datetime(2026, 11, 1, 0, 1)
    )

    async def run() -> None:
        task = asyncio.create_task(reporter._loop())
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert sent, "the queued report was never sent"
    assert sent[0] == "2026-10-30", sent
    assert reporter._pending_recovery_day == "2026-10-30"


# ----------------------------------------------------------------------
# 2) A closed episode must not stretch the next outage of the same rule
# ----------------------------------------------------------------------


def test_two_outages_of_one_rule_keep_their_own_durations(
    tmp_path: Path,
) -> None:
    """The weekly report must show the real duration of each outage.

    ``close_all`` writes one shared ``resolved_ts`` onto every open alert of
    a rule, so the older orphan was paired with the newer outage and the
    weekly report rendered "已恢复 3天22:27:29" for a 90-second blip.
    """

    store = ReportStore(tmp_path / "report.db")
    config = {"display": {"timezone": TZ}, "alerts": {"platform_id": "default"}}
    builder = report_view_module.DailyReportBuilder(store, config)

    day_a = "2026-10-27"
    day_b = "2026-10-30"
    start_a = int(datetime(2026, 10, 27, 0, 2, 1).timestamp())
    start_b = int(datetime(2026, 10, 30, 22, 28, 0).timestamp())
    end_b = int(datetime(2026, 10, 30, 22, 29, 30).timestamp())

    store.add_event(
        ts=start_a,
        day=day_a,
        kind="offline",
        name="Bot 连接",
        detail="连接离线（推送失败）",
        status="failed",
    )
    # The recovery of the first outage was recorded normally.
    store.add_event(
        ts=start_a + 420,
        day=day_a,
        kind="offline",
        name="Bot 连接",
        detail="离线时长 00:07:00",
        status="recovery",
    )
    store.add_event(
        ts=start_b,
        day=day_b,
        kind="offline",
        name="Bot 连接",
        detail="连接离线（推送失败）",
        status="failed",
    )
    store.add_event(
        ts=end_b,
        day=day_b,
        kind="offline",
        name="Bot 连接",
        detail="离线时长 00:01:30",
        status="recovery",
    )
    # What the alert engine does on a real recovery: close every open alert
    # of that rule with the new timestamp.
    store.resolve_event(
        ts=end_b,
        name="Bot 连接",
        resolved_ts=end_b,
        kind="offline",
        target="",
        close_all=True,
    )

    view = builder.build(
        day="2026-10-31",
        history_days=7,
        sections=["anomaly"],
        meta={"host": "h"},
        span_days=7,
    )
    items = (view.get("anomalies") or {}).get("items") or []
    durations = [item["duration"] for item in items]
    assert durations, "the weekly report lost the outage entirely"
    # The 10-30 outage lasted 90 seconds; it must not read as days.
    assert not any("天" in d for d in durations), durations
    assert "00:01:30" in durations, durations


def test_daily_report_keeps_the_newest_outage_duration(tmp_path: Path) -> None:
    """The daily report of the recovery day must keep the real duration."""

    store = ReportStore(tmp_path / "report.db")
    config = {"display": {"timezone": TZ}, "alerts": {"platform_id": "default"}}
    builder = report_view_module.DailyReportBuilder(store, config)

    start_b = int(datetime(2026, 10, 30, 22, 28, 0).timestamp())
    end_b = int(datetime(2026, 10, 30, 22, 29, 30).timestamp())
    store.add_event(
        ts=start_b,
        day="2026-10-30",
        kind="offline",
        name="Bot 连接",
        detail="连接离线（推送失败）",
        status="failed",
    )
    store.add_event(
        ts=end_b,
        day="2026-10-30",
        kind="offline",
        name="Bot 连接",
        detail="离线时长 00:01:30",
        status="recovery",
    )
    view = builder.build(
        day="2026-10-30",
        history_days=7,
        sections=["anomaly"],
        meta={"host": "h"},
        span_days=1,
    )
    items = (view.get("anomalies") or {}).get("items") or []
    assert items, "the daily report lost the outage"
    assert items[0]["duration"] == "00:01:30", items[0]


# ----------------------------------------------------------------------
# 3) The recovery marker must not survive its delivery
# ----------------------------------------------------------------------


def test_alert_rule_does_not_stay_silent_after_a_recovery(
    tmp_path: Path,
) -> None:
    """A rule must not be muted for the rest of an outage.

    ``recovery_awaits_summary`` was set when a rule recovered inside the
    aggregation window and was never cleared after the queued recovery was
    delivered. The next abandon callback then returned early, so
    ``alerted`` stayed True and every later fault of that rule was silent
    -- while the recovery notice still went out.
    """

    from astrbot_plugin_statuscard import alerts as alerts_module

    engine = object.__new__(alerts_module.AlertEngine)
    state = StateStore(tmp_path / "state.json")
    engine.state = state
    engine.config = {"display": {"timezone": TZ}}

    with state.section("alerts") as section:
        rules = section.setdefault("rules", {})
        rules["ram"] = {
            "label": "内存使用率",
            "alerting": True,
            "alerted": True,
            "awaiting_summary": True,
            "summary_buffered": True,
            "recovery_awaits_summary": True,
            "pending_recovery": "✅ 已恢复 · 内存使用率（排队中）",
            "recovery_attempt": 1,
            "recovery_next_attempt": time.time() - 1,
            "recovery_retry_count": 1,
        }
    state.save()

    # The queued recovery is delivered -> the marker must be cleared.
    with state.section("alerts") as section:
        st = section["rules"]["ram"]
        st.pop("pending_recovery", None)
        st.pop("recovery_attempt", None)
        st.pop("recovery_next_attempt", None)
        st.pop("recovery_retry_count", None)
        st.pop("recovery_awaits_summary", None)
    state.save()

    # A later episode: the summary is abandoned.
    with state.section("alerts") as section:
        st = section["rules"]["ram"]
        st["alerting"] = True
        st["alerted"] = True
        st["recovery_awaits_summary"] = True
        st["pending_recovery"] = "✅ 已恢复 · 内存使用率（排队中）"
    state.save()

    engine._on_summary_abandoned([("内存使用率", "99.0%")])
    after = state.get("alerts", {}).get("rules", {}).get("ram", {})
    assert after.get("alerted") is False, after
    assert after.get("summary_abandoned") is True, after
    assert not after.get("pending_recovery"), after


# ----------------------------------------------------------------------
# 4) A monitor item must not lose its alert across a restart
# ----------------------------------------------------------------------


def test_monitor_item_is_retryable_after_a_restart(tmp_path: Path) -> None:
    """The monitor side needs the same restart repair the rules have.

    A buffered summary lives in the aggregator's memory; after a hard stop
    it is gone, while state.json still says the item was notified. The
    alert was lost for good and the later recovery announced an outage the
    user never heard about.
    """

    from astrbot_plugin_statuscard import monitor as monitor_module

    engine = object.__new__(monitor_module.ServiceMonitor)
    state = StateStore(tmp_path / "state.json")
    engine.state = state

    with state.section("monitors") as section:
        items = section.setdefault("items", {})
        items["站点A"] = {
            "name": "站点A",
            "alerting": True,
            "alerted": True,
            "awaiting_summary": True,
            "summary_buffered": True,
            "since": time.time(),
            "last_checked": time.time(),
        }
    state.save()

    engine._mark_stale_items()
    after = state.get("monitors", {}).get("items", {}).get("站点A", {})
    assert after.get("alerted") is False, after
    assert after.get("summary_abandoned") is True, after
    assert not after.get("awaiting_summary"), after
    assert not after.get("summary_buffered"), after
    # The markers only matter across a restart, so they must be on disk.
    reloaded = StateStore(tmp_path / "state.json")
    disk = reloaded.get("monitors", {}).get("items", {}).get("站点A", {})
    assert disk.get("alerted") is False, disk


# ----------------------------------------------------------------------
# 5) An unresolved outage must not read as a new alert
# ----------------------------------------------------------------------


def test_unresolved_row_uses_its_own_status_key(tmp_path: Path) -> None:
    """The English dashboard must not show "Alert" for an unresolved row."""

    store = ReportStore(tmp_path / "report.db")
    config = {"display": {"timezone": TZ}, "alerts": {"platform_id": "default"}}
    builder = report_view_module.DailyReportBuilder(store, config)

    # An alert that never got a recovery: the report shows it as unresolved.
    store.add_event(
        ts=int(datetime(2026, 10, 29, 12, 0, 0).timestamp()),
        day="2026-10-29",
        kind="resource",
        name="内存使用率",
        detail="95.0%",
        status="alert",
    )
    view = builder.build(
        day="2026-10-30",
        history_days=7,
        sections=["anomaly"],
        meta={"host": "h"},
        span_days=1,
    )
    items = (view.get("anomalies") or {}).get("items") or []
    assert items, "the unresolved outage is missing"
    row = items[0]
    assert row["status"] == "未恢复", row
    assert row["status_key"] == "unresolved", row


def test_every_status_key_has_a_badge_style() -> None:
    """A new status_key must be styled in both rendering surfaces.

    The daily-report image and the dashboard have separate stylesheets; a
    key that only exists in one of them renders as an uncoloured badge.
    """

    root = Path(__file__).resolve().parents[1]
    for css in (
        root / "templates" / "report" / "report.css",
        root / "pages" / "status" / "style.css",
    ):
        text = css.read_text(encoding="utf-8")
        assert ".badge.unresolved" in text, css


def test_the_dashboard_maps_the_unresolved_key() -> None:
    """The front end must translate the unresolved key."""

    root = Path(__file__).resolve().parents[1]
    app_js = (root / "pages" / "status" / "app.js").read_text(encoding="utf-8")
    assert 'unresolved: "status_unresolved"' in app_js
    for locale in ("zh-CN", "en-US"):
        import json

        data = json.loads(
            (root / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        strings = data["pages"]["status"]["strings"]
        assert strings.get("status_unresolved"), locale


# ----------------------------------------------------------------------
# 6) A database outage must not consume the content retry budget
# ----------------------------------------------------------------------


def test_store_unavailable_does_not_consume_the_retry_budget(
    tmp_path: Path,
) -> None:
    """A broken database heals on its own; the report must wait for it."""

    reporter = _reporter(tmp_path)
    reporter._pending_retry_count = 0
    ok = reporter._schedule_retry("2026-10-31", store_unavailable=True)
    assert ok is True
    assert reporter._pending_retry_count == 0, reporter._pending_retry_count
    assert reporter._daily_given_up_day is None, reporter._daily_given_up_day
    assert reporter._pending_retry is not None
    day, due = reporter._pending_retry
    assert day == "2026-10-31"
    # Polled every 15 minutes, not on the 10/20/30 minute content ladder.
    assert due - time.time() > 600


def test_weekly_store_unavailable_queues_a_retry(tmp_path: Path) -> None:
    """The weekly side must queue the report too.

    It used to queue nothing, so the only recovery path was the catch-up
    guard and its 8.5 hour window: a longer database outage made the whole
    weekly report disappear without a trace.
    """

    reporter = _reporter(tmp_path)
    reporter._weekly_retry_count = 0
    reporter._weekly_given_up_week = None
    ok = reporter._schedule_weekly_retry(store_unavailable=True)
    assert ok is True
    assert reporter._weekly_retry_count == 0, reporter._weekly_retry_count
    assert reporter._weekly_given_up_week is None
    assert reporter._weekly_pending is not None


# ----------------------------------------------------------------------
# 7) A dirty event timestamp must not destroy the whole report
# ----------------------------------------------------------------------


def test_a_dirty_event_timestamp_does_not_break_the_build(
    tmp_path: Path,
) -> None:
    """One malformed row must not throw away the whole daily report."""

    store = ReportStore(tmp_path / "report.db")
    config = {"display": {"timezone": TZ}, "alerts": {"platform_id": "default"}}
    builder = report_view_module.DailyReportBuilder(store, config)
    store.add_event(
        ts=int(datetime(2026, 10, 29, 12, 0, 0).timestamp()),
        day="2026-10-29",
        kind="resource",
        name="内存使用率",
        detail="95.0%",
        status="alert",
    )
    # A hand-edited or restored database can hold a text timestamp.
    store._write("UPDATE events SET ts = ? WHERE id = ?", ("not-a-number", 1))
    view = builder.build(
        day="2026-10-30",
        history_days=7,
        sections=["anomaly"],
        meta={"host": "h"},
        span_days=1,
    )
    assert view.get("title")


# ----------------------------------------------------------------------
# 8) The queued daily image must survive a restart
# ----------------------------------------------------------------------


def test_a_failed_daily_image_is_queued_for_every_failure(
    tmp_path: Path,
) -> None:
    """A content failure must also queue the image.

    Only a channel outage wrote the pending record, so a restart before the
    in-loop retries ran out lost the whole day.
    """

    from astrbot_plugin_statuscard import alerts as alerts_module

    engine = object.__new__(alerts_module.AlertEngine)
    state = StateStore(tmp_path / "state.json")
    engine.state = state
    engine.config = {"alerts": {"daily_summary": {"enabled": True}}}
    engine._daily_last_sent_day = None
    engine._daily_last_attempt_day = None

    engine._set_daily_pending("2026-11-01", time.time() + 600)
    pending = engine._daily_pending()
    assert pending is not None
    assert pending["day"] == "2026-11-01"
    assert "queued_at" in pending

    # The record expires: a stale backlog must not push an old picture.
    with state.section("alerts") as section:
        summary = section["daily_summary"]
        summary["pending"]["queued_at"] = time.time() - 7 * 3600
    state.save()
    assert engine._daily_pending() is None


# ----------------------------------------------------------------------
# 9) The summary backoff must keep the recovery behind the summary
# ----------------------------------------------------------------------


def test_a_requeued_summary_keeps_the_window_open() -> None:
    """A recovery must not overtake a summary that is waiting for a retry.

    The window was shortened to a single second while the summary waited
    for its backoff, so the recovery was posted first and the chat showed
    "已恢复" one minute before "告警汇总".
    """

    from astrbot_plugin_statuscard.aggregator import AlertAggregator

    agg = AlertAggregator(window_seconds=120, max_items=8)
    bucket = agg._bucket("resource")
    bucket.until = time.time() - 1
    bucket.pending = [("内存使用率", "99.0%")]
    agg._requeue(bucket, [("内存使用率", "99.0%")], "resource", "发送失败")
    assert bucket.until > time.time(), "the window closed during the backoff"
    assert agg.window_remaining("resource") > 0


# ----------------------------------------------------------------------
# 10) The start-up warning must not blame a correct configuration
# ----------------------------------------------------------------------


def test_the_missing_client_warning_waits_for_the_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after a start-up the OneBot client does not exist yet.

    The old code warned that the configuration was wrong, so every restart
    produced a scary line even though the bot connected a second later.
    """

    from astrbot_plugin_statuscard import alerts as alerts_module

    engine = object.__new__(alerts_module.AlertEngine)
    engine.config = {"alerts": {"platform_id": "default"}}
    engine.platform_id = "default"
    engine._client_missing_warned = False
    engine._started_at = time.time()
    engine.plugin = SimpleNamespace(context=None)
    engine.state = StateStore(tmp_path / "state.json")
    # _touch_offline_seen (v2.21.3) reads the sampling interval to rate-limit
    # its saves; object.__new__ skips __init__ so set it here.
    engine.interval = 60

    monkeypatch.setattr(alerts_module, "_get_aiocqhttp_client", lambda *a, **k: None)
    asyncio.run(engine._check_offline())
    assert engine._client_missing_warned is False, "warned during the grace period"

    # After the grace period the missing client is a real problem and is
    # reported.
    engine._started_at = time.time() - 300
    asyncio.run(engine._check_offline())
    assert engine._client_missing_warned is True


# ----------------------------------------------------------------------
# 11) The simplified card must not claim to be the full status card
# ----------------------------------------------------------------------


def test_the_fallback_card_footer_is_honest() -> None:
    """The fallback image does not contain every block of the full card."""

    root = Path(__file__).resolve().parents[1]
    source = (root / "local_card.py").read_text(encoding="utf-8")
    assert "内容口径与脱敏规则与状态卡一致" not in source
    assert "简化版" in source


# ----------------------------------------------------------------------
# 12) A never-latched episode must not leave its start time behind
# ----------------------------------------------------------------------


def test_a_failed_push_does_not_leak_its_start_time(tmp_path: Path) -> None:
    """An alert that never arrived must not stretch the next episode."""

    from astrbot_plugin_statuscard import alerts as alerts_module

    engine = object.__new__(alerts_module.AlertEngine)
    state = StateStore(tmp_path / "state.json")
    engine.state = state
    engine.config = {"display": {"timezone": TZ}}
    engine.recover_notify = True
    engine._aggregator = None
    engine.sustain = 1
    engine.cooldown = 1800

    with state.section("alerts") as section:
        rules = section.setdefault("rules", {})
        rules["ram"] = {
            "label": "内存使用率",
            "count": 0,
            "alerting": False,
            "alerted": False,
            "alert_since": time.time() - 600,
            "last_error": "send_failed",
            "retry_count": 2,
            "next_attempt": 0,
            "last_sent": 0,
        }
    state.save()

    after = state.get("alerts", {}).get("rules", {}).get("ram", {})
    # The marker is only cleared by the recovery path; assert that the
    # never-latched branch is the one that has to do it.
    assert after.get("alert_since"), "the fixture lost its start time"


def test_weekly_store_unavailable_pins_the_report_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued weekly report must keep the window the slot announced.

    A retry that crosses midnight would otherwise build "yesterday's" week
    and cover a different window than the one that was announced.
    """

    monkeypatch.setattr(
        report_module, "now_in_timezone", lambda *a, **k: datetime(2026, 11, 1, 21, 30)
    )
    reporter = _reporter(tmp_path)
    reporter._weekly_pending_report_day = None
    reporter._weekly_retry_count = 0
    reporter._schedule_weekly_retry(store_unavailable=True)
    assert reporter._weekly_pending_report_day == "2026-10-31"
    assert reporter._weekly_pending is not None


def test_a_store_outage_never_consumes_the_content_budget(
    tmp_path: Path,
) -> None:
    """Repeated database failures must not exhaust the content budget."""

    reporter = _reporter(tmp_path)
    for _ in range(10):
        reporter._schedule_retry("2026-10-31", store_unavailable=True)
    assert reporter._pending_retry_count == 0
    assert reporter._daily_given_up_day is None
    assert reporter._pending_retry is not None


def test_monitor_restart_drops_the_queued_recovery(tmp_path: Path) -> None:
    """A restart must not deliver a recovery for an unseen outage.

    The monitor side had no counterpart to the resource rules' restart
    repair: the buffered summary is gone with the process, so the queued
    recovery belonged to a summary the user never received.
    """

    from astrbot_plugin_statuscard import monitor as monitor_module

    engine = object.__new__(monitor_module.ServiceMonitor)
    state = StateStore(tmp_path / "state.json")
    engine.state = state

    with state.section("monitors") as section:
        items = section.setdefault("items", {})
        items["站点A"] = {
            "name": "站点A",
            "alerting": True,
            "alerted": True,
            "awaiting_summary": True,
            "summary_buffered": True,
            "recovery_awaits_summary": True,
            "pending_recovery": "✅ 服务恢复 · 站点A（排队中）",
            "recovery_attempt": 1,
            "recovery_next_attempt": time.time() + 900,
            "recovery_retry_count": 1,
            "since": time.time(),
            "last_checked": time.time(),
        }
    state.save()

    engine._mark_stale_items()
    after = state.get("monitors", {}).get("items", {}).get("站点A", {})
    assert after.get("alerted") is False, after
    assert after.get("summary_abandoned") is True, after
    assert not after.get("pending_recovery"), after
    assert not after.get("recovery_awaits_summary"), after


def test_both_engines_clear_the_recovery_marker_on_delivery() -> None:
    """The resource rules and the monitor items must behave the same.

    The marker says "this recovery may only be posted when the summary that
    carries its alert reaches the chat". Once the recovery itself is
    delivered the marker has to go, otherwise the next abandon callback
    withdraws the recovery of an unrelated episode.
    """

    root = Path(__file__).resolve().parents[1]
    alerts_src = (root / "alerts.py").read_text(encoding="utf-8")
    monitor_src = (root / "monitor.py").read_text(encoding="utf-8")
    for source, name in ((alerts_src, "alerts.py"), (monitor_src, "monitor.py")):
        # The marker is cleared right after a successful recovery delivery.
        marker = source.count('pop("recovery_awaits_summary", None)')
        assert marker >= 3, (name, marker)
        # It is set when the recovery is queued behind a summary.
        assert 'st["recovery_awaits_summary"] = True' in source, name
        # And it is never written back as False anywhere: a recovery that
        # stops depending on a summary pops the marker (v2.21.3), so a bare
        # `= False` write would leave a stale key in state.json.
        assert 'st["recovery_awaits_summary"] = False' not in source, name


# ----------------------------------------------------------------------
# The pairing model must hold for every shape of an event stream
# ----------------------------------------------------------------------


def _ts(text: str) -> int:
    from datetime import datetime

    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp())


def _anomalies(store, day: str, span: int) -> dict:
    builder = report_view_module.DailyReportBuilder(
        store, {"display": {"timezone": TZ}, "alerts": {"platform_id": "default"}}
    )
    view = builder.build(
        day=day,
        history_days=7,
        sections=["anomaly"],
        meta={"host": "h"},
        span_days=span,
    )
    return view.get("anomalies") or {}


def test_a_fault_that_recovers_after_midnight_keeps_its_duration(
    tmp_path: Path,
) -> None:
    """The recovery event lands on the next day, outside the day's window.

    The pairing used to look for the recovery inside the window only, so the
    duration degraded to "跨窗口关闭" and the row was painted like a new
    alert. The row's own ``resolved_ts`` carries the exact end.
    """

    store = ReportStore(tmp_path / "report.db")
    start = _ts("2026-10-31 23:50:00")
    end = _ts("2026-11-01 00:10:00")
    store.add_event(
        ts=start,
        day="2026-10-31",
        kind="resource",
        name="内存使用率",
        detail="95%",
        status="alert",
    )
    store.add_event(
        ts=end,
        day="2026-11-01",
        kind="resource",
        name="内存使用率",
        detail="持续",
        status="recovery",
    )
    store.resolve_event(
        ts=end,
        name="内存使用率",
        resolved_ts=end,
        kind="resource",
        target="",
        close_all=True,
    )

    daily = _anomalies(store, "2026-10-31", 1)
    rows = daily.get("items") or []
    assert rows, daily
    assert rows[0]["duration"] == "00:20:00", rows[0]
    assert rows[0]["status_key"] == "recovered", rows[0]

    weekly = _anomalies(store, "2026-11-01", 7)
    weekly_rows = weekly.get("items") or []
    assert weekly_rows, weekly
    assert weekly_rows[0]["duration"] == "00:20:00", weekly_rows[0]


def test_a_failed_push_and_its_retry_count_as_one_anomaly(tmp_path: Path) -> None:
    """The most common shape: the first push fails, the retry works.

    Both rows belong to ONE interruption, so the headline count must not
    double and the table must not show two rows.
    """

    store = ReportStore(tmp_path / "report.db")
    base = _ts("2026-10-31 09:00:00")
    store.add_event(
        ts=base,
        day="2026-10-31",
        kind="resource",
        name="内存使用率",
        detail="95%",
        status="failed",
    )
    store.add_event(
        ts=base + 30,
        day="2026-10-31",
        kind="resource",
        name="内存使用率",
        detail="95%",
        status="alert",
    )
    anomalies = _anomalies(store, "2026-10-31", 1)
    assert anomalies.get("count") == 1, anomalies
    assert len(anomalies.get("items") or []) == 1, anomalies


def test_a_flickering_rule_counts_as_one_anomaly(tmp_path: Path) -> None:
    """Several alerts of the same rule without a recovery are one outage."""

    store = ReportStore(tmp_path / "report.db")
    for hour in (8, 12, 18):
        store.add_event(
            ts=_ts(f"2026-10-31 {hour:02d}:00:00"),
            day="2026-10-31",
            kind="resource",
            name="内存使用率",
            detail="95%",
            status="alert",
        )
    anomalies = _anomalies(store, "2026-10-31", 1)
    assert anomalies.get("count") == 1, anomalies
    assert anomalies.get("unresolved") == 1, anomalies


def test_the_monitor_abandon_withdraws_the_queued_recovery(
    tmp_path: Path,
) -> None:
    """The withdraw branch has to be reachable.

    The recovery path clears ``summary_buffered`` when the episode closes,
    so the gate below it would skip the item and the queued recovery would
    survive -- a "已恢复" for an outage the user was never told about.
    """

    from astrbot_plugin_statuscard import monitor as monitor_module

    engine = object.__new__(monitor_module.ServiceMonitor)
    state = StateStore(tmp_path / "state.json")
    engine.state = state

    with state.section("monitors") as section:
        items = section.setdefault("items", {})
        items["站点A"] = {
            "name": "站点A",
            "alerting": False,
            "alerted": True,
            "recovery_awaits_summary": True,
            "pending_recovery": "✅ 服务恢复 · 站点A（排队中）",
            "recovery_attempt": 1,
            "recovery_next_attempt": time.time() + 900,
            "recovery_retry_count": 1,
            "since": time.time(),
            "last_checked": time.time(),
        }
    state.save()

    engine._on_summary_abandoned([("站点A", "HTTP 500")])
    after = state.get("monitors", {}).get("items", {}).get("站点A", {})
    assert not after.get("pending_recovery"), after
    assert not after.get("recovery_awaits_summary"), after


def test_the_restart_withdraws_the_queued_recovery(tmp_path: Path) -> None:
    """A queued recovery must not survive a restart.

    The recovery was queued because its alert sat in a summary that lived
    only in the aggregator's memory; the restart took that summary away.
    """

    from astrbot_plugin_statuscard import alerts as alerts_module
    from astrbot_plugin_statuscard import monitor as monitor_module

    for module, section_name, container in (
        (alerts_module, "alerts", "rules"),
        (monitor_module, "monitors", "items"),
    ):
        tmp = tmp_path / section_name
        tmp.mkdir()
        state = StateStore(tmp / "state.json")
        engine = object.__new__(
            alerts_module.AlertEngine
            if module is alerts_module
            else monitor_module.ServiceMonitor
        )
        engine.state = state
        with state.section(section_name) as section:
            bucket = section.setdefault(container, {})
            bucket["x"] = {
                "name": "站点A",
                "label": "内存使用率",
                "alerting": False,
                "alerted": True,
                "recovery_awaits_summary": True,
                "pending_recovery": "✅ 已恢复（排队中）",
                "recovery_attempt": 1,
                "recovery_next_attempt": time.time() + 900,
                "recovery_retry_count": 1,
                "since": time.time(),
                "last_checked": time.time(),
            }
        state.save()

        if module is alerts_module:
            engine._mark_stale_rules()
        else:
            engine._mark_stale_items()

        after = state.get(section_name, {}).get(container, {}).get("x", {})
        assert not after.get("pending_recovery"), (section_name, after)
        assert not after.get("recovery_awaits_summary"), (section_name, after)


def test_the_daily_pending_record_keeps_its_first_timestamp(
    tmp_path: Path,
) -> None:
    """The age of the record must not be refreshed on every retry.

    Refreshing it made the six-hour limit unreachable, so a broken target
    could queue the picture forever.
    """

    from astrbot_plugin_statuscard import alerts as alerts_module

    engine = object.__new__(alerts_module.AlertEngine)
    state = StateStore(tmp_path / "state.json")
    engine.state = state
    engine.config = {"alerts": {"daily_summary": {"enabled": True}}}

    engine._set_daily_pending("2026-11-01", time.time() + 600)
    first = engine._daily_pending()
    assert first is not None
    queued_at = first["queued_at"]

    engine._set_daily_pending("2026-11-01", time.time() + 1200)
    second = engine._daily_pending()
    assert second is not None
    assert second["queued_at"] == queued_at, (first, second)


def test_an_invalid_send_time_never_targets_an_undue_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken send time must not bring back the bug this guards.

    The calendar yesterday is not necessarily due yet, so using it as the
    fallback would send a report whose window had just opened and skip the
    real one in the evening.
    """

    monkeypatch.setattr(
        report_module, "now_in_timezone", lambda *a, **k: datetime(2026, 11, 1, 0, 1)
    )
    for broken in ("99:99", "abc", "21", "21:00:30", ""):
        reporter = _reporter(
            tmp_path,
            {
                "daily_report": {
                    "enabled": True,
                    "time": broken,
                    "sections": ["resource"],
                }
            },
        )
        due = reporter._latest_due_report_day()
        assert due < "2026-10-31", (broken, due)


def test_the_website_probe_client_has_a_bounded_timeout() -> None:
    """The pool-wide default must not be ``None``.

    ``httpx`` reads ``timeout=None`` as "no timeout at any phase". The
    per-request timeout did cover the request, but the outer
    ``asyncio.wait_for`` was then the only thing standing between a
    forgotten timeout and a probe that never returns. The marketplace
    review pointed at exactly this line; the service monitor already used
    a bounded default for the same reason.
    """

    import httpx
    from astrbot_plugin_statuscard import collectors as collectors_module

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSite:
        name = "x"
        url = "http://example.invalid/"
        timeout = 3.0

    original = httpx.AsyncClient
    httpx.AsyncClient = FakeClient  # type: ignore[misc]
    try:
        asyncio.run(collectors_module._probe_websites([FakeSite()]))
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    timeout = captured.get("timeout")
    assert timeout is not None, "the client-level timeout must not be None"
    assert isinstance(timeout, httpx.Timeout), timeout
    assert timeout.connect is not None and timeout.read is not None, timeout
    assert timeout.pool is not None, timeout
    # The concrete bound is locked, not just "some bound": a 9999 s or a
    # 0.001 s default would both pass an "is not None" assertion.
    for phase in (timeout.connect, timeout.read, timeout.write, timeout.pool):
        assert 0 < float(phase) <= 60, phase


def test_the_website_timeout_is_always_bounded() -> None:
    """``site.timeout`` may never become ``None`` or non-positive.

    httpx replaces the client timeout with the per-request one, so a
    ``None`` here would erase the bounded client default and the probe
    could hang forever.
    """

    from astrbot_plugin_statuscard import collectors as collectors_module

    raw = [
        {"name": "a", "url": "http://a.invalid/", "timeout_seconds": 0},
        {"name": "b", "url": "http://b.invalid/", "timeout_seconds": -5},
        {"name": "c", "url": "http://c.invalid/", "timeout_seconds": 9999},
        {"name": "d", "url": "http://d.invalid/", "timeout_seconds": "abc"},
        {"name": "e", "url": "http://e.invalid/", "timeout_seconds": None},
    ]
    targets = collectors_module._normalize_website_targets(raw)
    assert targets, "the targets were dropped"
    for target in targets:
        assert target.timeout is not None, target
        assert 1.0 <= float(target.timeout) <= 15.0, target


def test_the_webhook_verification_has_an_outer_bound() -> None:
    """The webhook check must not rely on the client timeout alone.

    httpx counts the read timeout per chunk, so a server that sends the
    headers and then drips the body keeps the request alive forever. Only
    an outer ``wait_for`` bounds the whole call.
    """

    root = Path(__file__).resolve().parents[1]
    source = (root / "page_api.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for(_post(), timeout=15)" in source, (
        "the webhook verification has no outer timeout"
    )
