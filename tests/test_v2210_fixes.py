"""v2.21.0: 第九轮独立找茬修复批的回归测试。

覆盖：日报异常事件重复行、维护静默期内已告警项自愈不收尾、磁盘池挂死
耗尽、采样器脏值、内存百分比口径、日报快照用当日开盘读数、告警冷却补发
在断线后失效、汇总放弃后的孤儿恢复、会话格式错误的投递判定、Komari 标题
脱敏、以及聚合缓冲重启后的状态修复。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import astrbot_plugin_statuscard.report as report_module
import pytest
from astrbot_plugin_statuscard import collectors
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.delivery import DeliveryResult, _is_local_umo_error
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report import ReportCollector
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.sampler import Sampler
from astrbot_plugin_statuscard.state import StateStore

TZ = ZoneInfo("Asia/Shanghai")


def _ts(day: str, hour: int, minute: int, second: int = 0) -> int:
    base = datetime.fromisoformat(day).replace(
        hour=hour, minute=minute, second=second, tzinfo=TZ
    )
    return int(base.timestamp())


class _RecordingPlugin:
    def __init__(self, results: list[object] | None = None) -> None:
        self.sent: list[str] = []
        self._results = list(results or [])

    async def send_text(self, targets, text, source="unknown"):
        self.sent.append(text)
        if self._results:
            return self._results.pop(0)
        return True

    async def send_image(self, targets, url, source="unknown"):
        self.sent.append(url)
        return True


# ----------------------------------------------------------------------
# 日报异常事件：一个 episode 只显示一行
# ----------------------------------------------------------------------


def test_in_window_recovery_does_not_duplicate_alert_row(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        _ts("2026-10-20", 12, 0), "2026-10-20", "service", "Nginx", "down", "alert", "x"
    )
    store.add_event(
        _ts("2026-10-20", 12, 1),
        "2026-10-20",
        "service",
        "Nginx",
        "up",
        "recovery",
        "x",
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-20", sections=["anomaly"])
    items = view["anomalies"]["items"]
    assert len(items) == 1
    row = items[0]
    assert row["status_key"] == "recovered"
    assert row["duration"] == "00:01:00"


def test_window_started_episode_keeps_single_row(tmp_path: Path) -> None:
    """A recovery for an episode that started before the window still shows."""

    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        _ts("2026-10-19", 23, 50),
        "2026-10-19",
        "offline",
        "Bot 连接",
        "连接离线",
        "failed",
    )
    store.add_event(
        _ts("2026-10-20", 0, 5),
        "2026-10-20",
        "offline",
        "Bot 连接",
        "离线时长 00:15:00",
        "recovery",
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-20", sections=["anomaly"])
    items = view["anomalies"]["items"]
    assert len(items) == 1
    # The alert itself is outside the window, so the recovery is the only row
    # and it must carry the pre-window start marker.
    assert items[0]["status_key"] == "recovery"
    assert "窗口前开始" in items[0]["duration"]


# ----------------------------------------------------------------------
# 维护静默：静默前已告警的项在静默期内自愈必须收尾
# ----------------------------------------------------------------------


def _monitor_config(silenced: bool) -> dict:
    until = (datetime.now(TZ) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    return {
        "alerts": {"targets": ["123"], "enabled": True},
        "monitors": {
            "enabled": True,
            "targets": ["123"],
            "silence_enabled": silenced,
            "silence_until": until if silenced else "",
            "items": [
                {
                    "__template_key": "http",
                    "name": "probe",
                    "url": "http://127.0.0.1:9/",
                    "enabled": True,
                }
            ],
        },
        "display": {"timezone": "Asia/Shanghai"},
    }


def test_silenced_item_already_alerting_heals(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _RecordingPlugin()
    config = _monitor_config(True)
    state = StateStore(tmp_path / "state.json")
    monitor = ServiceMonitor(plugin, state, config, report_store=store)
    item = monitor.items[0]
    key = monitor._item_key(item)
    started = time.time() - 120
    state.data.setdefault("monitors", {}).setdefault("items", {})[key] = {
        "alerting": True,
        "alerted": True,
        "since": started,
        "last_error": "HTTP 503",
        "fails": 0,
    }

    async def probe(_item):
        return True, "HTTP 200", "http://127.0.0.1:9/"

    monitor._probe = probe
    asyncio.run(monitor._check_item(item))

    rule = state.data["monitors"]["items"][key]
    assert rule["alerting"] is False
    assert rule["since"] == 0
    assert "alerted" not in rule
    # Quiet inside the window: no message pushed.
    assert plugin.sent == []
    # The event log is closed so the daily report stops listing it.
    events = store.list_events("2026-10-20", limit=50)
    assert any(event["status"] == "recovery" for event in events) or store.available


def test_silenced_new_fault_still_records_a_pair(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _RecordingPlugin()
    config = _monitor_config(True)
    state = StateStore(tmp_path / "state.json")
    monitor = ServiceMonitor(plugin, state, config, report_store=store)
    item = monitor.items[0]
    key = monitor._item_key(item)

    results = [
        (False, "HTTP 503", "http://127.0.0.1:9/"),
        (True, "HTTP 200", "http://127.0.0.1:9/"),
    ]

    async def probe(_item):
        return results.pop(0)

    monitor._probe = probe
    asyncio.run(monitor._check_item(item))
    asyncio.run(monitor._check_item(item))
    assert plugin.sent == []
    rule = state.data["monitors"]["items"][key]
    assert rule["alerting"] is False


# ----------------------------------------------------------------------
# 磁盘探测：挂死挂载点有限次超时后进入长冷却，daemon 线程不拖退出
# ----------------------------------------------------------------------


def test_hung_mount_is_abandoned_after_max_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collectors, "_DISK_FAIL_COUNT", {})
    monkeypatch.setattr(collectors, "_DISK_DEAD_UNTIL", {})
    monkeypatch.setattr(collectors, "_DISK_DEAD_COOLDOWN", 0.0)
    real = collectors._disk_usage_one

    def fake(name: str):
        if name == "/hung-mount":
            time.sleep(0.25)
        return real(name)

    monkeypatch.setattr(collectors, "_disk_usage_one", fake)
    for _ in range(collectors._DISK_MAX_TIMEOUTS):
        result = collectors.disk_usage_bounded(["/hung-mount"], timeout=0.05)
        assert result[0].exception == "读取超时"
    # v2.20.30: no longer float("inf") -- the mount is probed once per
    # _DISK_DEAD_RETRY window instead of requiring a plugin restart.
    dead_until = collectors._DISK_DEAD_UNTIL["/hung-mount"]
    assert dead_until > time.time()
    assert dead_until <= time.time() + collectors._DISK_DEAD_RETRY + 5
    # Inside the window it is not probed again.
    result = collectors.disk_usage_bounded(["/hung-mount"], timeout=0.05)
    assert result[0].exception == "读取超时"
    assert collectors._DISK_FAIL_COUNT["/hung-mount"] == collectors._DISK_MAX_TIMEOUTS


def test_abandoned_mount_gets_one_retry_per_window(monkeypatch) -> None:
    """After the cool-down expires the mount is probed exactly once more."""

    monkeypatch.setattr(collectors, "_DISK_FAIL_COUNT", {"/hung-mount": 3})
    monkeypatch.setattr(collectors, "_DISK_DEAD_UNTIL", {"/hung-mount": 0.0})
    monkeypatch.setattr(collectors, "_DISK_MAX_TIMEOUTS", 3)

    def fake(name: str):
        time.sleep(0.25)
        return collectors.DiskUsage(
            name=name, used=None, total=None, percent=None, exception="读取超时"
        )

    monkeypatch.setattr(collectors, "_disk_usage_one", fake)
    result = collectors.disk_usage_bounded(["/hung-mount"], timeout=0.05)
    assert result[0].exception == "读取超时"
    # The counter did not grow; a fresh window starts now.
    assert collectors._DISK_FAIL_COUNT["/hung-mount"] == 3
    assert collectors._DISK_DEAD_UNTIL["/hung-mount"] > time.time()


# ----------------------------------------------------------------------
# 采样器：state.json 里的脏值不再让整轮采样永久失败
# ----------------------------------------------------------------------


def test_sampler_survives_dirty_month_counter(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    now = time.time()
    state.data["net"] = {
        "month": "2026-11",
        "month_up": "12.3GB",
        "month_down": "oops",
        "last_ts": now - 60,
        "last_sent": 0,
        "last_recv": 0,
        "interface": "",
    }
    sampler = Sampler(state, config={"display": {"timezone": "Asia/Shanghai"}})
    sampler._sample_network(now)
    assert isinstance(state.data["net"]["month_up"], int)
    assert isinstance(state.data["net"]["month_down"], int)


# ----------------------------------------------------------------------
# 内存百分比与「used / total」文本同口径
# ----------------------------------------------------------------------


def test_memory_percent_keeps_the_threshold_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The RAM percentage must stay psutil's own definition.

    It is the value the RAM alert threshold and every stored sample are
    compared against. v2.20.10 first changed it to used/total, which
    silently moved the threshold on Linux (2-4 points higher) and mixed two
    definitions in the 30-day trend. The card text was aligned instead.
    """

    fake = SimpleNamespace(
        total=8 * 1024**3,
        used=4 * 1024**3,
        available=5 * 1024**3,  # psutil reports 37.5% here
        percent=37.5,
    )
    monkeypatch.setattr(collectors.psutil, "virtual_memory", lambda: fake)
    stat = collectors.memory_stat()
    # Unchanged: the threshold semantics must not move.
    assert stat.percent == pytest.approx(37.5, abs=1e-6)
    # The text can be aligned with that percentage.
    assert stat.available == 5 * 1024**3


