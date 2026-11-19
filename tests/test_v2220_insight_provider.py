"""v2.22.0: weekly insight block and provider x model quality detail."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard import provider_quality as pq
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import (
    DEFAULT_SECTIONS,
    DailyReportBuilder,
    build_report_text,
)

TZ = ZoneInfo("Asia/Shanghai")

_ROWS_COLUMNS = (
    "created_at, status, provider_model, conversation_id, "
    "start_time, end_time, time_to_first_token"
)


def _config() -> dict:
    return {
        "display": {"timezone": "Asia/Shanghai", "redact": []},
        "daily_report": {"sections": ["resource", "anomaly", "provider"]},
        "alerts": {"targets": []},
    }


def _utc_text(hours_ago: float = 1.0) -> str:
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )


def _make_db(tmp_path: Path, rows: list[tuple], provider_column: bool) -> Path:
    path = tmp_path / "data_v4.db"
    columns = _ROWS_COLUMNS + (", provider_id" if provider_column else "")
    placeholders = ", ".join("?" for _ in rows[0]) if rows else ""
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE provider_stats ({columns})")
    if rows:
        connection.executemany(
            f"INSERT INTO provider_stats VALUES ({placeholders})", rows
        )
    connection.commit()
    connection.close()
    return path


def _row7(status: str, model: str = "gpt-x", start: float = 100.0) -> tuple:
    return (_utc_text(), status, model, "conv-aaaa", start, start + 2.0, 0.5)


def _row8(status: str, provider: str, model: str, start: float = 100.0) -> tuple:
    return (
        _utc_text(),
        status,
        model,
        "conv-aaaa",
        start,
        start + 2.0,
        0.5,
        provider,
    )


def _view() -> dict:
    return {
        "window_label": "近 7 天",
        "resources": {
            "yesterday": {
                "cpu": {
                    "avg": 10.0,
                    "avg_text": "10.0%",
                    "delta_text": "+2.0%",
                    "peak_over": 0,
                },
                "ram": {
                    "avg": 60.0,
                    "avg_text": "60.0%",
                    "delta_text": "+5.0%",
                    "peak_over": 0,
                },
                "swap": {
                    "avg": 5.0,
                    "avg_text": "5.0%",
                    "delta_text": "-1.0%",
                    "peak_over": 0,
                },
                "disk": {
                    "avg": 70.0,
                    "avg_text": "70.0%",
                    "delta_text": "+3.0%",
                    "peak_over": 2,
                },
                "load1": {
                    "avg": 0.5,
                    "avg_text": "0.50",
                    "delta_text": "+0.1",
                    "peak_over": 0,
                },
            },
            "previous": {
                "cpu": {"avg": 8.0},
                "ram": {"avg": 55.0},
                "swap": {"avg": 6.0},
                "disk": {"avg": 67.0},
                "load1": {"avg": 0.4},
            },
        },
        "anomalies": {
            "by_name": [
                {
                    "name": "站点A",
                    "count": 5,
                    "unresolved": 1,
                    "kind_slug": "service",
                }
            ],
            "by_kind": [
                {
                    "kind": "服务",
                    "kind_slug": "service",
                    "count": 5,
                    "unresolved": 1,
                }
            ],
        },
        "provider_quality": {
            "provider_models": [
                {
                    "provider": "p1",
                    "model": "m1",
                    "name": "p1 / m1",
                    "calls_text": "100",
                    "success_rate_text": "90.0%",
                    "fail_rate": 10.0,
                    "fail_rate_text": "10.0%",
                }
            ]
        },
    }


# ----------------------------------------------------------------------
# insight
# ----------------------------------------------------------------------


def test_insight_builds_five_conclusions(tmp_path: Path) -> None:
    builder = DailyReportBuilder(ReportStore(tmp_path / "r.db"), _config())
    insight = builder._insight_section(_view(), span=7)
    assert insight["available"] is True
    texts = [line["text"] for line in insight["lines"]]
    assert any("磁盘较上期上升" in text for text in texts)
    assert any("增长最快：RAM" in text for text in texts)
    assert any("最不稳定：站点A" in text for text in texts)
    assert any("告警最多来自：服务" in text for text in texts)
    assert any("LLM 失败率最高：p1 / m1" in text for text in texts)
    assert len(texts) == 5


def test_insight_needs_source_sections(tmp_path: Path) -> None:
    builder = DailyReportBuilder(ReportStore(tmp_path / "r.db"), _config())
    insight = builder._insight_section({"window_label": "近 7 天"}, span=7)
    assert insight["available"] is False
    assert insight["lines"] == []
    assert "近 7 天" in insight["empty_reason"]


def test_insight_tolerates_dirty_data(tmp_path: Path) -> None:
    builder = DailyReportBuilder(ReportStore(tmp_path / "r.db"), _config())
    view = {
        "window_label": "近 7 天",
        "resources": {
            "yesterday": {"disk": {"avg": "broken", "avg_text": None}},
            "previous": {"disk": {"avg": None}},
        },
        "anomalies": {"by_name": "not-a-list", "by_kind": None},
        "provider_quality": {"provider_models": [{"fail_rate": "oops"}]},
    }
    insight = builder._insight_section(view, span=7)
    assert insight["available"] is False


def test_insight_placed_before_resource_in_text() -> None:
    view = {
        "title": "系统周报",
        "day": "2026-11-19",
        "sections": ["insight", "resource"],
        "window_label": "近 7 天",
        "day_label": "最后一天",
        "insights": {"lines": [{"text": "磁盘基本持平"}]},
        "resources": {
            "available": True,
            "yesterday": {
                "cpu": {"avg_text": "10.0%", "peak_text": "20.0%"},
                "ram": {"avg_text": "60.0%", "peak_text": "70.0%"},
                "swap": {"avg_text": "5.0%"},
                "disk": {"avg_text": "70.0%"},
                "load1": {"avg_text": "0.50"},
            },
        },
    }
    text = build_report_text(view)
    assert text.index("本期洞察") < text.index("资源(近 7 天)")


def test_insight_not_in_default_daily_sections() -> None:
    assert "insight" not in DEFAULT_SECTIONS


def test_build_dispatches_insight(tmp_path: Path) -> None:
    builder = DailyReportBuilder(ReportStore(tmp_path / "r.db"), _config())
    view = builder.build(sections=["resource", "insight"])
    assert "insights" in view
    # The daily build with the default list must not grow an insight block.
    assert "insights" not in builder.build(sections=["resource"])


# ----------------------------------------------------------------------
# provider x model
# ----------------------------------------------------------------------


def test_fetch_window_without_provider_column(tmp_path: Path) -> None:
    path = _make_db(tmp_path, [_row7("completed")], provider_column=False)
    connection = sqlite3.connect(path)
    try:
        rows, _, _ = pq.fetch_window(connection, "2000-01-01 00:00:00", "2100-01-01")
    finally:
        connection.close()
    assert len(rows[0]) == 7
    stats = pq.aggregate(rows)
    assert stats["by_provider_model"][0]["provider"] == "未知供应商"


def test_fetch_window_with_provider_column(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path, [_row8("completed", "deepseek", "v4")], provider_column=True
    )
    connection = sqlite3.connect(path)
    try:
        rows, _, _ = pq.fetch_window(connection, "2000-01-01 00:00:00", "2100-01-01")
    finally:
        connection.close()
    assert len(rows[0]) == 8
    stats = pq.aggregate(rows)
    detail = stats["by_provider_model"][0]
    assert detail["provider"] == "deepseek"
    assert detail["model"] == "v4"


def test_provider_model_percentiles_and_rates() -> None:
    rows = [
        _row8("completed", "p1", "m1", start=100.0),
        _row8("completed", "p1", "m1", start=200.0),
        _row8("error", "p1", "m1", start=300.0),
        _row8("aborted", "p1", "m1", start=400.0),
        _row8("completed", "p2", "m2", start=500.0),
    ]
    stats = pq.aggregate(rows)
    detail = next(
        item
        for item in stats["by_provider_model"]
        if item["provider"] == "p1" and item["model"] == "m1"
    )
    # aborted stays out of the denominator; the completed rows carry latency.
    assert detail["calls"] == 4
    assert detail["completed"] == 2
    assert detail["errors"] == 1
    assert detail["aborted"] == 1
    assert detail["success_rate"] == 66.66666666666666
    assert detail["fail_rate"] == 33.33333333333333
    assert detail["latency_n"] == 2
    assert detail["p50_latency"] is not None
    assert detail["p95_latency"] is not None


def test_provider_model_window_filter_keeps_trend() -> None:
    today = datetime.now(TZ).date().isoformat()
    rows = [_row8("completed", "p1", "m1")]
    stats = pq.aggregate(rows, window_keys={today}, tz=TZ)
    assert stats["total"] == 1
    assert len(stats["by_provider_model"]) == 1
    # A window without today keeps the trend but drops the headline figures.
    stats_out = pq.aggregate(rows, window_keys={"1999-01-01"}, tz=TZ)
    assert stats_out["total"] == 0
    assert stats_out["by_provider_model"] == []
    assert sum(bucket["calls"] for bucket in stats_out["by_day"].values()) == 1


def test_provider_quality_section_exposes_provider_models(tmp_path: Path) -> None:
    path = _make_db(
        tmp_path,
        [
            _row8("completed", "deepseek", "v4"),
            _row8("error", "deepseek", "v4"),
        ],
        provider_column=True,
    )
    builder = DailyReportBuilder(
        ReportStore(tmp_path / "report.db"), _config(), db_path=path
    )
    section = builder._provider_quality_section(datetime.now(TZ).date(), 7, 1)
    assert section["available"] is True
    model = section["provider_models"][0]
    assert model["provider"] == "deepseek"
    assert model["model"] == "v4"
    assert model["calls_text"] == "2"
    assert model["success_rate_text"] == "50.0%"
    assert model["p50_latency_text"] not in ("", None)
    # The legacy fields stay for the old dashboard/tests.
    assert section["top_models"]
