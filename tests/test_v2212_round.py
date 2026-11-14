"""v2.21.2 audit-round regressions.

Locks the defects found by the two independent audits of v2.21.1:

* a scalar ``monitors.items`` crashed the whole plugin at construction;
* the alert loop counted one cached CPU sample several times when
  ``alerts.interval_seconds`` was shorter than ``display.sample_seconds``;
* the dashboard events list positioned recovery rows at their episode start,
  producing a backwards timeline;
* a still-unrecovered fault that outlived a reload was listed twice (once as
  "未恢复", once as "进行中");
* a single corrupt numeric/timestamp value aborted the whole report build;
* ``daily_report.collect=false`` skipped every retention prune;
* Top processes used the max of one same-name process instead of the
  same-sample total.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.report import ReportCollector
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import (
    DailyReportBuilder,
    _line_chart,
    _nice_ceil,
    _numeric,
)
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import safe_float

TZ = ZoneInfo("Asia/Shanghai")


def _ts(day: datetime, hour: int = 0, minute: int = 0) -> int:
    return int((day + timedelta(hours=hour, minutes=minute)).timestamp())


# ---------------------------------------------------------------------------
# P2: scalar monitors.items must not break plugin construction
# ---------------------------------------------------------------------------


def test_scalar_monitor_items_does_not_raise(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    monitor = ServiceMonitor(None, state, {"monitors": {"items": 1}})
    assert monitor.items == []
    assert monitor._configured_items == []


# ---------------------------------------------------------------------------
# P2: dashboard events timeline uses the moment each row describes
# ---------------------------------------------------------------------------


def _page_api(tmp_path: Path) -> PageApi:
    store = ReportStore(tmp_path / "report.db")
    plugin = SimpleNamespace(
        config={"display": {"timezone": "Asia/Shanghai", "page_redact": []}},
        report_store=store,
        context=None,
        state=SimpleNamespace(get=lambda *_a, **_k: {}),
        audit_issues=list,
        _audit_failed=False,
    )
    return PageApi(plugin)


def test_events_display_time_and_order(tmp_path: Path) -> None:
    api = _page_api(tmp_path)
    store = api.plugin.report_store
    now = int(time.time())
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    # The alert was pushed 10 minutes ago but its episode began 50 hours ago;
    # the recovery was pushed 20 minutes ago and began 40 hours ago. Push-time
    # order says alert, recovery; display-time order says recovery, alert.
    store.add_event(
        now - 600,
        day,
        "service",
        "X",
        "push-delayed alert",
        "alert",
        started_ts=now - 50 * 3600,
    )
    store.add_event(
        now - 1200,
        day,
        "service",
        "X",
        "recovery",
        "recovery",
        started_ts=now - 40 * 3600,
    )
    payload = asyncio.run(api.events())
    items = payload["items"]
    # The API keeps push-time order; the dashboard sorts by display_ts.
    by_status = {item["status"]: item for item in items}
    assert by_status["recovery"]["display_ts"] == by_status["recovery"]["ts"]
    assert by_status["alert"]["display_ts"] == by_status["alert"]["started_ts"]


def test_dashboard_js_uses_display_ts() -> None:
    js = (
        Path(__file__).resolve().parents[1] / "pages" / "status" / "app.js"
    ).read_text(encoding="utf-8")
    assert "item.display_ts || item.started_ts || item.ts" in js
    assert "const items = (data.items || []).slice().sort" in js


# ---------------------------------------------------------------------------
# P2: one unfinished episode is one row, not stale + in-window duplicates
# ---------------------------------------------------------------------------


def _alerts_builder(store: ReportStore) -> DailyReportBuilder:
    return DailyReportBuilder(
        store, {"display": {"timezone": "Asia/Shanghai", "redact": []}}
    )


def test_open_episode_is_not_duplicated_as_stale_row(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 11, 10, 0, 0, tzinfo=TZ)
    start = _ts(day, -1, 0)  # 11-09 23:00, before the report window
    store.add_event(
        start,
        "2026-11-09",
        "service",
        "New API",
        "down",
        "alert",
        target="http://a",
        started_ts=start,
    )
    # A reload/retry inside the window re-writes the same open episode, with
    # the same started_ts; the run-up latch row must not become a second row.
    store.add_event(
        _ts(day, 9),
        "2026-11-10",
        "service",
        "New API",
        "reload latch",
        "alert",
        target="http://a",
        started_ts=start,
    )
    view = _alerts_builder(store).build(day="2026-11-10", sections=["anomaly"])
    anomalies = view["anomalies"]
    assert anomalies["count"] == 1
    assert anomalies["unresolved"] == 1
    assert anomalies["total"] == 1
    assert len(anomalies["items"]) == 1


# ---------------------------------------------------------------------------
# P3: corrupt numeric/timestamp rows must not abort the report
# ---------------------------------------------------------------------------


def test_huge_numeric_values_are_ignored() -> None:
    huge = 10**400
    assert _numeric(huge) is None
    assert safe_float(huge) == 0.0
    chart = _line_chart(["a"], [{"name": "x", "values": [huge]}])
    assert chart["empty"] is True
    assert _nice_ceil(1e308) <= 1e308


def test_corrupt_resource_timestamp_does_not_abort(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_resource(ts=10**18, cpu=1, ram=1, swap=1, disk=1, load1=1)
    view = _alerts_builder(store).build(day="2026-11-10", sections=["resource"])
    assert view["resources"]["available"] is False


def test_corrupt_website_timestamp_does_not_abort(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_website_samples(
        10**18,
        [{"name": "x", "ok": True, "latency_ms": 1.0, "status": 200}],
    )
    view = _alerts_builder(store).build(day="2026-11-10", sections=["website"])
    assert view["website"]["available"] is False


def test_corrupt_event_timestamp_does_not_abort(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        10**18,
        "2026-11-10",
        "service",
        "X",
        "broken ts",
        "alert",
    )
    view = _alerts_builder(store).build(day="2026-11-10", sections=["anomaly"])
    assert view["anomalies"]["count"] == 0
    assert view["anomalies"]["items"] == []


# ---------------------------------------------------------------------------
# P3: retention prune survives daily_report.collect=false
# ---------------------------------------------------------------------------


def test_collect_disabled_still_starts_prune_loop(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    collector = ReportCollector(
        None, state, {"daily_report": {"collect": False}}, store
    )

    async def run() -> None:
        collector.start()
        assert collector._prune_task is not None
        assert collector._task is None
        assert collector._prune_running is True
        # The diagnostic must still report the operator's switch, not the
        # retention-only task, as "enabled" (v2.21.2 reverse audit).
        assert collector.status()["enabled"] is False

        # Same-instance switch back to collect=true: the prune task must not
        # hold the collection slot (v2.21.2 reverse audit).
        collector.config["daily_report"]["collect"] = True
        collector.start()
        assert collector._prune_task is None
        assert collector._task is not None
        assert collector.status()["enabled"] is True
        await collector.stop()

    asyncio.run(run())


def test_retention_shorter_than_history_is_disclosed(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    base = datetime(2026, 11, 10, 0, 0, tzinfo=TZ)
    for hour in (8, 9):
        store.add_resource(
            ts=_ts(base, hour),
            cpu=1.0,
            ram=1.0,
            swap=1.0,
            disk=1.0,
            load1=0.1,
        )
    builder = DailyReportBuilder(
        store,
        {
            "display": {"timezone": "Asia/Shanghai"},
            "daily_report": {"retention_days": 7},
        },
    )
    view = builder.build(day="2026-11-10", history_days=30, sections=["resource"])
    assert "受保留期限制" in view["resources"]["history_note"]


# ---------------------------------------------------------------------------
# P3: same-sample processes with one name are summed, not maxed
# ---------------------------------------------------------------------------


def test_process_section_sums_same_sample_processes(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    ts = int(datetime(2026, 11, 10, 12, 0, tzinfo=TZ).timestamp())
    rows = [
        {"rank": 1, "name": "python", "pid": 1, "cpu": 25.0, "mem_mb": 100.0},
        {"rank": 2, "name": "python", "pid": 2, "cpu": 25.0, "mem_mb": 100.0},
    ]
    store.add_process_samples(ts, "cpu", rows)
    store.add_process_samples(ts, "mem", rows)
    view = _alerts_builder(store).build(day="2026-11-10", sections=["process"])
    top = view["process"]["cpu"][0]
    cores = __import__("psutil").cpu_count(logical=True) or 1
    assert top["cpu_text"] == f"{50.0 / cores:.1f}%"
    assert view["process"]["mem"][0]["mem_text"] == "200 MB"


# ---------------------------------------------------------------------------
# P3: identical buffered alerts get one line with a repeat count
# ---------------------------------------------------------------------------


def test_aggregator_summary_groups_identical_entries() -> None:
    sent: list[str] = []

    async def sender(text: str):
        sent.append(text)
        return DeliveryResult(ok=True, ok_targets=["t"])

    agg = AlertAggregator(window_seconds=120, max_items=8)

    async def run() -> None:
        await agg.push("CPU", "95.0%", "first", sender, kind="k")
        await agg.push("CPU", "95.0%", "second", sender, kind="k")
        await agg.push("CPU", "95.0%", "third", sender, kind="k")
        await agg.flush("k", sender)

    asyncio.run(run())
    assert sent, "the summary should be delivered"
    summary = sent[-1]
    assert "2 项异常" in summary
    assert summary.count("• CPU: 95.0%") == 1
    assert "（2 次）" in summary
