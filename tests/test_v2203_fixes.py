"""v2.20.3 修复批：第五轮独立找茬（2 P1 + 11 P2）的回归测试。

每条对应一个真实修复点，聚焦「修复前会失败 / 修复后通过」的行为断言。
"""

from __future__ import annotations

import math
from pathlib import Path

from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.collectors import format_bytes
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.state import StateStore

# ---------------------------------------------------------------------------
# 公共桩
# ---------------------------------------------------------------------------


class _PluginStub:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, targets, text, source="alert", kind="resource"):
        self.sent.append((str(targets), str(text)))
        return True


class _Config(dict):
    """Plain dict that also answers cfg_get dotted paths."""

    def _dig(self, path):
        cur = self
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur


def _make_state(tmp_path: Path, payload: dict) -> StateStore:
    store = StateStore(tmp_path / "state.json")
    for key, value in payload.items():
        store.set(key, value)
    return store


def _engine(
    tmp_path: Path, state_payload: dict, config: _Config | None = None
) -> AlertEngine:
    state = _make_state(tmp_path, state_payload)
    plugin = _PluginStub()
    engine = AlertEngine(plugin, state, config or _Config(), None, None)
    engine.targets = ["default:GroupMessage:123456"]
    return engine


# ---------------------------------------------------------------------------
# P1-2：坏 state.json 不再中断 initialize（监测继续启动）
# ---------------------------------------------------------------------------


def test_mark_stale_rules_tolerates_non_numeric_state(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "alerts": {
                "rules": {
                    "cpu": {"alerting": True, "last_seen": "oops"},
                    "ram": {"alerting": True, "alert_since": ["not", "a", "number"]},
                }
            }
        },
    )
    # Before the fix this raised ValueError/TypeError out of alerts.start(),
    # aborting initialize() and leaving monitor.start() never called.
    engine._mark_stale_rules()
    rules = engine.state.get("alerts")["rules"]
    assert "stale" not in rules["cpu"]
    assert "stale" not in rules["ram"]


# ---------------------------------------------------------------------------
# P2-3：资源规则冷却补发失败后仍按重试阶梯重发（不再永久静默）
# ---------------------------------------------------------------------------


def test_resource_cooldown_retry_failure_keeps_retrying(tmp_path: Path) -> None:
    engine = _engine(
        tmp_path,
        {
            "alerts": {
                "rules": {
                    "cpu": {
                        "alerting": True,
                        "alerted": False,
                        "retry_count": 1,
                        "next_attempt": 1.0,  # already due
                        "last_sent": 1000.0,
                        "alert_since": 900.0,
                        "count": 0,
                    }
                }
            }
        },
        _Config({"alerts": {"cooldown_seconds": 1800, "sustain_samples": 1}}),
    )
    engine.cooldown = 1800

    async def _sample_high():
        await engine._evaluate("cpu", "CPU 使用率", 99.0, 85.0, "%", "99.0%")

    import asyncio

    asyncio.run(_sample_high())
    # retry_count>0 while alerting and not alerted must re-enter the send
    # path; the previous code only retried when the outer cooldown_retry was
    # true, which a failed first attempt permanently disabled.
    assert engine.plugin.sent, "cooldown-retry failure must keep retrying"


# ---------------------------------------------------------------------------
# P2-5：Webhook 并发去重（发送前登记 in-flight）
# ---------------------------------------------------------------------------


def test_webhook_inflight_set_initialized() -> None:
    engine = _engine(tmp_path=Path("."), state_payload={})
    assert engine._webhook_inflight == set()


# ---------------------------------------------------------------------------
# P2-1/P2-2 采集防御：format_bytes 对 NaN/Inf 返回 "--"
# ---------------------------------------------------------------------------


def test_format_bytes_nan_inf_returns_dash() -> None:
    assert format_bytes(math.nan) == "--"
    assert format_bytes(math.inf) == "--"
    assert format_bytes(-math.inf) == "--"
    # Ordinary values still render.
    assert format_bytes(2048) == "2.00 KB"


def test_format_bytes_invalid_returns_dash() -> None:
    assert format_bytes("not-a-number") == "--"