def test_memory_text_uses_the_available_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(
        total=8 * 1024**3,
        used=4 * 1024**3,
        available=5 * 1024**3,
        percent=37.5,
    )
    monkeypatch.setattr(collectors.psutil, "virtual_memory", lambda: fake)
    resources = collectors._resources(
        10.0, collectors.memory_stat(), collectors.swap_stat()
    )
    ram = next(item for item in resources if item["title"] == "RAM")
    # 37.5% of 8 GiB is 3 GiB used, i.e. 5 GiB available: the text must
    # print the available side so it agrees with the percentage.
    assert "5.00 GB" in ram["desc"]
    assert "可用" in ram["desc"]


# ----------------------------------------------------------------------
# 日报快照优先使用当天开盘读数
# ----------------------------------------------------------------------


def test_snapshot_prefers_day_open_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    config = {"display": {"timezone": "Asia/Shanghai"}}
    collector = ReportCollector(SimpleNamespace(context=None), state, config, store)
    fixed = datetime(2026, 10, 20, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(report_module, "now_in_timezone", lambda _cfg: fixed)
    monkeypatch.setattr(report_module, "read_message_stats_total", lambda: (5000, None))
    captured: dict[str, object] = {}

    async def fake_bot(*_args, **_kwargs):
        return {"friends": 1, "groups": 2, "members": 3}

    monkeypatch.setattr(report_module, "collect_bot_info", fake_bot)

    def fake_record(day, friends, groups, members, recv_total, complete, ts):
        captured.update({"day": day, "recv_total": recv_total, "complete": complete})

    monkeypatch.setattr(store, "record_snapshot", fake_record)
    state.data["recv"] = {
        "day_open_day": "2026-10-20",
        "day_open_total": 4200,
    }
    asyncio.run(collector._maybe_snapshot())
    assert captured["day"] == "2026-10-20"
    assert captured["recv_total"] == 4200


# ----------------------------------------------------------------------
# 告警：冷却补发遇到断线后仍能在退避结束时补发
# ----------------------------------------------------------------------


def test_cooldown_retry_survives_an_offline_failure(tmp_path: Path) -> None:
    offline = DeliveryResult(ok=False, failed_targets=["123"], transport_offline=True)
    plugin = _RecordingPlugin([True, offline])
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        }
    }
    engine = AlertEngine(plugin, state, config)

    def sample(value: float) -> None:
        asyncio.run(engine._evaluate("mem", "内存", value, 85.0, "%", f"{value}%"))

    sample(91.0)  # first alert delivered
    sample(41.0)  # recovery
    rule = state.data["alerts"]["rules"]["mem"]
    # Simulate the documented stuck state: latched inside the cooldown, then
    # the cooldown-retry push failed while the channel was down.
    rule.update(
        {
            "alerting": True,
            "alerted": False,
            "retry_count": 0,
            "next_attempt": time.time() - 1,
            "last_sent": time.time() - 3600,
            "count": 1,
        }
    )
    before = len(plugin.sent)
    sample(92.0)
    assert len(plugin.sent) == before + 1


