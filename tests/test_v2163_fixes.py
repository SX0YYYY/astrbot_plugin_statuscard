"""v2.16.3 日报审读修复：孤儿未恢复告警与 failed/recovery 配对。

All timestamps are built with an explicit ``ZoneInfo`` - the CI host runs
in UTC, and the report windows are display-timezone days.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder

TZ = ZoneInfo("Asia/Shanghai")


def _ts(day: int, hour: int, minute: int, second: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, second, tzinfo=TZ).timestamp())


def test_unresolved_alerts_skip_legacy_rows_with_later_recovery(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    # A pre-v2.12 alert (resolved_ts never back-filled) that does have a
    # later recovery event in the log.
    store.add_event(
        _ts(20, 8, 52), "2026-10-20", "service", "New API 容器", "容器停止", "alert"
    )
    store.add_event(
        _ts(20, 8, 59),
        "2026-10-20",
        "service",
        "New API 容器",
        "容器 running=True",
        "recovery",
    )
    # A genuinely unresolved alert: no recovery follows.
    store.add_event(
        _ts(27, 3, 0), "2026-10-27", "resource", "内存使用率", "超过阈值", "alert"
    )

    rows = store.list_unresolved_alerts(_ts(28, 0, 0), _ts(1, 0, 0), limit=50)
    names = [row["name"] for row in rows]
    assert names == ["内存使用率"]


def test_failed_alert_pairs_with_recovery_duration(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        _ts(27, 0, 2),
        "2026-10-27",
        "offline",
        "Bot 连接",
        "连接离线（推送失败）",
        "failed",
    )
    store.add_event(
        _ts(27, 0, 9),
        "2026-10-27",
        "offline",
        "Bot 连接",
        "离线时长 00:07:03",
        "recovery",
    )

    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-27", history_days=7, sections=["anomaly"])
    items = view["anomalies"]["items"]
    recovery_items = [item for item in items if item["status_key"] == "recovery"]
    assert len(recovery_items) == 1
    # 00:02 -> 00:09 paired across the failed row: the duration is filled
    # instead of "--".
    assert recovery_items[0]["duration"] == "00:07:00"
    # The failed row itself stays visible as the fault record.
    failed_items = [item for item in items if item["status_key"] == "failed"]
    assert len(failed_items) == 1
