"""Regression tests for the v2.13.0 reliability and polish batch (A + B).

Covers:
* A1 framework send timeout is not retried (no duplicate delivery)
* A2 only the newest matching alert is closed by a recovery
* A3 unresolved alerts from before the window stay visible in the report
* A4 resource alert gives up after three failed sends, one failed event
* A5 a corrupt report.db is renamed and rebuilt instead of silent data loss
* A6 a complete snapshot still accepts a late recv_total backfill
* A7 daily summary send markers persist across reloads
* B2 out-of-range config values are clamped with a warning
* B3 report image render failures retry twice before degrading to text
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta
from pathlib import Path

from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.delivery import DeliveryService
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import clamp_config

# ----------------------------------------------------------------------
# A1: a framework send timeout must not repeat the message
# ----------------------------------------------------------------------


class _TimeoutContext:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, target, chain) -> bool:
        self.calls += 1
        raise TimeoutError()


def test_fallback_send_timeout_not_retried(tmp_path: Path) -> None:
    context = _TimeoutContext()
    state = StateStore(tmp_path / "state.json")
    service = DeliveryService(
        context=context,
        state=state,
        get_client=lambda: None,
        build_chain=lambda payload: payload,
    )
    ok = asyncio.run(service._send_via_astrbot("default:GroupMessage:1", {"text": "x"}))
    assert ok is False
    assert context.calls == 1  # a timeout may have delivered; never resend


# ----------------------------------------------------------------------
# A2: one recovery closes only the newest open alert
# ----------------------------------------------------------------------


def test_resolve_event_closes_only_newest(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    base = 1_700_000_000
    # Two alerts of the same target; the first one's recovery was lost.
    store.add_event(base, "2026-10-19", "service", "Nginx", "down", "alert", "nginx")
    store.add_event(
        base + 100, "2026-10-19", "service", "Nginx", "down", "alert", "nginx"
    )
    assert (
        store.resolve_event(
            base + 200, "Nginx", resolved_ts=base + 200, kind="service", target="nginx"
        )
        is True
    )
    rows = store.list_events("2026-10-19", limit=10)
    resolved = {row["ts"]: row["resolved_ts"] for row in rows}
    assert resolved[base] is None
    assert resolved[base + 100] == base + 200


# ----------------------------------------------------------------------
# A3: an interruption older than the window stays visible
# ----------------------------------------------------------------------


def test_unresolved_alerts_visible_in_report(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    today = date(2026, 10, 26)
    yesterday = today - timedelta(days=1)
    old_ts = int(time.time()) - 5 * 86400
    store.add_event(
        old_ts,
        (today - timedelta(days=5)).isoformat(),
        "service",
        "旧故障",
        "still down",
        "alert",
        "老目标",
    )
    builder = DailyReportBuilder(store, {})
    section = builder._anomaly_section(yesterday, today - timedelta(days=2))
    assert section["unresolved"] == 1
    first = section["items"][0]
    assert first["status"] == "未恢复"
    assert first["name"] == "旧故障"

    # A recovery inside the window must hide the stale alert.
    store.add_event(
        int(time.time()) - 86400,
        yesterday.isoformat(),
        "service",
        "旧故障",
        "up",
        "recovery",
        "老目标",
    )
    section = builder._anomaly_section(yesterday, today - timedelta(days=2))
    assert section["unresolved"] == 0


# ----------------------------------------------------------------------
# A4: resource alerts stop retrying and log one failed event
# ----------------------------------------------------------------------


class _FailingPlugin:
    async def send_text(self, targets, text, source="unknown") -> bool:
        return False


def test_alert_gives_up_after_three_failures(tmp_path: Path) -> None:
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
    engine = AlertEngine(_FailingPlugin(), state, config, report_store=store)
    for _ in range(4):
        state.data.setdefault("alerts", {}).setdefault("rules", {})["cpu"] = (
            state.data.get("alerts", {}).get("rules", {}).get("cpu", {})
        )
        rule = state.data["alerts"]["rules"].get("cpu", {})
        rule.pop("next_attempt", None)  # allow the next attempt immediately
        asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["alerting"] is True  # treated as delivered-but-failed
    assert rule["retry_count"] == 0
    events = store.list_events(date.today().isoformat(), limit=50)
    failed = [e for e in events if e["status"] == "failed"]
    assert len(failed) == 1


# ----------------------------------------------------------------------
# A5: a corrupt database is rebuilt, the original is kept aside
# ----------------------------------------------------------------------


def test_report_db_recovers_from_corruption(tmp_path: Path) -> None:
    path = tmp_path / "report.db"
    path.write_bytes(b"this is not a sqlite database at all")
    store = ReportStore(path)
    assert store.available is True
    corrupt = list(tmp_path.glob("report.db.corrupt-*"))
    assert corrupt and corrupt[0].read_bytes().startswith(b"this is not")
    assert store.add_resource(1, cpu=1.0)


# ----------------------------------------------------------------------
# A6: a late recv_total still lands on a complete snapshot
# ----------------------------------------------------------------------


def test_snapshot_backfills_missing_recv_total(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.record_snapshot("2026-10-25", 22, 8, 2119, None, True, 1)
    assert store.snapshot_complete("2026-10-25") is True
    assert store.record_snapshot("2026-10-25", 22, 8, 2119, 50000, False, 2)
    snapshot = store.get_snapshot("2026-10-25")
    assert snapshot is not None
    assert snapshot["recv_total"] == 50000
    assert snapshot["complete"] == 1


# ----------------------------------------------------------------------
# A7: daily summary markers persist
# ----------------------------------------------------------------------


def test_daily_summary_markers_persist(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    engine = AlertEngine(None, state, {})
    engine._mark_daily_sent("2026-10-26")
    saved = state.get("alerts", {}).get("daily_summary", {})
    assert saved.get("last_sent_day") == "2026-10-26"
    assert saved.get("last_attempt_day") == "2026-10-26"

    reloaded = AlertEngine(None, state, {})
    assert reloaded._daily_last_sent_day == "2026-10-26"
    assert reloaded._daily_last_attempt_day == "2026-10-26"
    # Same day: no catch-up. Yesterday's marker: catch-up allowed.
    assert reloaded._daily_catch_up_allowed("2026-10-26") is False
    assert reloaded._daily_catch_up_allowed("2026-10-27") is True
    # A fresh install (no marker yet) must not double-send today's image.
    fresh = AlertEngine(None, StateStore(tmp_path / "fresh.json"), {})
    assert fresh._daily_catch_up_allowed("2026-10-26") is False


# ----------------------------------------------------------------------
# B2: clamp + warning
# ----------------------------------------------------------------------


def test_clamp_config_warns_out_of_range() -> None:
    assert clamp_config(3600, 15, 600, "demo") == 600
    assert clamp_config(5, 15, 600, "demo") == 15
    assert clamp_config(60, 15, 600, "demo") == 60


# ----------------------------------------------------------------------
# B3: image render retries before text fallback
# ----------------------------------------------------------------------


class _RetryPlugin:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.images: list[str] = []

    async def render_report_image(self, view):
        raise RuntimeError("T2I down")

    async def send_image(self, targets, image, source="unknown") -> bool:
        self.images.append(image)
        return True

    async def send_text(self, targets, text, source="unknown") -> bool:
        self.texts.append(text)
        return True


def test_daily_report_retries_image_twice_before_text(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _RetryPlugin()
    config = {
        "daily_report": {
            "enabled": True,
            "targets": ["123456"],
            "sections": ["resource"],
            "with_image": True,
        }
    }
    reporter = DailyReporter(plugin, store, config)
    reporter._build_view = lambda day=None: {
        "day": "2026-10-25",
        "sections": ["resource"],
    }

    asyncio.run(reporter._send_scheduled())
    assert plugin.texts == []  # suppressed while image retries remain
    assert reporter._image_retry_attempts == 1
    assert reporter._pending_image_retry is not None

    reporter._pending_image_retry = None
    asyncio.run(reporter._send_scheduled())
    assert plugin.texts == []
    assert reporter._image_retry_attempts == 2

    reporter._pending_image_retry = None
    asyncio.run(reporter._send_scheduled())
    assert plugin.texts  # third failure degrades to text
    assert reporter._image_retry_attempts == 0
