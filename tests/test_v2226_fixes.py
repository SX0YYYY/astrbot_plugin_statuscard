"""Regression tests for the v2.20.26 aborted-attribution fix.

The 2026-11-08 online review proved AstrBot's ``aborted`` status is a
cancellation, not necessarily a user stop: same-session takeovers, other
plugins stopping the event, and upstream timeouts cancelled mid-retry all
land there (the framework log line "requested to stop by user" is
misleading). This batch rewrites the plugin's attribution and adds a
separate counter for long-hanging aborts (>= 60 s, mostly upstream
timeouts). The success-rate denominator and the alert streak behaviour
stay untouched.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import astrbot_plugin_statuscard.provider_quality as pq
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder

TZ = ZoneInfo("Asia/Shanghai")

_CONFIG = {"display": {"timezone": "Asia/Shanghai"}}

_COLUMNS = (
    "created_at, status, provider_model, conversation_id, "
    "start_time, end_time, time_to_first_token"
)


def _today_noon_utc_text() -> str:
    """Noon of today in the configured timezone, as a UTC ``created_at`` cell.

    Anchoring to local noon keeps the row inside today's window no matter
    when the test runs (CI is UTC, business is Asia/Shanghai) - the same
    lesson as the v2.20.21/v2.20.25 CI reds.
    """

    noon = datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)
    return noon.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _row(status: str, latency: float) -> tuple:
    """One ``provider_stats`` row whose measured duration is ``latency``.

    The anchor must stay far above the largest tested latency: the
    aggregate treats ``start <= 0`` as a corrupted timestamp and zeroes
    the duration.
    """

    end = 1_800_000_000.0
    start = end - latency
    return (_today_noon_utc_text(), status, "m", "conv-12345678", start, end, 0.1)


def _make_db(tmp_path: Path, rows: list[tuple]) -> Path:
    path = tmp_path / "data_v4.db"
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE provider_stats ({_COLUMNS})")
    connection.executemany(
        "INSERT INTO provider_stats VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()
    return path


def _quality_section(tmp_path: Path, rows: list[tuple]) -> dict:
    db_path = _make_db(tmp_path, rows)
    builder = DailyReportBuilder(
        ReportStore(tmp_path / "report.db"), _CONFIG, db_path=db_path
    )
    return builder._provider_quality_section(
        datetime.now(TZ).date(), history_days=1, span=1
    )


# ----------------------------------------------------------------------
# aggregate: aborted_long
# ----------------------------------------------------------------------


def test_aggregate_counts_long_aborts_separately() -> None:
    rows = [
        _row("aborted", 2160.0),  # 36-minute hang (the 11-07 kimi shape) -> long
        _row("aborted", 60.0),  # exactly at the threshold -> long
        _row("aborted", 59.9),  # just under -> short
        _row("aborted", 1.0),  # mechanical takeover -> short
        _row("aborted", 0.0),  # unusable timestamps -> latency 0 -> short
        _row("completed", 120.0),
        _row("error", 30.0),
    ]
    stats = pq.aggregate(rows, None, TZ)
    assert stats["aborted"] == 5
    assert stats["aborted_long"] == 2
    assert stats["completed"] == 1
    assert stats["errors"] == 1
    # The success-rate denominator still excludes aborts entirely:
    # 1 completed out of (1 completed + 1 error) = 50%, aborts invisible.
    assert stats["success_rate"] == 50.0


def test_aggregate_aborted_long_stays_zero_without_aborts() -> None:
    stats = pq.aggregate([_row("completed", 10.0)], None, TZ)
    assert stats["aborted"] == 0
    assert stats["aborted_long"] == 0


# ----------------------------------------------------------------------
# today_overview: aborted_long passthrough
# ----------------------------------------------------------------------


def test_today_overview_reports_aborted_long(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path,
        [
            _row("aborted", 2160.0),
            _row("aborted", 2.0),
            _row("completed", 10.0),
        ],
    )
    overview = pq.today_overview(_CONFIG, path)
    assert overview["available"] is True
    assert overview["aborted"] == 2
    assert overview["aborted_long"] == 1


def test_today_overview_default_includes_aborted_long(tmp_path: Path) -> None:
    overview = pq.today_overview(_CONFIG, tmp_path / "missing.db")
    assert overview["available"] is False
    assert overview["aborted_long"] == 0


# ----------------------------------------------------------------------
# report section: attribution wording and the >= 60 s hint
# ----------------------------------------------------------------------


def test_quality_section_note_lists_cancellation_sources(tmp_path: Path) -> None:
    view = _quality_section(tmp_path, [_row("aborted", 2.0), _row("completed", 10.0)])
    assert view["available"] is True
    assert view["stats"]["aborted_text"] == "1"
    assert view["stats"]["aborted_long_text"] == "0"
    note = view["note"]
    assert "中断" in note
    assert "不一定是人为中止" in note
    assert "用户中止" not in note
    assert "用户主动中止" not in note
    # No long abort -> the extra sentence must stay out.
    assert "≥60 秒" not in note


def test_quality_section_flags_long_aborts(tmp_path: Path) -> None:
    view = _quality_section(tmp_path, [_row("aborted", 2160.0)])
    assert view["available"] is True
    assert view["stats"]["aborted_long_text"] == "1"
    assert "≥60 秒" in view["note"]
    assert "中断 1 次" in view["note"]


def test_quality_section_wording_survives_empty_window(tmp_path: Path) -> None:
    # A window with rows but zero aborts must render the neutral wording
    # too (the note is shared by the token section's denominator text).
    view = _quality_section(tmp_path, [_row("completed", 10.0)])
    assert view["available"] is True
    assert view["stats"]["aborted_text"] == "0"
    assert "不一定是人为中止" in view["note"]