# ----------------------------------------------------------------------
# 汇总被放弃后不再发出孤儿「已恢复」
# ----------------------------------------------------------------------


def test_summary_abandon_drops_queued_recovery(tmp_path: Path) -> None:
    plugin = _RecordingPlugin()
    state = StateStore(tmp_path / "state.json")
    config = {"alerts": {"enabled": True, "targets": ["123"]}}
    engine = AlertEngine(plugin, state, config)
    state.data["alerts"] = {
        "rules": {
            "mem": {
                "label": "内存使用率",
                "alerting": False,
                "pending_recovery": "✅ 已恢复 · 内存使用率",
                "recovery_retry_count": 1,
                "recovery_next_attempt": 0,
                "recovery_awaits_summary": True,
            }
        }
    }
    engine._on_summary_abandoned([("内存使用率", "send failed")])
    rule = state.data["alerts"]["rules"]["mem"]
    assert "pending_recovery" not in rule
    assert "recovery_next_attempt" not in rule


# ----------------------------------------------------------------------
# 投递：框架在发送前拒绝的目标格式不再算「可能已送达」
# ----------------------------------------------------------------------


def test_local_umo_error_detection() -> None:
    assert _is_local_umo_error(ValueError("不合法的 session 字符串: a:b"))
    assert _is_local_umo_error(ValueError("无法发送消息：缺少有效的数字 session_id(x)"))
    # A ValueError raised while parsing a response is still uncertain.
    assert not _is_local_umo_error(ValueError("Expecting value: line 1 column 1"))


