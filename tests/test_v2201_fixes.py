"""v2.20.1 修复批：投递语义、锁内 await、监测/采集/报表/看板修复的回归测试。

按修复批次组织；每个测试对应第三轮找茬报告中的一条 P1/P2/P3。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from astrbot_plugin_statuscard.delivery import DeliveryResult, DeliveryService
from astrbot_plugin_statuscard.onebot_receipt import OnebotReceipt, extract_receipt

# ---------------------------------------------------------------------------
# 批次 1：投递语义（P1-2 / P2-5）
# ---------------------------------------------------------------------------


def test_onebot_status_failed_is_rejected_not_attempted() -> None:
    receipt = extract_receipt(
        {"status": "failed", "retcode": 100, "message": "blocked"}
    )
    assert receipt.ok is False
    assert receipt.rejected is True
    # Nothing reached the chat: the delivery layer may retry safely.
    assert receipt.attempted is False


def test_onebot_non_failed_retcode_stays_uncertain() -> None:
    receipt = extract_receipt({"status": "ok", "retcode": 1400, "message": "bad"})
    assert receipt.ok is False
    assert receipt.rejected is False
    assert receipt.attempted is True


def test_delivery_result_rejected_is_retryable() -> None:
    result = DeliveryResult(
        ok=False,
        failed_targets=["default:GroupMessage:1"],
        rejected_targets=["default:GroupMessage:1"],
    )
    assert result.retryable is True
    assert result.as_state()["rejected_targets"] == ["default:GroupMessage:1"]


class _StateStub:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def set(self, key: str, value: object) -> None:
        self.data[key] = value

    def save(self) -> None:
        pass


def _service(monkeypatch, fallback):
    from astrbot_plugin_statuscard import delivery as delivery_module

    async def fake_send_direct(client, target, segments):
        return OnebotReceipt(
            ok=False,
            retcode=100,
            error="blocked",
            attempted=False,
            rejected=True,
        )

    monkeypatch.setattr(delivery_module, "send_direct", fake_send_direct)
    monkeypatch.setattr(
        DeliveryService, "_send_via_astrbot", lambda self, target, payload: fallback()
    )
    return DeliveryService(
        None, _StateStub(), lambda: object(), lambda payload: payload
    )


def test_delivery_rejected_falls_back_to_framework(monkeypatch) -> None:
    async def fallback():
        return True, False

    service = _service(monkeypatch, fallback)
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "x"}))
    assert result.ok is True
    assert result.ok_targets == ["default:GroupMessage:1"]
    assert result.failed_targets == []
    assert result.rejected_targets == []


def test_delivery_rejected_and_fallback_failed_is_retryable(monkeypatch) -> None:
    async def fallback():
        return False, False

    service = _service(monkeypatch, fallback)
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "x"}))
    assert result.ok is False
    assert result.failed_targets == ["default:GroupMessage:1"]
    assert result.rejected_targets == ["default:GroupMessage:1"]
    # An explicit rejection is not a "maybe delivered" outcome.
    assert result.uncertain_targets == []
    assert result.retryable is True


def test_test_notification_does_not_overwrite_delivery_real() -> None:
    state = _StateStub()
    service = DeliveryService(None, state, lambda: None, lambda payload: payload)
    service.record(
        "text", ["t"], DeliveryResult(ok=True, ok_targets=["t"]), source="test"
    )
    assert "delivery_real" not in state.data
    service.record(
        "text", ["t"], DeliveryResult(ok=True, ok_targets=["t"]), source="alert"
    )
    assert "delivery_real" in state.data
    assert "delivery" in state.data


# ---------------------------------------------------------------------------
# 批次 2：状态锁内禁止 await（P1-1 死锁的静态回归）
# ---------------------------------------------------------------------------


def test_no_await_inside_state_section() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            is_section = any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "section"
                for item in node.items
            )
            if not is_section:
                continue
            for statement in node.body:
                for child in ast.walk(statement):
                    if isinstance(child, ast.Await):
                        offenders.append(f"{path.name}:{node.lineno}->{child.lineno}")
    assert not offenders, f"await inside state.section: {offenders}"


# ---------------------------------------------------------------------------
# 批次 1 续：provider 关闭时清排队恢复
# ---------------------------------------------------------------------------


class _RecordingPlugin:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown") -> bool:
        self.sent.append(text)
        return True


def test_provider_close_clears_queued_recovery(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.alerts import AlertEngine
    from astrbot_plugin_statuscard.state import StateStore

    state = StateStore(tmp_path / "state.json")
    engine = AlertEngine(
        _RecordingPlugin(),
        state,
        {"alerts": {"enabled": True, "targets": ["1"]}},
    )
    state.set(
        "alerts",
        {
            "provider_watch": {
                "alerting": True,
                "alerted": True,
                "pending_recovery": "stale",
                "recovery_retry_count": 2,
                "send_failed": True,
            }
        },
    )
    engine._close_provider_watch("LLM 服务监控已关闭")
    watch = state.data["alerts"]["provider_watch"]
    assert watch["alerting"] is False
    assert "pending_recovery" not in watch
    assert "recovery_retry_count" not in watch
    assert "send_failed" not in watch


# ---------------------------------------------------------------------------
# 批次 3：监测
# ---------------------------------------------------------------------------


def _monitor(tmp_path: Path, items: list[dict], extra: dict | None = None):
    from astrbot_plugin_statuscard.monitor import ServiceMonitor
    from astrbot_plugin_statuscard.state import StateStore

    state = StateStore(tmp_path / "state.json")
    monitors = {"enabled": True, "items": items, "fail_threshold": 2}
    monitors.update(extra or {})
    return ServiceMonitor(
        plugin=None, state=state, config={"monitors": monitors}
    ), state


def test_probe_timeout_is_not_a_service_failure(tmp_path: Path, monkeypatch) -> None:
    item = {"__template_key": "docker", "name": "容器", "container": "x"}
    monitor, state = _monitor(tmp_path, [item])

    async def fake_probe(_item):
        return False, "探测失败: docker inspect 超过 10s 未返回", "x"

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    asyncio.run(monitor._check_item(item))
    key = monitor._item_key(item)
    st = state.data["monitors"]["items"][key]
    assert st.get("fails", 0) == 0
    assert not st.get("alerting")


def test_silenced_config_error_does_not_push(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    item = {"__template_key": "docker", "name": "容器", "container": "gone"}
    until = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M"
    )
    monitor, state = _monitor(
        tmp_path,
        [item],
        {"silence_enabled": True, "silence_until": until},
    )
    key = monitor._item_key(item)
    state.set(
        "monitors",
        {
            "items": {
                key: {
                    "name": "容器",
                    "target": "gone",
                    "alerting": True,
                    "alerted": True,
                    "since": 1.0,
                }
            }
        },
    )
    sent: list[str] = []

    async def fake_send(text):
        sent.append(text)
        return True

    async def fake_probe(_item):
        return False, "配置错误: No such object: gone", "gone"

    monkeypatch.setattr(monitor, "_send", fake_send)
    monkeypatch.setattr(monitor, "_probe", fake_probe)
    assert monitor._is_silenced() is True
    asyncio.run(monitor._check_item(item))
    assert sent == []
    assert state.data["monitors"]["items"][key]["alerting"] is False


def test_already_alerting_fails_counter_does_not_grow(
    tmp_path: Path, monkeypatch
) -> None:
    item = {"__template_key": "docker", "name": "容器", "container": "x"}
    monitor, state = _monitor(tmp_path, [item])
    key = monitor._item_key(item)
    state.set(
        "monitors",
        {
            "items": {
                key: {
                    "name": "容器",
                    "target": "x",
                    "alerting": True,
                    "alerted": True,
                    "since": 1.0,
                    "fails": 0,
                }
            }
        },
    )

    async def fake_probe(_item):
        return False, "容器 running=False", "x"

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    for _ in range(3):
        asyncio.run(monitor._check_item(item))
    st = state.data["monitors"]["items"][key]
    assert st["fails"] == 0


# ---------------------------------------------------------------------------
# 批次 5：报表
# ---------------------------------------------------------------------------


def _reporter(tmp_path: Path, config: dict | None = None):
    from astrbot_plugin_statuscard import report as report_module
    from astrbot_plugin_statuscard.report_store import ReportStore
    from astrbot_plugin_statuscard.state import StateStore

    class _Plugin:
        pass

    plugin = _Plugin()
    plugin.state = StateStore(tmp_path / "state.json")
    reporter = report_module.DailyReporter(
        plugin, ReportStore(tmp_path / "report.db"), config or {}
    )
    return report_module, reporter


def test_weekly_given_up_week_blocks_catch_up(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    report_module, reporter = _reporter(tmp_path)
    sunday = datetime(2026, 11, 1, 21, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(report_module, "now_in_timezone", lambda config=None: sunday)
    week = reporter._week_key(sunday.date().isoformat())
    reporter._weekly_attempt_week = week
    reporter._weekly_given_up_week = week
    assert reporter._weekly_catch_up_allowed() is False
    reporter._weekly_given_up_week = None
    assert reporter._weekly_catch_up_allowed() is True


def test_empty_weekly_sections_pauses_loop(tmp_path: Path) -> None:
    import time as time_module

    _report_module, reporter = _reporter(
        tmp_path,
        {"daily_report": {"sections": [], "weekly_sections": []}},
    )
    before = time_module.time()
    asyncio.run(reporter._send_weekly_scheduled())
    assert reporter._weekly_pause_until > before
    assert reporter._weekly_empty_recorded_week is not None


def test_corrupt_resource_value_does_not_fail_report_build(tmp_path: Path) -> None:
    import sqlite3
    import time as time_module

    from astrbot_plugin_statuscard.report_store import ReportStore
    from astrbot_plugin_statuscard.report_view import DailyReportBuilder

    db_path = tmp_path / "report.db"
    store = ReportStore(db_path)
    store.add_resource(
        ts=int(time_module.time()) - 86400,
        cpu=10.0,
        ram=50.0,
        swap=10.0,
        disk=40.0,
    )
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE resource_samples SET cpu = 'abc'")
    connection.commit()
    connection.close()
    builder = DailyReportBuilder(store, {"daily_report": {"history_days": 1}})
    view = builder.build(day=None, history_days=1)
    # The point is that one corrupt value no longer aborts the whole build.
    # Whether the (single-sample) day counts as "complete" is a separate rule.
    assert view["day"]
    assert "resources" in view
    assert view["resources"]["available"] is False
    assert view["resources"]["empty_reason"]


# ---------------------------------------------------------------------------
# 批次 4：采集（网络回退不记账）
# ---------------------------------------------------------------------------


def test_all_interface_fallback_skips_month_accounting(
    tmp_path: Path, monkeypatch
) -> None:
    import time as time_module
    from types import SimpleNamespace

    from astrbot_plugin_statuscard import sampler as sampler_module
    from astrbot_plugin_statuscard.sampler import Sampler
    from astrbot_plugin_statuscard.state import StateStore

    store = StateStore(tmp_path / "state.json")
    store.data["net"] = {
        "month": "2026-10",
        "month_up": 100,
        "month_down": 200,
        "last_ts": time_module.time() - 60,
        "last_sent": 1_000_000,
        "last_recv": 2_000_000,
    }
    sampler = Sampler(store, interval_seconds=60, config={"display": {}})
    counters = SimpleNamespace(bytes_sent=99_000_000, bytes_recv=99_000_000)
    # No per-interface counters -> the old code booked the all-interface total
    # (a permanent month-total jump); the new code must skip the round.
    monkeypatch.setattr(
        sampler_module.psutil,
        "net_io_counters",
        lambda pernic=False: {} if pernic else counters,
    )
    monkeypatch.setattr(sampler_module.psutil, "net_if_stats", dict)
    sampler._sample_network(time_module.time())
    net = store.get("net")
    assert net["month_up"] == 100
    assert net["month_down"] == 200