# ---------------------------------------------------------------------------
# P2-2 名称脱敏：mask_name 隐藏地址/URL 形态名称
# ---------------------------------------------------------------------------


def test_mask_name_hides_address_when_hostname_rule_on() -> None:
    redactor = Redactor({"hostname"})
    assert redactor.mask_name("https://internal.corp.local:8443/health") == "已隐藏"
    assert redactor.mask_name("10.1.2.3:8443") == "已隐藏"
    # Non-address names pass through untouched.
    assert redactor.mask_name("NapCat 容器") == "NapCat 容器"


def test_mask_name_disabled_without_hostname_rule() -> None:
    redactor = Redactor({"process"})
    assert (
        redactor.mask_name("https://internal.corp.local:8443/health")
        == "https://internal.corp.local:8443/health"
    )


# ---------------------------------------------------------------------------
# P2-1/P2-2 报表：周报 store 不可用时设置 pause（不再 1Hz 空转）
# ---------------------------------------------------------------------------


class _UnavailableStore:
    available = False

    def ensure_ready(self) -> bool:
        return False


def test_weekly_store_unavailable_sets_pause(tmp_path: Path) -> None:
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.store = _UnavailableStore()
    reporter.config = _Config()
    reporter._weekly_pause_until = 0.0
    reporter._weekly_attempt_week = "2026-W44"
    reporter._weekly_empty_recorded_week = None
    reporter._weekly_retry_count = 0
    reporter._weekly_given_up_week = None
    reporter._weekly_last_sent_week = None
    reporter._weekly_pending = None
    reporter._save_delivery_state = lambda: None  # type: ignore[attr-defined]
    sent_notices: list[str] = []

    async def _notify_store_unavailable(targets) -> None:
        sent_notices.append("x")

    reporter._notify_store_unavailable = _notify_store_unavailable  # type: ignore[attr-defined]

    import time as _time

    reporter._today = lambda: "2026-10-26"  # Sunday (weekday 6)
    reporter._week_key = lambda day: "2026-W44"
    reporter._weekly_sections = lambda: ["resources"]  # type: ignore[attr-defined]
    reporter._default_report_day = lambda: "2026-10-25"  # type: ignore[attr-defined]
    reporter._record = lambda *a, **k: None  # type: ignore[attr-defined]
    reporter._clear_retry = lambda *a, **k: None  # type: ignore[attr-defined]
    reporter._targets = lambda kind="weekly": ["default:GroupMessage:1"]  # type: ignore[attr-defined]

    import asyncio

    asyncio.run(reporter._send_weekly_scheduled())
    assert reporter._weekly_pause_until > _time.time(), (
        "store-unavailable weekly branch must back off, not spin at 1Hz"
    )


# ---------------------------------------------------------------------------
# P2-2 报表：_send_scheduled 把 attempt 标记绑定到报告日而非 wall-clock 日
# ---------------------------------------------------------------------------


def test_send_scheduled_scopes_attempt_marker_to_report_day(tmp_path: Path) -> None:
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _Config()
    reporter.store = (
        _UnavailableStore()
    )  # exercises the guard order; build aborts before it
    saved: dict[str, object] = {}
    reporter._save_delivery_state = lambda: saved.update(  # type: ignore[attr-defined]
        {"last_attempt_day": reporter._last_attempt_day}
    )
    reporter._today = lambda: "2026-10-31"  # wall clock says 10-31
    reporter._sections = lambda: None  # type: ignore[attr-defined]
    reporter._targets = lambda kind="daily": []  # type: ignore[attr-defined]
    reporter._default_report_day = lambda: "2026-10-30"  # type: ignore[attr-defined]
    reporter._record = lambda *a, **k: None  # type: ignore[attr-defined]
    reporter._clear_retry = lambda *a, **k: None  # type: ignore[attr-defined]

    import asyncio

    asyncio.run(reporter._send_scheduled(day="2026-10-30"))
    assert saved["last_attempt_day"] == "2026-10-30", (
        "attempt marker must follow the report day, otherwise a cross-midnight "
        "stale retry marks *today* attempted and the fresh report is skipped"
    )
