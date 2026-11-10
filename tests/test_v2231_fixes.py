"""Regression tests for the v2.20.31 daily-report batch.

1. The QQ section no longer charts friends/groups/members: those series are
   flat for months and made every image longer. The stat cards and the
   send/receive chart stay.
2. The LLM quality trend draws interruptions as a third line, and the shared
   note defines "failed" vs "interrupted" explicitly.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard.provider_quality import aggregate
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import (
    QUALITY_NOTE_BASE,
    DailyReportBuilder,
)
from astrbot_plugin_statuscard.t2i_renderer import build_report_html

TZ = ZoneInfo("Asia/Shanghai")
CONFIG = {"display": {"timezone": "Asia/Shanghai"}}

_ROWS_COLUMNS = (
    "created_at, status, provider_model, conversation_id, "
    "start_time, end_time, time_to_first_token"
)


def _builder(store: ReportStore, db_path: Path | None = None) -> DailyReportBuilder:
    return DailyReportBuilder(store, CONFIG, db_path=db_path)


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


def _utc_text(**offset: object) -> str:
    return (datetime.now(UTC) + timedelta(**offset)).strftime("%Y-%m-%d %H:%M:%S.%f")


# ---------------------------------------------------------------------------
# QQ section
# ---------------------------------------------------------------------------


def test_qq_section_only_charts_traffic(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.record_snapshot("2026-10-19", 12, 3, 120, 4900, True, 1)
    store.record_snapshot("2026-10-20", 12, 3, 120, 5000, True, 1)

    view = _builder(store).build(day="2026-10-20", sections=["qq"])
    qq = view["qq"]
    assert set(qq["charts"]) == {"traffic"}
    # The flat figures are still collected and shown as numbers.
    assert qq["yesterday"]["friends"] == 12
    assert qq["yesterday"]["groups"] == 3
    assert qq["yesterday"]["members"] == 120
    assert qq["charts"]["traffic"]["series"]


def test_report_html_drops_the_flat_charts(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.record_snapshot("2026-10-19", 12, 3, 120, 4900, True, 1)
    store.record_snapshot("2026-10-20", 12, 3, 120, 5000, True, 1)
    db = _make_db(
        tmp_path,
        [("2026-10-20 02:00:00.000000", "completed", "m", "c", 10, 20, 30)],
    )
    view = _builder(store, db).build(
        day="2026-10-20",
        sections=["qq", "provider"],
    )
    html = build_report_html(view, render_width=650)
    assert "好友 / 群聊" not in html
    assert "群员 <span>" not in html
    assert "发送 / 接收" in html
    assert "调用 / 失败 / 中断" in html


# ---------------------------------------------------------------------------
# LLM quality: interrupted trend + wording
# ---------------------------------------------------------------------------


def test_by_day_bucket_carries_aborted(tmp_path: Path) -> None:
    rows = [
        (_utc_text(hours=-1), "completed", "m", "c", 10, 20, 30),
        (_utc_text(hours=-1), "aborted", "m", "c", 10, 0, 0),
        (_utc_text(hours=-1), "error", "m", "c", 10, 20, 30),
    ]
    stats = aggregate(rows, None, TZ)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    bucket = stats["by_day"][today]
    assert bucket["calls"] == 3
    assert bucket["errors"] == 1
    assert bucket["aborted"] == 1


def test_quality_trend_draws_three_lines(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    db = _make_db(
        tmp_path,
        [
            (_utc_text(hours=-1), "completed", "m", "c", 10, 20, 30),
            (_utc_text(hours=-1), "aborted", "m", "c", 10, 0, 0),
            (_utc_text(hours=-1), "error", "m", "c", 10, 20, 30),
        ],
    )
    view = _builder(store, db).build(
        day=datetime.now(TZ).date().isoformat(), sections=["provider"]
    )
    trend = view["provider_quality"]["trend"]
    names = [item["name"] for item in trend["series"]]
    assert names == ["调用", "失败", "中断"]
    # ``_line_chart`` returns computed polylines, not the raw values; the
    # numbers themselves are asserted on ``by_day`` above.
    assert all(item.get("segments") for item in trend["series"])


def test_quality_note_defines_failed_and_interrupted() -> None:
    # The two terms must be defined side by side, not just listed.
    assert "失败 = 调用返回错误" in QUALITY_NOTE_BASE
    assert "中断 = 调用被取消" in QUALITY_NOTE_BASE
    assert "不计入失败" in QUALITY_NOTE_BASE
    assert "不打断连续失败" in QUALITY_NOTE_BASE
    assert "不一定是人为中止" in QUALITY_NOTE_BASE
    # The old attribution must not come back.
    assert "用户主动中止" not in QUALITY_NOTE_BASE
    assert "provider_stats" in QUALITY_NOTE_BASE


def test_quality_note_mentions_the_three_trend_lines() -> None:
    assert "调用 / 失败 / 中断" in QUALITY_NOTE_BASE
