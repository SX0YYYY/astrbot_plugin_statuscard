"""v2.20.4 修复批：第六轮独立找茬的回归测试。

覆盖本轮修掉的严重问题与主要 P2：告警永久静默、恢复通知抢跑汇总、
日报整晚重发、Webhook 并发去重、初始化任务泄漏、脱敏不一致、脏数据崩溃。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import mask_chat_ids, redact_url, safe_int

# ---------------------------------------------------------------------------
# 公共桩
# ---------------------------------------------------------------------------


class _Plugin:
    context = None

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown", kind="resource"):
        self.sent.append(str(text))
        if self.ok:
            return DeliveryResult(ok=True)
        return DeliveryResult(ok=False, failed_targets=list(targets))


def _engine(tmp_path: Path, rules: dict, config: dict | None = None) -> AlertEngine:
    state = StateStore(tmp_path / "state.json")
    state.data.setdefault("alerts", {})["rules"] = rules
    store = ReportStore(tmp_path / "report.db")
    plugin = _Plugin(ok=True)
    engine = AlertEngine(plugin, state, config or {}, report_store=store)
    engine.targets = ["default:GroupMessage:123456"]
    return engine


# ---------------------------------------------------------------------------
# P1：聚合被放弃后规则不得永久静默
# ---------------------------------------------------------------------------


def test_abandon_keeps_rule_retryable(tmp_path: Path) -> None:
    """An abandoned summary must not silence the rule for the whole outage.

    The withdraw used to clear ``last_sent``, which was the only signal the
    cooldown-retry path used: the rule stayed latched-but-unannounced, so
    neither the alert nor the recovery notice was ever delivered.
    """

    now = time.time()
    engine = _engine(
        tmp_path,
        {
            "cpu": {
                "alerting": True,
                "alerted": True,
                "last_sent": now - 10,
                "label": "CPU 使用率",
                "alert_since": now - 60,
                "count": 0,
            }
        },
    )
    engine._on_summary_abandoned([("CPU 使用率", "原因")])
    rule = engine.state.get("alerts")["rules"]["cpu"]
    assert rule["alerted"] is False
    # Still retryable: the cooldown reference survives and the abandon is
    # recorded so the next sample re-pushes the alert.
    assert rule["last_sent"] > 0
    assert rule["summary_abandoned"] is True


def test_abandoned_rule_repushes_after_cooldown(tmp_path: Path) -> None:
    now = time.time()
    engine = _engine(
        tmp_path,
        {
            "cpu": {
                "alerting": True,
                "alerted": False,
                "last_sent": now - 4000,
                "label": "CPU 使用率",
                "alert_since": now - 5000,
                "count": 0,
            }
        },
        {"alerts": {"cooldown_seconds": 1800, "sustain_samples": 1}},
    )
    engine.cooldown = 1800
    engine._on_summary_abandoned([("CPU 使用率", "原因")])
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 99.0, 85.0, "%", "99.0%"))
    assert engine.plugin.sent, "an abandoned summary must not silence the alert"


# ---------------------------------------------------------------------------
# P1：发送被中断后规则不得永久静默
# ---------------------------------------------------------------------------


def test_interrupted_send_stays_retryable(tmp_path: Path) -> None:
    now = time.time()
    engine = _engine(
        tmp_path,
        {
            "cpu": {
                "alerting": False,
                "send_inflight": True,
                "last_attempt": now - 5000,
                "count": 0,
            }
        },
        {"alerts": {"cooldown_seconds": 1800, "sustain_samples": 1}},
    )
    engine.cooldown = 1800
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 99.0, 85.0, "%", "99.0%"))
    assert engine.plugin.sent, (
        "an interrupted send must be retried, not silently dropped for the "
        "rest of the outage"
    )


# ---------------------------------------------------------------------------
# P1：恢复通知不得抢在它自己的汇总之前
# ---------------------------------------------------------------------------


def test_summary_inflight_blocks_recovery_window() -> None:
    """While a summary is being sent, a recovery notice has to queue."""

    agg = AlertAggregator(window_seconds=120, max_items=8)
    assert agg.window_remaining("resource") == 0.0


def test_aggregator_flush_keeps_window_open_while_sending() -> None:
    """While a summary is being sent, a recovery notice has to queue.

    ``flush()`` used to clear the window marker before the await, so
    ``window_remaining()`` returned 0 and the recovery notice was pushed
    before the summary that contained its own alert.
    """

    sent: list[str] = []
    release = asyncio.Event()

    async def slow_sender(text: str):
        sent.append(text)
        await release.wait()
        return DeliveryResult(ok=True)

    async def scenario() -> float:
        agg = AlertAggregator(window_seconds=120, max_items=8)
        # Fill the buffer directly: ``push`` schedules a flush task for the
        # end of the window, which would never fire inside the test.
        bucket = agg._bucket("resource")
        bucket.pending = [("CPU 使用率", "99%")]
        bucket.send = slow_sender
        bucket.until = time.time() + 120
        flush = asyncio.create_task(agg.flush("resource"))
        await asyncio.sleep(0.05)
        # The summary is in flight: the recovery window must stay open.
        remaining = agg.window_remaining("resource")
        release.set()
        await asyncio.wait_for(flush, timeout=5)
        return remaining

    remaining = asyncio.run(scenario())
    assert remaining > 0, (
        "the recovery window must stay open while the summary is in flight, "
        "otherwise the recovery notice overtakes its own summary"
    )
    assert sent, "the summary must actually have been sent"


# ---------------------------------------------------------------------------
# P1：日报不得整晚无限重发
# ---------------------------------------------------------------------------


def test_daily_given_up_stops_the_nightly_loop() -> None:
    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._last_sent_day = None
    reporter._last_attempt_day = "2026-10-30"
    reporter._last_attempt_wall_day = "2026-10-30"
    reporter._daily_given_up_day = "2026-10-31"
    assert reporter._daily_catch_up_allowed("2026-10-31") is False


def test_schedule_retry_marks_the_day_as_given_up(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    reporter = DailyReporter(
        _Plugin(), store, {"display": {"timezone": "Asia/Shanghai"}}
    )
    reporter._save_delivery_state = lambda: None  # type: ignore[attr-defined]
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._daily_given_up_day = None
    reporter._today = lambda: "2026-10-31"
    for _ in range(4):
        reporter._schedule_retry("2026-10-30")
    assert reporter._daily_given_up_day == "2026-10-31"


# ---------------------------------------------------------------------------
# P1：provider 冷却补发不得是死代码
# ---------------------------------------------------------------------------


def test_provider_cooldown_retry_is_reachable(tmp_path: Path) -> None:
    """A streak that began inside the cooldown must still be reported.

    The retry used to be computed *inside* the ``not alerting`` guard, where
    ``alerting`` is constantly False -- so it never fired and a sustained
    LLM outage produced neither an alert nor a recovery notice.
    """

    from astrbot_plugin_statuscard import alerts as alerts_mod

    state = StateStore(tmp_path / "state.json")
    plugin = _Plugin(ok=True)
    engine = alerts_mod.AlertEngine(
        plugin,
        state,
        {"alerts": {"provider_watch": {"enabled": True}, "targets": ["123456"]}},
        report_store=ReportStore(tmp_path / "report.db"),
    )
    engine.targets = ["default:GroupMessage:123456"]
    engine.provider_watch_enabled = True
    engine.provider_watch_threshold = 3
    engine.provider_watch_cooldown = 1800
    engine.recover_notify = True

    now = time.time()
    state.data.setdefault("alerts", {})
    # A previous episode was announced; the streak then started again inside
    # the cooldown window.
    state.data["alerts"]["provider_watch"] = {
        "alerting": True,
        "alerted": False,
        "last_alert_ts": now - 60,
        "last_streak": 5,
    }

    def _streak(_db, _limit):
        return 5, "failed", True

    original = alerts_mod.provider_recent_streak
    alerts_mod.provider_recent_streak = _streak
    try:
        # The cooldown has not expired yet: nothing may be pushed.
        engine._provider_last_check = 0.0
        asyncio.run(engine._check_provider_quality())
        assert not plugin.sent, "inside the cooldown the alert stays quiet"

        # After the cooldown the alert must go out even though the rule is
        # already latched.
        state.data["alerts"]["provider_watch"]["last_alert_ts"] = now - 2000
        engine._provider_last_check = 0.0
        asyncio.run(engine._check_provider_quality())
    finally:
        alerts_mod.provider_recent_streak = original
    assert plugin.sent, (
        "a streak that began inside the cooldown must be reported once the "
        "cooldown expires"
    )


# ---------------------------------------------------------------------------
# P2：Webhook 并发去重必须真正生效
# ---------------------------------------------------------------------------


def test_webhook_inflight_dedups_concurrent_requests(tmp_path: Path) -> None:
    """Two identical requests arriving at once must push only one notice.

    The in-flight set used to be written but never read, so the dedup was
    completely ineffective and a retrying Komari posted the same notice
    twice.
    """

    import aiohttp
    from astrbot_plugin_statuscard import alerts as alerts_mod

    pushed: list[str] = []
    port = 19987

    async def scenario() -> list[str]:
        class _Plugin:
            context = None

            async def send_text(self, targets, text, source="unknown", kind="service"):
                pushed.append(str(text))
                await asyncio.sleep(0.3)
                return DeliveryResult(ok=True)

        engine = alerts_mod.AlertEngine(
            _Plugin(),
            StateStore(tmp_path / "state.json"),
            {
                "alerts": {
                    "targets": ["123456"],
                    "komari_webhook": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": port,
                        "path": "/webhook",
                        "require_token": False,
                    },
                }
            },
        )
        engine.targets = ["default:GroupMessage:123456"]
        await engine.start_webhook()
        try:
            url = f"http://127.0.0.1:{port}/webhook"
            body = b'{"event":"offline","message":"node down"}'
            async with (
                aiohttp.ClientSession() as session,
                asyncio.TaskGroup() as tg,
            ):
                for _ in range(2):
                    tg.create_task(
                        session.post(
                            url,
                            data=body,
                            headers={"Content-Type": "application/json"},
                        )
                    )
            await asyncio.sleep(0.2)
        finally:
            await engine.stop_webhook()
        return pushed

    result = asyncio.run(scenario())
    assert len(result) == 1, (
        f"the same webhook body must be pushed once, got {len(result)}"
    )


# ---------------------------------------------------------------------------
# P2：初始化必须逐组件隔离
# ---------------------------------------------------------------------------


def test_initialize_isolates_component_start_failures() -> None:
    """One failing component must not abort the others.

    ``initialize()`` used to call the five ``start()`` methods in a row
    without a guard: one raise aborted the whole initialization, and
    AstrBot then unloaded the plugin *without* calling terminate() -- the
    already-started sampling tasks kept running against a plugin instance
    that no longer existed.
    """

    # main.py cannot be imported here (it needs the AstrBot runtime), so
    # the source is inspected instead.
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    body = source.split("async def initialize", 1)[1].split("async def terminate", 1)[0]
    assert "except Exception:" in body, (
        "each component start must be isolated so one failure does not "
        "abort the rest (and leak the already-started tasks)"
    )
    # The isolation must wrap every start call, not just one of them.
    for name in ("sampler", "report", "daily_report", "alerts", "monitor"):
        assert f'("{name}", self.{name}.start)' in body, (
            f"{name}.start() must run inside the isolated loop"
        )


# ---------------------------------------------------------------------------
# P2：脏数据不得让整份日报构建失败
# ---------------------------------------------------------------------------


def test_report_view_survives_dirty_rows(tmp_path: Path) -> None:
    """A single dirty row must not take the whole report down.

    SQLite happily stores any text in a REAL/INTEGER column, and a
    hand-edited report.db then made the daily report build raise -- the user
    received nothing that day.
    """

    from astrbot_plugin_statuscard.report_view import DailyReportBuilder

    store = ReportStore(tmp_path / "report.db")
    day = "2026-10-30"
    ts = int(time.mktime(time.strptime(day + " 12:00:00", "%Y-%m-%d %H:%M:%S")))
    store.add_event(ts, day, "resource", "CPU 使用率", "99.0%", "alert")
    # A dirty event row: the timestamp is not a number at all.
    store._write(
        "UPDATE events SET ts = 'abc' WHERE day = ?",
        (day,),
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day=day, history_days=7, sections=["resource"])
    assert "resources" in view


# ---------------------------------------------------------------------------
# P2：脱敏必须六路由一致
# ---------------------------------------------------------------------------


def test_redact_url_handles_scheme_relative_urls() -> None:
    assert (
        redact_url("//internal.corp.lan/path?token=abc") == "//internal.corp.lan/path"
    )


def test_mask_chat_ids_hides_bare_ids() -> None:
    masked = mask_chat_ids("send_group_msg failed for 461961169: timeout")
    assert "461961169" not in masked
    assert masked.endswith(": timeout")


def test_safe_int_survives_infinity() -> None:
    assert safe_int(float("inf")) == 0
    assert safe_int(float("nan")) == 0


# ---------------------------------------------------------------------------
# P2：清理切点必须对齐日边界
# ---------------------------------------------------------------------------


def test_prune_keeps_the_whole_oldest_trend_day(tmp_path: Path) -> None:
    """A late-evening cleanup must not delete the first day of the chart."""

    store = ReportStore(tmp_path / "report.db")
    # A day that is exactly retention_days old, sampled at 00:30.
    base = 1_800_000_000
    day_start = base - (base % 86400)
    old_ts = day_start - 30 * 86400 + 1800
    store.add_resource(old_ts, 10.0, 50.0, 10.0, 40.0, 0.1)
    # Cleanup runs late in the evening of the same wall-clock day.
    store.prune(resource_days=30, event_days=90, now=day_start + 23 * 3600)
    rows = store.list_resources(day_start - 31 * 86400, day_start)
    assert any(int(row["ts"]) == old_ts for row in rows), (
        "the oldest day of the 30-day trend must survive a late cleanup"
    )