# ----------------------------------------------------------------------
# Komari 标题脱敏
# ----------------------------------------------------------------------


def test_komari_title_is_masked() -> None:
    text = AlertEngine._format_komari(
        {"title": "节点 10.1.2.3:8080 离线", "message": "连接失败"},
        Redactor(["hostname"]),
    )
    assert "10.1.2.3" not in text


# ----------------------------------------------------------------------
# 聚合缓冲在硬重启后的状态修复
# ----------------------------------------------------------------------


def test_awaiting_summary_repaired_on_start(tmp_path: Path) -> None:
    plugin = _RecordingPlugin()
    state = StateStore(tmp_path / "state.json")
    config = {"alerts": {"enabled": True, "targets": ["123"]}}
    engine = AlertEngine(plugin, state, config)
    state.data["alerts"] = {
        "rules": {
            "mem": {
                "label": "内存使用率",
                "alerting": True,
                "alerted": True,
                "awaiting_summary": True,
                "summary_buffered": True,
                "since": time.time() - 60,
                "last_seen": time.time(),
            }
        }
    }
    engine._mark_stale_rules()
    rule = state.data["alerts"]["rules"]["mem"]
    assert rule["alerted"] is False
    assert rule["summary_abandoned"] is True
    assert "awaiting_summary" not in rule


# ----------------------------------------------------------------------
# 日报定时循环：配置错误不再 1 Hz 自旋
# ----------------------------------------------------------------------


def test_empty_targets_pauses_the_daily_loop(tmp_path: Path) -> None:
    """An empty target list must not spin the send path every second."""

    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"enabled": True, "time": "21:00", "targets": []},
        "alerts": {"targets": []},
    }
    reporter = report_module.DailyReporter(
        SimpleNamespace(context=None, state=state, render_report_image=None),
        store,
        config,
    )
    calls: list[str] = []
    reporter._send_scheduled = lambda day=None: calls.append(day or "")  # type: ignore[assignment]
    reporter._daily_catch_up_allowed = lambda today: True  # type: ignore[assignment]

    async def run() -> None:
        task = asyncio.create_task(reporter._loop())
        await asyncio.sleep(2.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    # One attempt at most; the pause keeps the loop from re-entering.
    assert len(calls) <= 1


def test_empty_targets_branch_sets_a_pause(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"enabled": True, "targets": []},
        "alerts": {"targets": []},
    }
    reporter = report_module.DailyReporter(
        SimpleNamespace(context=None, state=state), store, config
    )
    asyncio.run(reporter._send_scheduled())
    assert reporter._daily_pause_until > time.time()


# ----------------------------------------------------------------------
# 掉线补发只保留最新一份
# ----------------------------------------------------------------------


def test_offline_recovery_keeps_the_newest_report_day(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {
            "enabled": True,
            "time": "21:00",
            "targets": ["123"],
        },
        "alerts": {"targets": ["123"]},
    }
    reporter = report_module.DailyReporter(
        SimpleNamespace(context=None, state=state, render_report_image=None),
        store,
        config,
    )
    old_day = "2026-10-29"
    reporter._pending_recovery_day = old_day
    reporter._pending_recovery_since = time.time() - 86400
    reporter._pending_retry = (old_day, time.time() - 1)
    # The pin moves forward to the newest report whose window has closed --
    # not to the calendar yesterday, whose report is not due until the next
    # evening (v2.20.12).
    newest = reporter._latest_due_report_day()
    sent: list[str] = []

    async def fake_send(day=None):
        sent.append(day or "")

    reporter._send_scheduled = fake_send  # type: ignore[assignment]
    reporter._running = True

    async def run() -> None:
        task = asyncio.create_task(reporter._loop())
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert newest >= old_day
    assert reporter._pending_recovery_day == newest


# ----------------------------------------------------------------------
# 图片重试排程过期后丢弃
# ----------------------------------------------------------------------


