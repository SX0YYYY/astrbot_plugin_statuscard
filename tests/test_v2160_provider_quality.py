"""v2.16.0 provider quality statistics: aggregation, streaks, alert wiring.

Every datetime in these tests is built with an explicit ``ZoneInfo`` - the
CI host runs in UTC, and naive timestamps have already burned two CI runs
(v2.14.0 / v2.15.0).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import provider_quality as pq
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import (
    DailyReportBuilder,
    build_report_text,
)
from astrbot_plugin_statuscard.state import StateStore

TZ = ZoneInfo("Asia/Shanghai")

_ROWS_COLUMNS = (
    "created_at, status, provider_model, conversation_id, "
    "start_time, end_time, time_to_first_token"
)


def _utc_text(**offset) -> str:
    return (datetime.now(UTC) + timedelta(**offset)).strftime("%Y-%m-%d %H:%M:%S.%f")


def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
    path = tmp_path / "data_v4.db"
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE provider_stats ({_ROWS_COLUMNS})")
    connection.executemany(
        "INSERT INTO provider_stats VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()
    return path


# ----------------------------------------------------------------------
# error_streak
# ----------------------------------------------------------------------


def test_error_streak_counts_consecutive_errors() -> None:
    recent = [("t", "error"), ("t", "error"), ("t", "completed")]
    assert pq.error_streak(recent) == 2


def test_error_streak_skips_aborted_without_breaking() -> None:
    recent = [
        ("t", "error"),
        ("t", "aborted"),
        ("t", "error"),
        ("t", "aborted"),
        ("t", "completed"),
    ]
    # The two errors on either side of the abort form one streak of two.
    assert pq.error_streak(recent) == 2


def test_error_streak_stops_on_completed_and_unknown() -> None:
    assert pq.error_streak([("t", "completed")]) == 0
    assert pq.error_streak([("t", "error"), ("t", "weird"), ("t", "error")]) == 1


def test_error_streak_empty_and_all_errors() -> None:
    assert pq.error_streak([]) == 0
    assert pq.error_streak([("t", "error")] * 7) == 7


# ----------------------------------------------------------------------
# aggregate
# ----------------------------------------------------------------------


def _row(
    created: str,
    status: str,
    model: str = "gpt-x",
    conv: str = "aaaaaaaa-1111",
    start: float = 100.0,
    end: float = 102.5,
    ttft: float = 0.8,
) -> tuple:
    return (created, status, model, conv, start, end, ttft)


def test_aggregate_success_rate_excludes_aborted() -> None:
    rows = [
        _row(_utc_text(), "completed"),
        _row(_utc_text(), "completed"),
        _row(_utc_text(), "error"),
        _row(_utc_text(), "aborted"),
        _row(_utc_text(), "aborted"),
    ]
    stats = pq.aggregate(rows, None, TZ)
    assert stats["total"] == 5
    assert stats["completed"] == 2
    assert stats["errors"] == 1
    assert stats["aborted"] == 2
    # aborted rows stay out of the denominator: 2 / (2 + 1).
    assert stats["success_rate"] == pytest.approx(100.0 * 2 / 3)


def test_aggregate_latency_completed_only_and_ttft_zero_excluded() -> None:
    rows = [
        _row(_utc_text(), "completed", start=10.0, end=13.0, ttft=1.0),
        _row(_utc_text(), "completed", start=10.0, end=12.0, ttft=0.0),
        _row(_utc_text(), "error", start=10.0, end=99.0, ttft=5.0),
        _row(_utc_text(), "completed", start=0.0, end=0.0, ttft=0.0),
    ]
    stats = pq.aggregate(rows, None, TZ)
    # Only the two completed rows with usable timings: (3.0 + 2.0) / 2.
    assert stats["avg_latency"] == pytest.approx(2.5)
    assert stats["p95_latency"] == pytest.approx(3.0)
    assert stats["avg_ttft"] == pytest.approx(1.0)
    assert stats["p95_ttft"] == pytest.approx(1.0)


def test_aggregate_window_filter_keeps_trend_complete() -> None:
    yesterday = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    rows = [
        _row(_utc_text(), "completed", model="m1"),
        _row(_utc_text(days=-1), "error", model="m2"),
    ]
    stats = pq.aggregate(rows, {today}, TZ)
    # Headline figures only cover the local window (today: 1 call); the
    # per-day trend keeps both days so the chart can show history.
    assert stats["total"] == 1
    assert stats["errors"] == 0
    assert stats["by_model"][0]["name"] == "m1"
    assert set(stats["by_day"]) == {today, yesterday}
    assert stats["by_day"][yesterday]["errors"] == 1


def test_aggregate_by_conversation_truncates_uuid() -> None:
    rows = [_row(_utc_text(), "completed", conv="12345678-abcd-ef00")]
    stats = pq.aggregate(rows, None, TZ)
    assert stats["by_conversation"][0]["conversation"] == "12345678"
    empty = pq.aggregate([_row(_utc_text(), "completed", conv="")], None, TZ)
    assert empty["by_conversation"][0]["conversation"] == "未知会话"


def test_parse_utc_value_variants() -> None:
    parsed = pq.parse_utc_value("2026-10-28 12:00:00")
    assert parsed is not None and parsed.utcoffset() == timedelta(0)
    assert pq.parse_utc_value(None) is None
    assert pq.parse_utc_value("") is None
    assert pq.parse_utc_value("not a date") is None


# ----------------------------------------------------------------------
# sqlite access
# ----------------------------------------------------------------------


def test_fetch_window_and_recent(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path,
        [
            (_utc_text(), "completed", "m", "c1", 1.0, 2.0, 0.5),
            (_utc_text(hours=-3), "error", "m", "c1", 0, 0, 0),
        ],
    )
    connection = pq.connect_readonly(path)
    assert connection is not None
    try:
        rows, min_created, truncated = pq.fetch_window(
            connection,
            "2000-01-01 00:00:00",
            "2999-01-01 00:00:00",
        )
        assert len(rows) == 2
        assert min_created is not None
        assert truncated is False
        recent = pq.fetch_recent(connection, 5)
        assert [status for _created, status in recent] == ["completed", "error"]
    finally:
        connection.close()


def test_recent_streak_missing_database(tmp_path: Path) -> None:
    assert pq.recent_streak(tmp_path / "nope.db") == (0, "", 0)


def test_recent_streak_reads_newest_rows(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path,
        [
            (_utc_text(minutes=-1), "error", "m", "c", 0, 0, 0),
            (_utc_text(minutes=-2), "error", "m", "c", 0, 0, 0),
            (_utc_text(minutes=-3), "aborted", "m", "c", 0, 0, 0),
            (_utc_text(minutes=-4), "completed", "m", "c", 0, 0, 0),
        ],
    )
    streak, latest, sampled = pq.recent_streak(path)
    assert (streak, latest, sampled) == (2, "error", 4)


def test_today_overview_counts_today_only(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path,
        [
            (_utc_text(), "completed", "m", "c1", 1.0, 2.0, 0.5),
            (_utc_text(), "error", "m", "c1", 0, 0, 0),
            (_utc_text(days=2), "error", "m", "c1", 0, 0, 0),
        ],
    )
    config = {"display": {"timezone": "Asia/Shanghai"}}
    overview = pq.today_overview(config, path)
    assert overview["available"] is True
    assert overview["calls"] == 2
    assert overview["completed"] == 1
    assert overview["errors"] == 1
    assert overview["success_text"] == "50.0%"

    sub = tmp_path / "sub"
    sub.mkdir()
    empty_db = _make_db(sub, [])
    empty = pq.today_overview(config, empty_db)
    assert empty["available"] is False
    assert empty["empty_reason"]


# ----------------------------------------------------------------------
# report section
# ----------------------------------------------------------------------


def _builder(tmp_path: Path) -> DailyReportBuilder:
    store = ReportStore(tmp_path / "report.db")
    config = {"display": {"timezone": "Asia/Shanghai"}}
    return DailyReportBuilder(store, config, db_path=tmp_path / "data_v4.db")


def test_provider_quality_section_without_database(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    section = builder._provider_quality_section(datetime.now(TZ).date(), 7, 1)
    assert section["available"] is False
    assert section["empty_reason"]


def test_provider_quality_section_with_data(tmp_path: Path) -> None:
    _make_db(
        tmp_path,
        [
            (_utc_text(), "completed", "m-a", "11111111-x", 10.0, 12.0, 0.5),
            (_utc_text(), "completed", "m-a", "11111111-x", 10.0, 11.0, 0.4),
            (_utc_text(), "error", "m-a", "11111111-x", 0, 0, 0),
            (_utc_text(), "aborted", "m-b", "22222222-x", 0, 0, 0),
        ],
    )
    builder = _builder(tmp_path)
    section = builder._provider_quality_section(datetime.now(TZ).date(), 7, 1)
    assert section["available"] is True
    stats = section["stats"]
    assert stats["calls_text"] == "4"
    assert stats["success_rate_text"] == "66.7%"
    assert stats["aborted_text"] == "1"
    assert stats["avg_latency_text"] == "1.50s"
    assert stats["avg_ttft_text"] == "0.45s"
    # m-a has 3 calls, m-b has 1: an unequal order keeps the top-model
    # table deterministic even when SQLite returns equal created_at rows
    # in an arbitrary order.
    names = [model["name"] for model in section["top_models"]]
    assert names == ["m-a", "m-b"]
    convs = [conv["conversation"] for conv in section["top_conversations"]]
    assert convs[0] == "11111111"
    assert section["trend"] is not None
    assert "provider_stats" in section["note"]


def test_build_dispatches_provider_section(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    today = datetime.now(TZ).date()
    view = builder.build(day=today, history_days=7, sections=["provider", "bogus"])
    assert view["sections"] == ["provider"]
    assert "provider_quality" in view
    assert view["provider_quality"]["available"] is False
    # Default sections stay unchanged for existing installs.
    default_view = builder.build(day=today, history_days=7)
    assert "provider_quality" not in default_view


def test_report_text_includes_quality(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    today = datetime.now(TZ).date()
    view = builder.build(day=today, history_days=7, sections=["provider"])
    text = build_report_text(view)
    assert "LLM 质量" in text
    # No database in the sandbox: the section degrades to its reason line.
    assert "未找到" in text


# ----------------------------------------------------------------------
# alert state machine
# ----------------------------------------------------------------------


class _OkPlugin:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str, str]] = []

    async def send_text(self, targets, text, source="unknown") -> bool:
        self.sent.append((targets, text, source))
        return True


def _engine(tmp_path: Path, plugin: _OkPlugin) -> AlertEngine:
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "provider_watch": {
                "enabled": True,
                "threshold": 3,
                "cooldown_seconds": 300,
            },
        }
    }
    return AlertEngine(
        plugin,
        StateStore(tmp_path / "state.json"),
        config,
        report_store=ReportStore(tmp_path / "report.db"),
    )


def _watch_state(engine: AlertEngine) -> dict:
    return (engine.state.get("alerts", {}) or {}).get("provider_watch", {})


def test_provider_watch_alerts_and_recovers(tmp_path, monkeypatch) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    fake = lambda db_path, limit: (3, "error", 20)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", fake)

    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True
    assert len(plugin.sent) == 1
    assert "连续失败" in plugin.sent[0][1]
    assert plugin.sent[0][2] == "provider"

    fake = lambda db_path, limit: (0, "completed", 20)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", fake)
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is False
    assert len(plugin.sent) == 2
    assert "已恢复" in plugin.sent[1][1]


def test_provider_watch_cooldown_gates_realert(tmp_path, monkeypatch) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    failing = lambda db_path, limit: (5, "error", 20)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", failing)
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True

    recovered = lambda db_path, limit: (0, "completed", 20)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", recovered)
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is False

    # Failing again right after the recovery stays quiet push-wise (the
    # cooldown has not elapsed since the last alert), but the rule still
    # latches and records the incident.
    monkeypatch.setattr(alerts_module, "provider_recent_streak", failing)
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True
    assert len(plugin.sent) == 2


def test_provider_watch_aborted_does_not_resolve(tmp_path, monkeypatch) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (2, "aborted", 20),
    )
    alerts_state = engine.state.setdefault("alerts", {})
    alerts_state.setdefault("provider_watch", {})["alerting"] = True
    engine.state.save()

    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True
    assert plugin.sent == []


def test_provider_watch_no_data_keeps_state(tmp_path, monkeypatch) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module, "provider_recent_streak", lambda db_path, limit: (0, "", 0)
    )
    asyncio.run(engine._check_provider_quality())
    assert plugin.sent == []
    assert "alerting" not in _watch_state(engine)


def test_provider_watch_below_threshold_stays_quiet(tmp_path, monkeypatch) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module, "provider_recent_streak", lambda db_path, limit: (2, "error", 20)
    )
    asyncio.run(engine._check_provider_quality())
    assert plugin.sent == []
    assert _watch_state(engine).get("alerting") in (False, None)


# ----------------------------------------------------------------------
# schema / audit
# ----------------------------------------------------------------------


def _schema() -> dict:
    path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_schema_provider_options() -> None:
    schema = _schema()
    daily_items = schema["daily_report"]["items"]
    assert "provider" in daily_items["sections"]["options"]
    assert "provider" in daily_items["weekly_sections"]["options"]
    for key in ("sections", "weekly_sections"):
        labels = daily_items[key]["labels"]
        options = daily_items[key]["options"]
        assert len(labels) == len(options)
    notify_rules = schema["notify"]["items"]["rules"]["templates"]["rule"]["items"]
    assert "provider" in notify_rules["kinds"]["options"]
    watch = schema["alerts"]["items"]["provider_watch"]["items"]
    assert watch["enabled"]["default"] is False
    assert watch["threshold"]["default"] == 3
    assert watch["cooldown_seconds"]["default"] == 1800


def test_audit_provider_watch_without_targets() -> None:
    config = {
        "alerts": {
            "enabled": False,
            "targets": [],
            "provider_watch": {"enabled": True, "threshold": 3},
        }
    }
    codes = [issue.code for issue in run_audit(config)]
    assert "provider_watch_no_targets" in codes

    config["alerts"]["targets"] = ["123456"]
    codes = [issue.code for issue in run_audit(config)]
    assert "provider_watch_no_targets" not in codes

    # A matching notification-center rule replaces the legacy target list.
    config["alerts"]["targets"] = []
    config["notify"] = {
        "rules": [
            {
                "enabled": True,
                "target": "654321",
                "kinds": ["provider"],
            }
        ]
    }
    codes = [issue.code for issue in run_audit(config)]
    assert "provider_watch_no_targets" not in codes


def test_overview_streak_uses_newest_first_order(tmp_path: Path) -> None:
    # newest-first input is the caller contract; a chronological list must
    # not fake a streak.
    path = _make_db(
        tmp_path,
        [
            (_utc_text(minutes=-1), "completed", "m", "c", 0, 0, 0),
            (_utc_text(minutes=-2), "error", "m", "c", 0, 0, 0),
        ],
    )
    assert pq.recent_streak(path)[0] == 0


# ----------------------------------------------------------------------
# v2.16.1 找茬修复回归
# ----------------------------------------------------------------------


def test_fail_rate_denominator_excludes_aborted() -> None:
    rows = [
        _row(_utc_text(), "completed", model="m-a"),
        _row(_utc_text(), "error", model="m-a"),
        _row(_utc_text(), "aborted", model="m-a"),
    ]
    stats = pq.aggregate(rows, None, TZ)
    # 1 error / (1 completed + 1 error) = 50%, not 1/3.
    assert stats["by_model"][0]["fail_rate"] == pytest.approx(50.0)
    # A model with only aborts has no measurable failure rate.
    stats2 = pq.aggregate([_row(_utc_text(), "aborted", model="m-b")], None, TZ)
    assert stats2["by_model"][0]["fail_rate"] is None


def test_fetch_window_oldest_kept_marks_truncated_gap(tmp_path: Path) -> None:
    rows = [(_utc_text(minutes=-i), "completed", "m", "c", 0, 0, 0) for i in range(7)]
    path = _make_db(tmp_path, rows)
    connection = pq.connect_readonly(path)
    assert connection is not None
    try:
        fetched, _oldest_kept, truncated = pq.fetch_window(
            connection, "2000-01-01 00:00:00", "2999-01-01 00:00:00", limit=5
        )
    finally:
        connection.close()
    assert truncated is True
    assert len(fetched) == 5
    # Exactly-at-cap is NOT truncation.
    conn2 = pq.connect_readonly(path)
    try:
        fetched2, _oldest, truncated2 = pq.fetch_window(
            conn2, "2000-01-01 00:00:00", "2999-01-01 00:00:00", limit=7
        )
    finally:
        conn2.close()
    assert truncated2 is False
    assert fetched2[0][0] == rows[0][0]


def test_today_overview_streak_uses_full_table(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path,
        [
            (_utc_text(minutes=-1), "error", "m", "c", 0, 0, 0),
            (_utc_text(minutes=-90), "error", "m", "c", 0, 0, 0),
            (_utc_text(minutes=-95), "error", "m", "c", 0, 0, 0),
        ],
    )
    config = {"display": {"timezone": "Asia/Shanghai"}}
    # Three newest errors regardless of local day: the card must agree
    # with the alert engine when the streak crosses midnight.
    assert pq.today_overview(config, path)["error_streak"] == 3


def test_today_overview_reason_codes(tmp_path: Path) -> None:
    config = {"display": {"timezone": "Asia/Shanghai"}}
    empty_db = _make_db(tmp_path, [])
    empty = pq.today_overview(config, empty_db)
    assert empty["reason_code"] == "no_data_today"
    missing = pq.today_overview(config, tmp_path / "nope.db")
    assert missing["reason_code"] == "no_db"


def test_provider_watch_cooldown_records_event(tmp_path, monkeypatch) -> None:

    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    store = engine.report_store
    failing = lambda db_path, limit: (5, "error", 30)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", failing)
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True

    recovered = lambda db_path, limit: (0, "completed", 30)
    monkeypatch.setattr(alerts_module, "provider_recent_streak", recovered)
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is False

    # A second threshold hit inside the cooldown must still latch and
    # record an event - only the duplicate push is suppressed.
    monkeypatch.setattr(alerts_module, "provider_recent_streak", failing)
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True
    events = store.list_events_range(0, int(time.time()) + 60, limit=50)
    cooldown_alerts = [
        e for e in events if e["kind"] == "provider" and "冷却" in (e["detail"] or "")
    ]
    assert len(cooldown_alerts) == 1


def test_provider_watch_recovery_pending_retry(tmp_path, monkeypatch) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module, "provider_recent_streak", lambda db_path, limit: (3, "error", 30)
    )
    asyncio.run(engine._check_provider_quality())
    assert _watch_state(engine).get("alerting") is True

    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (0, "completed", 30),
    )
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    watch = _watch_state(engine)
    assert watch.get("alerting") is False
    # First recovery send succeeded (plugin returns True), so nothing pends.
    assert "pending_recovery" not in watch
    assert len(plugin.sent) == 2
    assert "此前连续失败: 3 次" in plugin.sent[1][1]

    # A failed recovery send queues the notice for retry.
    class _BrokenPlugin:
        async def send_text(self, targets, text, source="unknown") -> bool:
            return False

    (tmp_path / "sub2").mkdir(exist_ok=True)
    engine2 = _engine(tmp_path / "sub2", _OkPlugin())
    engine2.plugin = _BrokenPlugin()
    monkeypatch.setattr(
        alerts_module, "provider_recent_streak", lambda db_path, limit: (3, "error", 30)
    )
    asyncio.run(engine2._check_provider_quality())
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (0, "completed", 30),
    )
    engine2._provider_last_check = 0
    asyncio.run(engine2._check_provider_quality())
    watch2 = _watch_state(engine2)
    assert watch2.get("pending_recovery")
    assert watch2.get("recovery_retry_count") == 1
    # The next check (after the retry delay) retries the queued notice.
    engine2.plugin = _OkPlugin()
    watch2["recovery_next_attempt"] = 0
    engine2.state.save()
    engine2._provider_last_check = 0
    asyncio.run(engine2._check_provider_quality())
    assert "pending_recovery" not in _watch_state(engine2)
    assert len(engine2.plugin.sent) == 1
    assert "已恢复" in engine2.plugin.sent[0][1]


def test_provider_watch_threshold_zero_disables(tmp_path) -> None:
    from astrbot_plugin_statuscard.report_store import ReportStore
    from astrbot_plugin_statuscard.state import StateStore

    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "provider_watch": {"enabled": True, "threshold": 0},
        }
    }
    engine = AlertEngine(
        _OkPlugin(),
        StateStore(tmp_path / "s.json"),
        config,
        report_store=ReportStore(tmp_path / "r.db"),
    )
    assert engine.provider_watch_threshold == 0
    asyncio.run(engine._check_provider_quality())
    assert plugin_sent_empty(engine)


def plugin_sent_empty(engine) -> bool:
    return not engine.plugin.sent
