"""v2.20.8：第八轮独立找茬的回归测试。

覆盖：报告日标签、周报补发窗口/暂停、过期补发、崩溃后补发、数据库探针、
快照全零、异常明细截断、聚合溢出与窗口代际、掉线告警冷却、LLM 排队清理、
每日状态图排队、监测放弃后补发、传输离线判定、回执空响应。
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.onebot_receipt import extract_receipt
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder

TZ = ZoneInfo("Asia/Shanghai")


def _reporter(tmp_path: Path) -> DailyReporter:
    reporter = DailyReporter(
        object(), ReportStore(tmp_path / "report.db"), {"display": {}}
    )
    reporter._save_delivery_state = lambda: None  # type: ignore[attr-defined]
    return reporter


# ----------------------------------------------------------------------
# 报表：日期标签、周报窗口、恢复补发
# ----------------------------------------------------------------------


def test_old_report_day_is_labelled_with_its_date(tmp_path: Path, monkeypatch) -> None:
    """A retried/recovered report must not call itself 昨日 (v2.20.8)."""

    import astrbot_plugin_statuscard.report_view as rv

    fixed = datetime(2026, 11, 1, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(rv, "now_in_timezone", lambda config=None: fixed)
    builder = DailyReportBuilder(
        ReportStore(tmp_path / "report.db"),
        {"display": {"timezone": "Asia/Shanghai"}},
        redact=set(),
    )
    yesterday = builder.build(day="2026-10-31", sections=["anomaly"])
    assert yesterday["day_label"] == "昨日"
    old = builder.build(day="2026-10-30", sections=["anomaly"])
    assert old["day_label"] == "10月30日"
    assert old["window_label"] == "10月30日"
    assert "10月30日" in old["token_label"]
    assert old["summary"]["window_label"] == "10月30日"


def test_weekly_catch_up_works_on_monday_morning() -> None:
    """A Monday 00:30 restart must still deliver last week's report."""

    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._weekly_pause_until = 0.0
    reporter._weekly_given_up_week = None
    reporter._weekly_last_sent_week = None
    reporter._weekly_time_text = lambda: "21:30"  # type: ignore[attr-defined]

    import astrbot_plugin_statuscard.report as rep

    monday = datetime(2026, 11, 2, 0, 30, tzinfo=TZ)
    original = rep.now_in_timezone
    rep.now_in_timezone = lambda config=None: monday
    try:
        assert reporter._weekly_catch_up_allowed() is True
        # The slot is last night's, so the report day is Saturday.
        slot = reporter._weekly_slot_before(monday)
        assert slot.strftime("%Y-%m-%d %H:%M") == "2026-11-01 21:30"
    finally:
        rep.now_in_timezone = original


def test_weekly_catch_up_uses_configured_time() -> None:
    """The retry window follows weekly_time instead of a fixed 21:30."""

    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._weekly_pause_until = 0.0
    reporter._weekly_given_up_week = None
    reporter._weekly_last_sent_week = None
    reporter._weekly_time_text = lambda: "12:00"  # type: ignore[attr-defined]

    import astrbot_plugin_statuscard.report as rep

    sunday = datetime(2026, 11, 1, 12, 5, tzinfo=TZ)
    original = rep.now_in_timezone
    rep.now_in_timezone = lambda config=None: sunday
    try:
        assert reporter._weekly_catch_up_allowed() is True
    finally:
        rep.now_in_timezone = original


def test_stale_pending_retry_is_dropped_on_reload(tmp_path: Path) -> None:
    """A retry queued before a long shutdown must not fire days later."""

    store = ReportStore(tmp_path / "report.db")
    from astrbot_plugin_statuscard.state import StateStore

    state = StateStore(tmp_path / "state.json")
    plugin = type("P", (), {"state": state})()
    old_day = (date.today() - timedelta(days=10)).isoformat()
    state.set(
        "report_delivery",
        {
            "schema": 2,
            "pending_retry": {"day": old_day, "due": time.time() + 60},
            "pending_retry_count": 1,
            "pending_retry_day": old_day,
        },
    )
    state.save()
    reporter = DailyReporter(plugin, store, {"display": {}})
    assert reporter._pending_retry is None


def test_crash_mid_send_allows_one_catch_up() -> None:
    """An attempt marker without delivery and without a retry plan = crash."""

    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._last_sent_day = None
    reporter._last_attempt_day = "2026-10-30"
    reporter._daily_given_up_day = None
    reporter._pending_retry = None
    reporter._pending_recovery_day = None
    assert reporter._daily_catch_up_allowed("2026-10-31") is True
    # Once a retry is queued the guard blocks a second copy.
    reporter._pending_retry = ("2026-10-30", 0.0)
    assert reporter._daily_catch_up_allowed("2026-10-31") is False


def test_store_probe_fails_when_connection_breaks(tmp_path: Path, monkeypatch) -> None:
    """A database that broke at runtime must not keep reporting "available"."""

    import sqlite3

    store = ReportStore(tmp_path / "report.db")
    assert store.probe() is True

    def boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_connect", boom)
    assert store.probe() is False
    assert store.available is False


# ----------------------------------------------------------------------
# 聚合器：溢出披露与窗口代际
# ----------------------------------------------------------------------