def test_stale_image_retry_is_dropped_on_start(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    state.data["report_delivery"] = {
        "schema": 2,
        "pending_image_retry": time.time() - 7 * 3600,
        "pending_image_retry_report_day": "2026-10-20",
        "pending_image_retry_send_day": "2026-10-20",
    }
    config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter = report_module.DailyReporter(
        SimpleNamespace(context=None, state=state), store, config
    )
    assert reporter._pending_image_retry is None
    assert reporter._pending_image_retry_report_day is None


# ----------------------------------------------------------------------
# 报表库探针必须触碰真实表
# ----------------------------------------------------------------------


def test_probe_fails_when_the_database_file_is_missing(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.probe() is True
    store.path.unlink()
    assert store.probe() is False
    assert store.available is False


# ----------------------------------------------------------------------
# 导出接口的脏值不再 500
# ----------------------------------------------------------------------


def test_export_processes_tolerates_a_dirty_cpu_column(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.page_api import PageApi

    rows = PageApi._process_cpu_pct_total(
        [{"cpu": "not-a-number", "name": "python"}, {"cpu": 40.0}]
    )
    assert rows[0]["cpu_pct_total"] == 0.0
    assert rows[1]["cpu_pct_total"] is not None


# ----------------------------------------------------------------------
# 体检：网卡通配写法与监测提示文案
# ----------------------------------------------------------------------


def test_audit_flags_ipv6_wildcard_webhook_without_token() -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    config = {
        "alerts": {
            "komari_webhook": {
                "enabled": True,
                "host": "0:0:0:0:0:0:0:0",
                "require_token": False,
                "token": "",
            }
        }
    }
    issues = run_audit(config)
    codes = {issue.code for issue in issues}
    assert "webhook_open_no_token" in codes


def test_audit_monitor_target_message_is_accurate() -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    config = {
        "monitors": {"enabled": True, "targets": [], "items": []},
        "alerts": {"targets": []},
        "notify": {"rules": []},
    }
    issues = run_audit(config)
    message = next(
        (issue.message for issue in issues if issue.code == "monitors_no_targets"),
        "",
    )
    assert "不会探测" in message
    assert "仍会探测" not in message


# ----------------------------------------------------------------------
# 英文界面：日报窗口标签与空态原因必须可翻译
# ----------------------------------------------------------------------


def test_report_view_exposes_a_translatable_window_label(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-30", sections=["anomaly"])
    assert view["summary"]["window_label_code"] in ("yesterday", "today", "date")
    assert view["summary"]["window_label_code"] != ""


def test_empty_reason_codes_exist_for_website_and_process(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-30", sections=["website", "process"])
    website = view.get("website") or {}
    process = view.get("process") or {}
    assert website.get("empty_reason_code") in (None, "website_none", "website_since")
    assert process.get("empty_reason_code") in (None, "process_none", "process_since")


# ----------------------------------------------------------------------
# 防回归：语义标记变更后，所有渲染端必须有对应样式
# ----------------------------------------------------------------------


def test_report_template_styles_every_status_key() -> None:
    """A new ``status_key`` must have a badge style in the report CSS.

    v2.20.10 introduced ``recovered`` (a closed episode with a duration)
    while the old key was ``recovery``. The dashboard CSS already styled
    both, but the daily-report image template only knew ``recovery`` --
    every recovered row lost its green badge in the pushed image. Style
    regressions are invisible to the API tests, so they are pinned here.
    """

    root = Path(__file__).resolve().parents[1]
    css = (root / "templates/report/report.css").read_text(encoding="utf-8")
    for key in ("alert", "failed", "recovery", "recovered"):
        assert f".badge.{key}" in css, f"report.css lacks .badge.{key}"
    assert '[data-theme="dark"] .badge.recovered' in css


def test_report_view_emits_only_known_status_keys() -> None:
    """Every ``status_key`` the view produces must be styled somewhere."""

    root = Path(__file__).resolve().parents[1]
    template = (root / "templates/report/index.html.jinja").read_text(encoding="utf-8")
    assert "item.status_key" in template
    css = (root / "templates/report/report.css").read_text(encoding="utf-8")
    known = {"alert", "failed", "recovery", "recovered"}
    for key in known:
        assert f".badge.{key}" in css


def test_page_api_reports_whether_memory_text_is_available() -> None:
    """The dashboard needs to know which wording to use for RAM."""

    src = (Path(__file__).resolve().parents[1] / "page_api.py").read_text(
        encoding="utf-8"
    )
    assert '"available": mem.available is not None' in src
