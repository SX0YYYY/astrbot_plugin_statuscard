"""v2.18.5：全面审计后的缺陷修复。

Every case here reproduces a defect found in the 2026-10-29 audit; each one
failed before the fix.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import collectors, page_api, report
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore


class _NullPlugin:
    async def send_text(self, targets, text, source="unknown") -> bool:
        return True

    async def send_image(self, targets, url, source="unknown"):
        return True


# ---------------------------------------------------------------------------
# disk percentage: one implementation for card, report, alert and dashboard
# ---------------------------------------------------------------------------


def test_every_surface_uses_the_same_disk_percent() -> None:
    """v2.18.4 only changed the card; the other three kept psutil's scale."""

    assert report.disk_usage is collectors.disk_usage
    assert alerts_module.disk_usage is collectors.disk_usage
    assert page_api.disk_usage is collectors.disk_usage


# ---------------------------------------------------------------------------
# aggregator: a concurrent first alert must not wipe the buffer
# ---------------------------------------------------------------------------


def test_aggregator_keeps_follow_ups_buffered_during_first_send() -> None:
    """Claiming the window after ``await send`` dropped every follow-up.

    Two concurrent alerts (monitor.check_all probes in parallel) both took the
    "first alert" branch; the later one reset ``pending`` and the buffered
    alerts were never sent, while their rules had already latched alerting.
    """

    async def run() -> list[str]:
        sent: list[str] = []
        agg = AlertAggregator(window_seconds=30, max_items=8)

        async def slow(text: str) -> bool:
            sent.append(text)
            await asyncio.sleep(0.05)
            return True

        async def fast(text: str) -> bool:
            sent.append(text)
            return True

        tasks = [
            asyncio.create_task(agg.push("A", "r", "text-a", slow, kind="service")),
            asyncio.create_task(agg.push("B", "r", "text-b", fast, kind="service")),
            asyncio.create_task(agg.push("C", "r", "text-c", fast, kind="service")),
        ]
        await asyncio.gather(*tasks)
        labels = [label for label, _reason in agg._buckets["service"].pending]
        assert labels == ["B", "C"]
        assert sent == ["text-a"]  # only the first alert is pushed on its own
        await agg.stop()
        return sent

    asyncio.run(run())


# ---------------------------------------------------------------------------
# daily snapshot: a retry must merge per column
# ---------------------------------------------------------------------------


def test_snapshot_retry_does_not_wipe_earlier_qq_counts(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.record_snapshot("2026-10-29", 21, 11, 3883, None, True, 1000)
    # The retry only recovered recv_total (OneBot counts failed this round).
    assert store.record_snapshot("2026-10-29", None, None, None, 123456, False, 2000)

    con = sqlite3.connect(tmp_path / "report.db")
    try:
        row = con.execute(
            "SELECT friends, groups, members, recv_total, complete"
            " FROM daily_snapshots WHERE day = '2026-10-29'"
        ).fetchone()
    finally:
        con.close()
    assert row == (21, 11, 3883, 123456, 1)


# ---------------------------------------------------------------------------
# website availability: 4xx/5xx is not "available"
# ---------------------------------------------------------------------------


def test_website_availability_requires_2xx_or_3xx() -> None:
    ok = report._website_ok
    assert ok("200") is True
    assert ok("302") is True
    assert ok("404") is False
    assert ok("500") is False
    assert ok("502") is False
    assert ok("ERR") is False


# ---------------------------------------------------------------------------
# alerts: a rule switched off must release its open interruption
# ---------------------------------------------------------------------------


def test_threshold_zero_records_a_recovery_event(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.data["alerts"] = {
        "rules": {
            "cpu": {
                "alerting": True,
                "alerted": True,
                "alert_since": time.time() - 60,
            }
        }
    }
    store = ReportStore(tmp_path / "report.db")
    config = {"alerts": {"enabled": True, "targets": ["1"], "sustain_samples": 1}}
    engine = AlertEngine(_NullPlugin(), state, config, report_store=store)

    asyncio.run(engine._evaluate("cpu", "CPU", 10.0, 0.0, "%", "10.0%"))

    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["alerting"] is False
    events = store.list_events_range(0, int(time.time()) + 60, limit=10)
    assert [event["status"] for event in events] == ["recovery"]
    assert "规则已停用" in events[0]["detail"]


# ---------------------------------------------------------------------------
# provider watch: a cooldown-suppressed episode stays silent on recovery
# ---------------------------------------------------------------------------


def test_provider_cooldown_suppresses_the_recovery_notice(
    tmp_path: Path, monkeypatch
) -> None:
    """The resource rules learned this in v2.18.1; provider did not."""

    sent: list[str] = []

    def fake_streak(_db, _limit):
        return 5, "error", True

    monkeypatch.setattr(alerts_module, "provider_recent_streak", fake_streak)

    state = StateStore(tmp_path / "state.json")
    # An alert was delivered 60s ago: the next episode is inside the cooldown.
    state.data["alerts"] = {
        "provider_watch": {
            "alerting": False,
            "alerted": True,
            "last_alert_ts": time.time() - 60,
            "last_streak": 4,
        }
    }
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["1"],
            "provider_watch": {
                "enabled": True,
                "threshold": 3,
                "cooldown_seconds": 1800,
            },
        }
    }

    engine = AlertEngine(_NullPlugin(), state, config, report_store=None)

    async def fake_send(text, source="unknown", kind="default"):
        sent.append(text)
        return True

    monkeypatch.setattr(engine, "_send", fake_send)
    monkeypatch.setattr(engine, "_delivered", lambda _result: True)
    asyncio.run(engine._check_provider_quality())

    watch = state.data["alerts"]["provider_watch"]
    assert watch["alerting"] is True
    assert watch["alerted"] is False
    assert sent == []  # suppressed: no alert

    # Now the service recovers: no recovery notice must be sent either.
    def fake_completed(_db, _limit):
        return 0, "completed", True

    monkeypatch.setattr(alerts_module, "provider_recent_streak", fake_completed)
    engine._provider_last_check = 0.0
    asyncio.run(engine._check_provider_quality())
    assert sent == []
    assert state.data["alerts"]["provider_watch"]["alerting"] is False


# ---------------------------------------------------------------------------
# weekly report: a missed window is caught up instead of skipping the week
# ---------------------------------------------------------------------------


def test_weekly_catch_up_requires_a_marker_and_the_trigger_day(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from astrbot_plugin_statuscard.report import DailyReporter

    tz = ZoneInfo("Asia/Shanghai")
    sunday = datetime(2026, 11, 1, 21, 40, tzinfo=tz)
    monday = datetime(2026, 11, 2, 10, 0, tzinfo=tz)
    assert sunday.weekday() == 6 and monday.weekday() == 0

    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}

    reporter._weekly_last_sent_week = None
    monkeypatch.setattr(report, "now_in_timezone", lambda _cfg: sunday)
    assert reporter._weekly_catch_up_allowed() is False  # first install

    reporter._weekly_last_sent_week = "2026-W43"
    monkeypatch.setattr(report, "now_in_timezone", lambda _cfg: monday)
    assert reporter._weekly_catch_up_allowed() is False  # not the trigger day

    monkeypatch.setattr(report, "now_in_timezone", lambda _cfg: sunday)
    assert reporter._weekly_catch_up_allowed() is True