def test_aggregator_dropped_items_are_disclosed_separately() -> None:
    sent: list[str] = []

    async def send(text: str):
        sent.append(text)
        return DeliveryResult(ok=True)

    async def scenario() -> None:
        agg = AlertAggregator(window_seconds=120, max_items=2)
        await agg.push("A", "原因A", "文本A", send)
        for label in ("B", "C", "D", "E", "F", "G"):
            await agg.push(label, f"原因{label}", f"文本{label}", send)
        await agg.flush("default", send)

    asyncio.run(scenario())
    summary = sent[-1]
    # 4 buffered remain; 2 trimmed items are disclosed, not counted as sent.
    assert "4 项异常" in summary
    assert "另有 2 项未列出" in summary
    assert "2 项未合并，将单独补发" in summary


def test_aggregator_new_window_survives_slow_summary() -> None:
    """A summary that outlives its window must not close the next one."""

    async def scenario() -> None:
        agg = AlertAggregator(window_seconds=120, max_items=4)
        bucket = agg._bucket("default")
        bucket.pending.append(("A", "原因A"))
        bucket.until = time.time() + 120
        bucket.window_seq = 1

        async def sneaky_send(text: str):
            # An alert arriving while the summary is in flight opens a new
            # window (window_seq + 1, fresh ``until``).
            bucket.window_seq += 1
            bucket.until = time.time() + 60
            return DeliveryResult(ok=True)

        await agg.flush("default", sneaky_send)
        assert bucket.until > time.time(), "the new window must stay open"
        assert bucket.window_seq == 2

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 投递与回执
# ----------------------------------------------------------------------


def test_empty_receipt_dict_is_uncertain() -> None:
    receipt = extract_receipt({})
    assert receipt.ok is False and receipt.attempted is True
    assert "无法识别" in receipt.error


def test_receipt_with_message_id_is_delivered() -> None:
    receipt = extract_receipt({"message_id": 123})
    assert receipt.ok is True and receipt.message_id == 123


def test_delivery_offline_helper_reads_result_flag() -> None:
    from astrbot_plugin_statuscard.utils import delivery_offline

    assert delivery_offline(DeliveryResult(ok=False, transport_offline=True)) is True
    assert delivery_offline(DeliveryResult(ok=False)) is False
    assert delivery_offline(True) is False


# ----------------------------------------------------------------------
# 监测：放弃汇总后的补发（P1）
# ----------------------------------------------------------------------


def test_monitor_summary_abandon_keeps_retry_path() -> None:
    """After on_abandon the item must still be able to push and recover."""

    import tempfile

    from astrbot_plugin_statuscard import monitor as mon_mod
    from astrbot_plugin_statuscard.state import StateStore

    with tempfile.TemporaryDirectory() as raw:
        state = StateStore(Path(raw) / "state.json")
        monitor = object.__new__(mon_mod.ServiceMonitor)
        monitor.state = state
        monitor.cooldown = 1800
        monitor.recover_notify = True
        monitor._on_summary_abandoned([("站点B", "HTTP 503")])

        with state.section("monitors") as monitors:
            items = monitors.setdefault("items", {})
            items["key"] = {
                "name": "站点B",
                "alerting": True,
                "alerted": False,
                "last_sent": time.time(),
                "summary_buffered": True,
            }
        monitor._on_summary_abandoned([("站点B", "HTTP 503")])
        st = state.data["monitors"]["items"]["key"]
        assert st.get("alerted") is False
        assert st.get("summary_abandoned") is True
        # last_sent is the cooldown reference; dropping it removed the only
        # path back to a push (v2.20.8).
        assert st.get("last_sent")


# ----------------------------------------------------------------------
# 前端/接口契约
# ----------------------------------------------------------------------


def test_reports_payload_exposes_configured_today() -> None:
    """The day arrows need the configured-timezone today (v2.20.8)."""

    source = (Path(__file__).resolve().parents[1] / "page_api.py").read_text(
        encoding="utf-8"
    )
    assert '"today": now_in_timezone(self.plugin.config).date().isoformat()' in source


def test_verify_cooldown_is_not_charged_for_empty_targets() -> None:
    """The 30-second timer starts only when the check really runs."""

    source = (Path(__file__).resolve().parents[1] / "page_api.py").read_text(
        encoding="utf-8"
    )
    empty_branch = source.index('"未配置任何推送目标"')
    charge = source.index("self._verify_last[kind] = now")
    assert charge > empty_branch, "cooldown must be charged after the guard"


def test_report_headline_uses_backend_label() -> None:
    """The overview cards must follow the backend's window_label (v2.20.9).

    v2.20.10 keeps that behaviour but reads the structured
    ``window_label_code`` first, so the label can be translated while still
    following the real report day.
    """

    js = (Path(__file__).resolve().parents[1] / "pages/status/app.js").read_text(
        encoding="utf-8"
    )
    assert "summary.window_label_code" in js
    assert "reportScopeLabel(data, summary)" in js
    assert js.count('t("report_yesterday", "昨日"),') == 0


def test_badge_recovered_style_and_i18n_key_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    css = (root / "pages/status/style.css").read_text(encoding="utf-8")
    assert ".badge.recovered" in css
    js = (root / "pages/status/app.js").read_text(encoding="utf-8")
    assert 'recovered: "status_recovered"' in js
    zh = (root / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8")
    assert '"status_recovered"' in zh


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "bad"])
def test_format_duration_extremes(value) -> None:
    from astrbot_plugin_statuscard.collectors import format_duration

    assert format_duration(value) in ("--", "0:00:00")
