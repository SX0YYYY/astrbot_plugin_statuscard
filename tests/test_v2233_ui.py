"""v2.20.33 展示层：资源卡变化量、峰值超阈值、异常恢复时间、文案。

- 「较前日」改为百分点（百分比指标）或绝对值（1 分钟负载）：CPU 7.6%→10.9%
  显示 +44.3% 会被读成暴涨，实际只有 +3.4pp。
- 峰值超过已配置告警阈值时卡片标红 +「超阈值」，解释「均值没到阈值为什么
  当天有告警」。
- 异常行在时长下补「至 HH:MM」，跨零点显示「次日 HH:MM」。
- 「异常见左表」在单列日报里指向不明，改为「异常项见上方列表」。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder, _signed_abs
from astrbot_plugin_statuscard.t2i_renderer import build_report_html

TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2026, 11, 10)
NEXT_DAY = date(2026, 11, 11)


def _ts(day: date, hour: int = 0, minute: int = 0) -> int:
    base = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    return int((base + timedelta(hours=hour, minutes=minute)).timestamp())


def _builder(store: ReportStore, config: dict | None = None) -> DailyReportBuilder:
    cfg = config or {"display": {"timezone": "Asia/Shanghai", "redact": []}}
    return DailyReportBuilder(store, cfg)


def _store_with_two_days(tmp_path: Path) -> ReportStore:
    """11-09 and 11-10 each have two samples, so the delta is computable."""

    store = ReportStore(tmp_path / "report.db")
    for day in (date(2026, 11, 9), DAY):
        for hour in (8, 9):
            store.add_resource(
                ts=_ts(day, hour),
                cpu=7.5 if day == date(2026, 11, 9) else 10.9,
                ram=60.0 if day == date(2026, 11, 9) else 68.0,
                swap=20.0 if day == date(2026, 11, 9) else 38.0,
                disk=70.0 if day == date(2026, 11, 9) else 81.0,
                load1=0.35 if day == date(2026, 11, 9) else 0.51,
            )
    return store


# ---------------------------------------------------------------------------
# delta: 百分点 / 绝对值
# ---------------------------------------------------------------------------


def test_signed_abs_uses_points_for_percent_and_raw_for_load() -> None:
    assert _signed_abs(10.9, 7.5, "cpu") == "+3.4pp"
    assert _signed_abs(0.51, 0.35, "load1") == "+0.16"
    assert _signed_abs(None, 7.5, "cpu") == "--"
    assert _signed_abs(10.9, None, "cpu") == "--"


def test_resource_cards_expose_point_change(tmp_path: Path) -> None:
    store = _store_with_two_days(tmp_path)
    view = _builder(store).build(day=DAY, sections=["resource"])
    yesterday = view["resources"]["yesterday"]

    assert yesterday["cpu"]["delta_abs_text"] == "+3.4pp"
    # The relative figure stays available for callers that want it.
    assert yesterday["cpu"]["delta_text"] == "+45.3%"
    assert yesterday["load1"]["delta_abs_text"] == "+0.16"


# ---------------------------------------------------------------------------
# 峰值超阈值
# ---------------------------------------------------------------------------


def test_peak_over_threshold_is_flagged(tmp_path: Path) -> None:
    store = _store_with_two_days(tmp_path)
    config = {
        "display": {"timezone": "Asia/Shanghai", "redact": []},
        "alerts": {"cpu_threshold": 85, "disk_threshold": 80},
    }
    yesterday = _builder(store, config).build(day=DAY, sections=["resource"])[
        "resources"
    ]["yesterday"]

    # Disk peaked at 81% with an 80% threshold: the card must explain the
    # alert the day produced.
    assert yesterday["disk"]["peak_over"] is True
    assert yesterday["disk"]["peak_threshold"] == 80.0
    # CPU peaked at 10.9% and stays unmarked.
    assert yesterday["cpu"]["peak_over"] is False


def test_threshold_zero_disables_the_marker(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    for hour in (8, 9):
        store.add_resource(ts=_ts(DAY, hour), cpu=99.0)
    config = {
        "display": {"timezone": "Asia/Shanghai", "redact": []},
        "alerts": {"cpu_threshold": 0},
    }
    yesterday = _builder(store, config).build(day=DAY, sections=["resource"])[
        "resources"
    ]["yesterday"]

    assert yesterday["cpu"]["peak_over"] is False
    assert yesterday["cpu"]["peak_threshold"] is None


# ---------------------------------------------------------------------------
# 异常行恢复时间
# ---------------------------------------------------------------------------


def test_anomaly_row_shows_recovery_time_across_midnight(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        ts=_ts(DAY, 22, 3),
        day=DAY.isoformat(),
        kind="resource",
        name="磁盘使用率 (/)",
        detail="80.3%（阈值 80.0%）",
        status="alert",
        resolved_ts=_ts(NEXT_DAY, 0, 47),
        started_ts=_ts(DAY, 22, 3),
    )
    view = _builder(store).build(day=DAY, sections=["anomaly"])
    item = view["anomalies"]["items"][0]

    assert item["time"] == "22:03"
    assert item["duration"] == "02:44:00"
    assert item["end_time"] == "次日 00:47"


def test_anomaly_row_shows_recovery_time_on_the_same_day(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        ts=_ts(DAY, 10, 0),
        day=DAY.isoformat(),
        kind="service",
        name="站点",
        detail="HTTP 502",
        status="alert",
        resolved_ts=_ts(DAY, 10, 5),
        started_ts=_ts(DAY, 10, 0),
    )
    view = _builder(store).build(day=DAY, sections=["anomaly"])
    item = view["anomalies"]["items"][0]

    assert item["end_time"] == "10:05"


def test_unresolved_row_has_no_recovery_time(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        ts=_ts(DAY, 10, 0),
        day=DAY.isoformat(),
        kind="service",
        name="站点",
        detail="HTTP 502",
        status="alert",
    )
    view = _builder(store).build(day=DAY, sections=["anomaly"])
    item = view["anomalies"]["items"][0]

    assert item["duration"] == "进行中"
    assert item["end_time"] is None


# ---------------------------------------------------------------------------
# 模板渲染
# ---------------------------------------------------------------------------


def test_report_html_marks_over_threshold_and_fixes_wording(tmp_path: Path) -> None:
    store = _store_with_two_days(tmp_path)
    store.add_event(
        ts=_ts(DAY, 22, 3),
        day=DAY.isoformat(),
        kind="resource",
        name="磁盘使用率 (/)",
        detail="80.3%（阈值 80.0%）",
        status="alert",
        resolved_ts=_ts(NEXT_DAY, 0, 47),
        started_ts=_ts(DAY, 22, 3),
    )
    config = {
        "display": {"timezone": "Asia/Shanghai", "redact": []},
        "alerts": {"disk_threshold": 80},
    }
    view = _builder(store, config).build(day=DAY, sections=["resource", "anomaly"])
    html = build_report_html(view)

    assert "异常见左表" not in html
    assert "异常项见上方列表" in html
    assert "over-tag" in html
    assert "超阈值" in html
    assert "至 次日 00:47" in html
    assert "+3.4pp" in html
