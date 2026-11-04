"""Regression tests for the v2.20.20 fix batch.

Each test drives the production code path that changed: the monitor probe
classifier, the "a recovery waits for an undelivered summary" criterion on
both alert engines, the daily-image pending cleanup, address-shaped disk
names in push texts, the stale ``since`` reset, the process-list isolation,
the framework-fallback receipt, the errno guard, the chart timestamp guard,
the cross-multi-day pairing, the snapshot window query, the read-only
database probe, the dirty-state sampler, the notification-kind
normalisation, the shared range normaliser and the T2I retry.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from astrbot_plugin_statuscard import collectors as collectors_module
from astrbot_plugin_statuscard import delivery as delivery_module
from astrbot_plugin_statuscard import main as main_module
from astrbot_plugin_statuscard import monitor as monitor_module
from astrbot_plugin_statuscard import report as report_module
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.collectors import (
    _short_disk_error,
    build_network_chart,
    now_in_timezone,
    process_info,
    resolve_targets,
)
from astrbot_plugin_statuscard.delivery import DeliveryResult, DeliveryService
from astrbot_plugin_statuscard.monitor import (
    CONFIG_ERROR_PREFIX,
    ServiceMonitor,
    _looks_like_config_error,
)
from astrbot_plugin_statuscard.onebot_receipt import OnebotReceipt
from astrbot_plugin_statuscard.page_api import _normalize_range
from astrbot_plugin_statuscard.report import ReportCollector
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder

TZ = timezone(timedelta(hours=8))


class MemoryState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.saves = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def setdefault(self, key, default):
        return self.data.setdefault(key, default)

    @contextmanager
    def section(self, key, default=None):
        section = self.data.setdefault(key, default if default is not None else {})
        if not isinstance(section, dict):
            section = {}
            self.data[key] = section
        yield section

    def save(self) -> None:
        self.saves += 1


class ScriptedPlugin:
    def __init__(self, results: list[DeliveryResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[str] = []

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append(text)
        if self.results:
            return self.results.pop(0)
        return DeliveryResult(ok=True, ok_targets=list(targets))


class PendingAggregator:
    """A summary that is still undelivered while its window has expired."""

    def __init__(self, remaining: float = 0.0, pending: bool = True) -> None:
        self._remaining = remaining
        self._pending = pending

    def window_remaining(self, kind: str) -> float:
        return self._remaining

    def summary_pending(self, kind: str) -> bool:
        return self._pending


def _make_monitor(
    tmp_path,
    config=None,
    aggregator=None,
    plugin=None,
) -> tuple[ServiceMonitor, MemoryState, ReportStore]:
    state = MemoryState()
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        plugin or ScriptedPlugin(),
        state,
        config or {},
        report_store=store,
        aggregator=aggregator,
    )
    return monitor, state, store


# ----------------------------------------------------------------------
# B P1-1: a missing docker/systemctl is a configuration error, not an outage
# ----------------------------------------------------------------------


def test_monitor_missing_docker_binary_is_config_error(monkeypatch, tmp_path) -> None:
    monitor, _, _ = _make_monitor(tmp_path)

    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件。")

    monkeypatch.setattr(monitor_module.subprocess, "run", boom)
    ok, detail, _target = asyncio.run(
        monitor._probe({"__template_key": "docker", "name": "容器", "container": "web"})
    )
    assert ok is False
    assert detail.startswith(CONFIG_ERROR_PREFIX), detail


def test_monitor_missing_systemctl_binary_is_config_error(
    monkeypatch, tmp_path
) -> None:
    monitor, _, _ = _make_monitor(tmp_path)

    def boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: 'systemctl'")

    monkeypatch.setattr(monitor_module.subprocess, "run", boom)
    ok, detail, _target = asyncio.run(
        monitor._probe({"__template_key": "systemd", "name": "服务", "unit": "nginx"})
    )
    assert ok is False
    assert detail.startswith(CONFIG_ERROR_PREFIX), detail


# ----------------------------------------------------------------------
# A/B P2: a recovery must wait while its summary is still undelivered
# ----------------------------------------------------------------------


def test_monitor_recovery_waits_for_undelivered_summary(tmp_path) -> None:
    aggregator = PendingAggregator(remaining=0.0, pending=True)
    monitor, state, _ = _make_monitor(tmp_path, aggregator=aggregator)
    item = {"__template_key": "http", "name": "站点", "url": "http://example.com/"}
    key = monitor._item_key(item)
    now = time.time()
    state.setdefault("monitors", {}).setdefault("items", {})[key] = {
        "alerting": True,
        "alerted": True,
        "since": now - 300,
        "awaiting_summary": True,
        "summary_buffered": True,
        "fails": 0,
        "last_sent": now - 300,
    }

    async def probe(one):
        return True, "HTTP 200", one["url"]

    monitor._probe = probe  # type: ignore[assignment]
    asyncio.run(monitor._check_item(item))
    st = state.get("monitors")["items"][key]
    assert st["pending_recovery"].startswith("✅ 服务恢复")


def test_resource_recovery_waits_for_undelivered_summary(tmp_path) -> None:
    aggregator = PendingAggregator(remaining=0.0, pending=True)
    state = MemoryState()
    store = ReportStore(tmp_path / "report.db")
    engine = AlertEngine(
        ScriptedPlugin(), state, {"alerts": {"sustain_samples": 1}}, store, aggregator
    )
    now = time.time()
    rules = state.setdefault("alerts", {}).setdefault("rules", {})
    rules["disk:x"] = {
        "alerting": True,
        "alerted": True,
        "alert_since": now - 300,
        "awaiting_summary": True,
        "summary_buffered": True,
        "count": 0,
        "last_sent": now - 300,
    }
    asyncio.run(
        engine._evaluate("disk:x", "磁盘使用率 (/data)", 10.0, 85.0, "%", "10.0%")
    )
    st = rules["disk:x"]
    assert st["pending_recovery"].startswith("✅ 已恢复")
    assert st["recovery_awaits_summary"] is True


# ----------------------------------------------------------------------
# A P2-2: the daily image must not spin at 1 Hz on a stale pending record
# ----------------------------------------------------------------------


def test_daily_summary_clears_leftover_pending(tmp_path) -> None:
    state = MemoryState()
    store = ReportStore(tmp_path / "report.db")
    config: dict = {}
    config = {"display": {"timezone": "Asia/Shanghai"}}
    today = now_in_timezone(config).strftime("%Y-%m-%d")
    value = today
    state.setdefault("alerts", {})["daily_summary"] = {
        "last_sent_day": today,
        "pending": {"day": value, "next": time.time() + 600, "queued_at": 0.0},
    }
    engine = AlertEngine(ScriptedPlugin(), state, config, store)
    ok = asyncio.run(engine._send_daily_summary())
    assert ok is True
    assert "pending" not in state.get("alerts")["daily_summary"]


# ----------------------------------------------------------------------
# A P2-3: an address-shaped disk path must be masked in alert texts
# ----------------------------------------------------------------------


def test_resource_alert_masks_address_disk_path(tmp_path) -> None:
    state = MemoryState()
    store = ReportStore(tmp_path / "report.db")
    plugin = ScriptedPlugin()
    config = {
        "display": {"redact": ["hostname"]},
        "alerts": {"sustain_samples": 1, "targets": ["default:GroupMessage:1"]},
    }
    engine = AlertEngine(plugin, state, config, store)
    asyncio.run(
        engine._evaluate(
            "disk://192.168.1.5/share",
            "磁盘使用率 (//192.168.1.5/share)",
            96.0,
            85.0,
            "%",
            "96.0%",
        )
    )
    text = "\n".join(plugin.calls)
    assert "192.168" not in text
    assert "已隐藏" in text


# ----------------------------------------------------------------------
# B P2-1: a stale ``since`` must not survive a healthy sample
# ----------------------------------------------------------------------


def test_monitor_success_clears_stale_since(tmp_path) -> None:
    monitor, state, _ = _make_monitor(tmp_path)
    item = {"__template_key": "http", "name": "站点", "url": "http://example.com/"}
    key = monitor._item_key(item)
    state.setdefault("monitors", {}).setdefault("items", {})[key] = {
        "since": time.time() - 900,
        "fails": 0,
    }

    async def probe(one):
        return True, "HTTP 200", one["url"]

    monitor._probe = probe  # type: ignore[assignment]
    asyncio.run(monitor._check_item(item))
    assert state.get("monitors")["items"][key]["since"] == 0


# ----------------------------------------------------------------------
# B P2-3: a restricted /proc must not degrade the whole card
# ----------------------------------------------------------------------


def test_process_info_survives_process_iter_failure(monkeypatch) -> None:
    monkeypatch.setattr(collectors_module, "_PROC_CPU_READY", True)

    def boom(*args, **kwargs):
        raise PermissionError("restricted /proc")

    monkeypatch.setattr(collectors_module.psutil, "process_iter", boom)
    result = process_info(5)
    assert result["top"] == []
    assert result["counts"]["all"] == 0


# ----------------------------------------------------------------------
# A P3-4: the framework fallback must update the stored receipt
# ----------------------------------------------------------------------


def test_framework_fallback_updates_receipt(monkeypatch) -> None:
    service = DeliveryService(
        context=SimpleNamespace(),
        state=MemoryState(),
        get_client=lambda: object(),
        build_chain=lambda payload: None,
    )
    receipt = OnebotReceipt(
        ok=False,
        retcode=120,
        message_id=None,
        error="refused",
        attempted=True,
        rejected=True,
    )

    async def fake_direct(client, target, segments):
        return receipt

    async def fake_via(target, payload):
        return True, False, False

    monkeypatch.setattr(delivery_module, "send_direct", fake_direct)
    service._send_via_astrbot = fake_via  # type: ignore[assignment]
    result = asyncio.run(
        service.send(["default:GroupMessage:1"], {"text": "x"}, kind="test")
    )
    stored = result.receipts["default:GroupMessage:1"]
    assert stored["ok"] is True
    assert stored["via"] == "framework"


# ----------------------------------------------------------------------
# B P3-1 / B P3-2: errno 20 is not "missing" and a bogus ts is dropped
# ----------------------------------------------------------------------


def test_short_disk_error_does_not_confuse_errno_20() -> None:
    assert _short_disk_error("[Errno 20] Not a directory") != "挂载点不存在"
    assert _short_disk_error("[Errno 2] No such file or directory") == "挂载点不存在"


def test_network_chart_drops_absurd_timestamp() -> None:
    now = time.time()
    chart = build_network_chart(
        [[now - 120, 10.0, 5.0], [now - 60, 10.0, 5.0], [1e30, 10.0, 5.0]]
    )
    assert chart["empty"] is False
    # The absurd point is dropped, so the two real points stay and the
    # padding does not collapse into a vertical line.
    assert len(chart["up_points"].split()) >= 2


# ----------------------------------------------------------------------
# B P3-10: "not found" must not swallow a real service error
# ----------------------------------------------------------------------


def test_config_error_hint_is_narrowed() -> None:
    assert _looks_like_config_error("Unit nginx.service not found.") is True
    assert (
        _looks_like_config_error("Error loading config: not found in /etc/app.conf")
        is False
    )


# ----------------------------------------------------------------------
# C P2-1: a fault longer than the previous day keeps its count/duration
# ----------------------------------------------------------------------


def test_report_cross_multi_day_recovery(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    start = int(datetime(2026, 11, 1, 22, 0, tzinfo=TZ).timestamp())
    end = int(datetime(2026, 11, 3, 8, 0, tzinfo=TZ).timestamp())
    assert store.add_event(start, "2026-11-01", "offline", "Bot 连接", "离线", "failed")
    assert store.add_event(
        end, "2026-11-03", "offline", "Bot 连接", "离线时长", "recovery"
    )
    # The engine stamps every open row with the recovery's timestamp.
    assert store.resolve_event(end, "Bot 连接", end, kind="offline", close_all=True)
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    section = builder._anomaly_section(date(2026, 11, 3), date(2026, 11, 2))
    assert section["count"] == 1
    row = section["items"][0]
    # A recovery row says "恢复"; "已恢复" is the alert row's status.
    assert row["status"] == "恢复"
    assert row["duration"].startswith("1天10:00:00")
    assert "窗口前开始" in row["duration"]


# ----------------------------------------------------------------------
# C P2-2: snapshots follow the requested window, not the newest N rows
# ----------------------------------------------------------------------


def test_list_snapshots_respects_window(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    for offset in range(16):
        day = (date(2026, 10, 20) + timedelta(days=offset)).isoformat()
        assert store.record_snapshot(day, 20, 10, 3000, 1000 + offset, True)
    rows = store.list_snapshots(30, since_day="2026-10-20", until_day="2026-10-22")
    days = [row["day"] for row in rows]
    assert days == ["2026-10-22", "2026-10-21", "2026-10-20"]


# ----------------------------------------------------------------------
# C P3-3: probe() must notice a database that stopped accepting writes
# ----------------------------------------------------------------------


def test_probe_detects_read_only_database(tmp_path) -> None:
    path = tmp_path / "report.db"
    store = ReportStore(path)
    assert store.add_resource(1000, 10.0, 20.0, 1.0, 30.0, 0.5, 0, 0, 0, 0)

    def read_only_connect():
        con = sqlite3.connect(path, timeout=5)
        con.execute("PRAGMA query_only = ON")
        return con

    store._connect = read_only_connect  # type: ignore[assignment]
    assert store.probe() is False


# ----------------------------------------------------------------------
# C P3-4: dirty state values no longer stop the resource sampler
# ----------------------------------------------------------------------


def test_sample_resource_survives_dirty_state(monkeypatch, tmp_path) -> None:
    state = MemoryState()
    state.set("net", {"up_rate": "oops", "down_rate": None})
    state.set("disk_io", "not-a-dict")
    store = ReportStore(tmp_path / "report.db")
    collector = ReportCollector(ScriptedPlugin(), state, {}, store)
    monkeypatch.setattr(report_module, "cached_cpu_percent", lambda: 42.0)
    monkeypatch.setattr(
        report_module, "memory_stat", lambda: SimpleNamespace(percent=50.0)
    )
    monkeypatch.setattr(
        report_module, "swap_stat", lambda: SimpleNamespace(percent=10.0)
    )
    monkeypatch.setattr(report_module, "disk_usage_bounded", lambda paths: [])
    asyncio.run(collector._sample_resource(time.time()))
    rows = store.list_resources(0)
    assert rows and abs(float(rows[-1]["cpu"]) - 42.0) < 0.01


# ----------------------------------------------------------------------
# D P3-6: " resource" in notify.rules must still match at runtime
# ----------------------------------------------------------------------


def test_resolve_targets_normalizes_kinds() -> None:
    config = {
        "alerts": {"platform_id": "default"},
        "notify": {
            "rules": [
                {"enabled": True, "target": "123456", "kinds": [" resource"]},
            ]
        },
    }
    targets = resolve_targets(config, "resource")
    assert targets and "123456" in targets[0]


# ----------------------------------------------------------------------
# D P3-3: /history and /export share one range normaliser
# ----------------------------------------------------------------------


def test_normalize_range_is_shared() -> None:
    assert _normalize_range("90d", "7d") == "30d"
    assert _normalize_range("bogus", "24h") == "24h"
    assert _normalize_range("7D", "24h") == "7d"


# ----------------------------------------------------------------------
# T2I retry: one short retry before giving up
# ----------------------------------------------------------------------


def test_report_render_retries_once_on_t2i_failure(monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_render(html, data, return_url=True, options=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("t2i timeout")
        return "http://img"

    async def fake_save() -> None:
        return None

    fake = SimpleNamespace(
        config={},
        _logical_render_width=lambda: 650,
        html_render=fake_render,
        _diag={},
        _save_diag=fake_save,
    )
    monkeypatch.setattr(main_module, "T2I_RETRY_DELAY_SCHEDULED", 0.0)
    monkeypatch.setattr(
        main_module, "build_report_html", lambda *args, **kwargs: "<html></html>"
    )
    result = asyncio.run(main_module.StatusCardPlugin.render_report_image(fake, {}))
    assert result == "http://img"
    assert calls["n"] == 2


# ----------------------------------------------------------------------
# Reverse audit R1: a masked disk name must not break the aggregation
# callbacks (delivered / abandoned) that match by the buffered rule key.
# ----------------------------------------------------------------------


def _masked_disk_engine(tmp_path):
    state = MemoryState()
    store = ReportStore(tmp_path / "report.db")
    aggregator = AlertAggregator(window_seconds=120)
    plugin = ScriptedPlugin()
    config = {
        "display": {"redact": ["hostname"]},
        "alerts": {"sustain_samples": 1, "targets": ["default:GroupMessage:1"]},
    }
    engine = AlertEngine(plugin, state, config, store, aggregator)
    rules = state.setdefault("alerts", {}).setdefault("rules", {})
    # Rule A opens the aggregation window with its own (plain) alert.
    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))
    # Rule B names an address-shaped disk path and is buffered.
    asyncio.run(
        engine._evaluate(
            "disk://192.168.1.5/share",
            "磁盘使用率 (//192.168.1.5/share)",
            96.0,
            85.0,
            "%",
            "96.0%",
        )
    )
    return state, rules["disk://192.168.1.5/share"], aggregator


def test_masked_disk_alert_delivery_clears_summary_markers(tmp_path) -> None:
    _state, st_b, aggregator = _masked_disk_engine(tmp_path)
    assert st_b.get("summary_buffered") is True
    assert st_b.get("summary_key")

    async def sender(text: str):
        return DeliveryResult(ok=True)

    asyncio.run(aggregator.flush("resource", sender))
    assert "summary_buffered" not in st_b
    assert "awaiting_summary" not in st_b
    assert st_b.get("alerted") is True


def test_masked_disk_alert_abandon_withdraws_alerted(tmp_path) -> None:
    _state, st_b, aggregator = _masked_disk_engine(tmp_path)
    asyncio.run(aggregator.stop())
    assert st_b.get("alerted") is False
    assert st_b.get("summary_abandoned") is True
    assert "summary_buffered" not in st_b
