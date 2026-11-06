"""Regression tests for the v2.20.19 fix batch.

Each test drives the production code path that changed: the provider alert
state machine, the aggregation-delivered callback on both alert engines, the
report store self-heal, report rendering (dirty token values, cross-window
markers), HTTP/TCP probe classification, the audit clamp and the test
notification result.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import psutil
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import PROBE_ERROR_PREFIX, ServiceMonitor
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.sampler import Sampler
from astrbot_plugin_statuscard.state import StateStore

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


# ----------------------------------------------------------------------
# 1) A queued provider recovery must not be sent inside a NEW outage
# ----------------------------------------------------------------------


def _provider_engine(tmp_path, plugin) -> AlertEngine:
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "provider_watch": {
                "enabled": True,
                "threshold": 3,
                "cooldown_seconds": 300,
            },
        },
    }
    return AlertEngine(
        plugin,
        StateStore(tmp_path / "state.json"),
        config,
        report_store=ReportStore(tmp_path / "report.db"),
    )


def test_provider_new_outage_folds_the_queued_recovery(tmp_path, monkeypatch) -> None:
    plugin = ScriptedPlugin()
    engine = _provider_engine(tmp_path, plugin)
    now = time.time()
    engine.state.set(
        "alerts",
        {
            "provider_watch": {
                "alerting": True,
                "alerted": False,
                "last_streak": 5,
                "last_alert_ts": now - 1000,
                "pending_recovery": "✅ 已恢复 · LLM 服务\n（旧文案）",
                "recovery_retry_count": 1,
                # Not due yet: the retry block must be bypassed so the new
                # alert is what clears the stale notice.
                "recovery_next_attempt": now + 900,
            }
        },
    )
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (6, "error", 20),
    )

    asyncio.run(engine._check_provider_quality())

    watch = (engine.state.get("alerts", {}) or {}).get("provider_watch", {})
    assert len(plugin.calls) == 1
    assert "已并入本条" in plugin.calls[0]
    assert "已恢复" not in plugin.calls[0]
    assert not watch.get("pending_recovery")
    assert not watch.get("recovery_next_attempt")

    # Even when the old retry deadline passes, no stale recovery goes out.
    watch["recovery_next_attempt"] = now - 1
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert all("已恢复" not in text for text in plugin.calls)


# ----------------------------------------------------------------------
# 2) A delivered summary clears "recovery_awaits_summary"
# ----------------------------------------------------------------------


def test_summary_delivered_clears_the_recovery_marker(tmp_path) -> None:
    engine = AlertEngine(
        ScriptedPlugin(),
        StateStore(tmp_path / "state.json"),
        {"alerts": {"enabled": True, "targets": ["123456"]}},
    )
    engine.state.set(
        "alerts",
        {
            "rules": {
                "rule-1": {
                    "label": "CPU",
                    "alerting": False,
                    "recovery_awaits_summary": True,
                    "pending_recovery": "✅ 已恢复 · CPU",
                }
            }
        },
    )

    engine._on_summary_delivered([("CPU", "CPU 95%")])

    rule = (engine.state.get("alerts", {}) or {}).get("rules", {}).get("rule-1", {})
    assert not rule.get("recovery_awaits_summary")
    # The queued recovery itself must stay: the summary did announce the
    # alert, so the recovery is still owed to the user.
    assert rule.get("pending_recovery")


def test_monitor_summary_delivered_clears_the_recovery_marker(tmp_path) -> None:
    monitor = ServiceMonitor(
        ScriptedPlugin(),
        StateStore(tmp_path / "state.json"),
        {"monitors": {"enabled": True, "targets": ["123456"]}},
    )
    monitor.state.set(
        "monitors",
        {
            "items": {
                "key-1": {
                    "name": "站点A",
                    "alerting": False,
                    "recovery_awaits_summary": True,
                    "pending_recovery": "✅ 服务恢复 · 站点A",
                }
            }
        },
    )

    monitor._on_summary_delivered([("站点A", "连接失败")])

    item = (monitor.state.get("monitors", {}) or {}).get("items", {}).get("key-1", {})
    assert not item.get("recovery_awaits_summary")
    assert item.get("pending_recovery")


# ----------------------------------------------------------------------
# 3) A failed probe must repair the store for the next attempt
# ----------------------------------------------------------------------


def test_store_ready_repairs_after_a_failed_probe(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    reporter = object.__new__(DailyReporter)
    reporter.store = store

    for suffix in ("", "-wal", "-shm"):
        path = store.path.with_name(store.path.name + suffix)
        if path.exists():
            path.unlink()

    assert reporter._store_ready() is False
    # ensure_ready() rebuilt the schema, so the next scheduled attempt works.
    assert store.available is True
    assert reporter._store_ready() is True


# ----------------------------------------------------------------------
# 4) A dirty token column must not kill the whole report
# ----------------------------------------------------------------------


def test_token_section_ignores_dirty_token_values() -> None:
    builder = object.__new__(DailyReportBuilder)
    builder.tz = TZ
    builder._provider_rows = lambda start, end: (  # type: ignore[method-assign]
        [
            ("2026-11-02 12:00:00", "completed", "m", "oops", 10, 5),
            ("2026-11-02 13:00:00", "completed", "m", 100, 20, 3),
        ],
        None,
        True,
        False,
    )

    section = builder._token_section(date(2026, 11, 2), 7)

    assert section["available"] is True
    # The dirty row contributes 0 tokens but still counts as a call.
    assert section["calls"] == 2
    assert section["total"] == 138


# ----------------------------------------------------------------------
# 5) A cross-window alert row carries the "started before" marker
# ----------------------------------------------------------------------


def test_cross_window_alert_row_has_the_started_before_marker(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    base = datetime(2026, 11, 2, tzinfo=TZ)
    start = int((base - timedelta(minutes=10)).timestamp())  # 11-01 23:50
    inside = int((base + timedelta(minutes=10)).timestamp())  # 00:10
    end = int((base + timedelta(minutes=40)).timestamp())  # 00:40
    store.add_event(
        start, "2026-11-01", "resource", "CPU", "95%", "alert", resolved_ts=end
    )
    store.add_event(inside, "2026-11-02", "resource", "CPU", "96%", "alert")
    store.add_event(end, "2026-11-02", "resource", "CPU", "40%", "recovery")

    builder = DailyReportBuilder(
        store, {"display.timezone": "Asia/Shanghai", "display.redact": []}
    )
    built = builder.build(day=date(2026, 11, 2), span_days=1)
    rows = [
        item
        for item in (built.get("anomalies") or {}).get("items", [])
        if item.get("name") == "CPU"
    ]

    assert rows
    assert rows[0]["duration"].endswith("（窗口前开始）")
    assert rows[0]["duration_code"] == "started_before_window"


# ----------------------------------------------------------------------
# 6) HTTP keyword past max_response_bytes: keep reading, do not report a
#    permanent "unknown" (v2.20.23). Only the hard 10 MiB ceiling is an
#    inconclusive probe.
# ----------------------------------------------------------------------


def test_keyword_probe_reads_past_the_soft_limit() -> None:
    async def scenario() -> tuple[bool, str, str]:
        async def handle(reader, writer):
            await reader.read(1024)
            body = b"<html>" + b"x" * 4096 + b"NEEDLE</html>"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monitor = ServiceMonitor(
            ScriptedPlugin(),
            MemoryState(),
            {
                "display": {"timezone": "Asia/Shanghai"},
                "monitors": {
                    "enabled": True,
                    "max_response_bytes": 1024,
                    "targets": ["123456"],
                },
            },
        )
        item = {
            "__template_key": "http",
            "kind": "http",
            "name": "big",
            "url": f"http://127.0.0.1:{port}/",
            "keyword": "NEEDLE",
            "timeout_seconds": 2,
            "expected_status": 0,
            "verify_tls": True,
        }
        try:
            return await monitor._probe(item)
        finally:
            server.close()
            await server.wait_closed()

    ok, detail, _target = asyncio.run(scenario())
    assert ok is True
    assert "200" in detail


def test_keyword_probe_beyond_hard_limit_is_a_probe_error() -> None:
    """Only the 10 MiB hard ceiling stays inconclusive (no keyword there)."""

    async def scenario() -> tuple[bool, str, str]:
        async def handle(reader, writer):
            await reader.read(1024)
            body = b"<html>" + b"x" * (10 * 1024 * 1024) + b"</html>"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monitor = ServiceMonitor(
            ScriptedPlugin(),
            MemoryState(),
            {
                "display": {"timezone": "Asia/Shanghai"},
                "monitors": {
                    "enabled": True,
                    "max_response_bytes": 1024,
                    "targets": ["123456"],
                },
            },
        )
        item = {
            "__template_key": "http",
            "kind": "http",
            "name": "huge",
            "url": f"http://127.0.0.1:{port}/",
            "keyword": "NEEDLE",
            "timeout_seconds": 15,
            "expected_status": 0,
            "verify_tls": True,
        }
        try:
            return await monitor._probe(item)
        finally:
            server.close()
            await server.wait_closed()

    ok, detail, _target = asyncio.run(scenario())
    assert ok is False
    assert detail.startswith(PROBE_ERROR_PREFIX)
    assert "无法确认关键字" in detail


# ----------------------------------------------------------------------
# 7) Audit compares the clamped runtime values
# ----------------------------------------------------------------------


def test_audit_does_not_warn_when_runtime_clamps_retention() -> None:
    issues = run_audit(
        {
            "daily_report": {
                "enabled": True,
                "history_days": 7,
                "retention_days": 3,
                "sections": ["resource"],
            }
        }
    )
    assert not any(issue.code == "history_exceeds_retention" for issue in issues)


# ----------------------------------------------------------------------
# 8) Test notification reports partial delivery per target
# ----------------------------------------------------------------------


def test_send_test_returns_per_target_results(tmp_path) -> None:
    plugin = ScriptedPlugin(
        [
            DeliveryResult(
                ok=False,
                ok_targets=["default:GroupMessage:10001"],
                failed_targets=["default:GroupMessage:20002"],
            )
        ]
    )
    engine = AlertEngine(
        plugin,
        StateStore(tmp_path / "state.json"),
        {
            "alerts": {
                "enabled": True,
                "targets": ["default:GroupMessage:10001"],
                "platform_id": "default",
            }
        },
    )

    result = asyncio.run(engine.send_test("default:GroupMessage:10001"))

    assert result["ok"] is False
    assert result["ok_targets"] == ["default:GroupMessage:10001"]
    assert result["failed_targets"] == ["default:GroupMessage:20002"]


# ----------------------------------------------------------------------
# 9) A down interface must not keep publishing the last rate
# ----------------------------------------------------------------------


def test_down_interface_zeroes_the_previous_rate(tmp_path, monkeypatch) -> None:
    state = StateStore(tmp_path / "state.json")
    now = time.time()
    state.set(
        "net",
        {
            "interface": "eth0",
            "up_rate": 9.8,
            "down_rate": 19.5,
            "last_sent": 100,
            "last_recv": 200,
            "last_ts": now - 60,
            "updated_at": now - 60,
            "month": "2026-11",
            "month_up": 0,
            "month_down": 0,
            "history": [],
        },
    )

    def fake_counters(pernic=False):
        if pernic:
            return {
                "eth0": SimpleNamespace(
                    isup=False, bytes_sent=100, bytes_recv=200, speed=1000
                )
            }
        return SimpleNamespace(bytes_sent=100, bytes_recv=200)

    monkeypatch.setattr(psutil, "net_io_counters", fake_counters)
    sampler = Sampler(state, 60, config={"display": {"timezone": "Asia/Shanghai"}})

    sampler._sample_network(now)

    net = state.get("net") or {}
    assert net.get("up_rate") == 0.0
    assert net.get("down_rate") == 0.0
    assert net.get("updated_at") == now


# ----------------------------------------------------------------------
# 10) Timeout exceptions get a readable reason, not "TimeoutError: "
# ----------------------------------------------------------------------


def test_tcp_timeout_has_a_readable_reason(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "open_connection", boom)
    monitor = ServiceMonitor(
        ScriptedPlugin(),
        MemoryState(),
        {
            "display": {"timezone": "Asia/Shanghai"},
            "monitors": {"enabled": True, "targets": ["123456"]},
        },
    )
    item = {
        "__template_key": "tcp",
        "kind": "tcp",
        "name": "port",
        "host": "127.0.0.1",
        "port": 9,
        "timeout_seconds": 1,
    }

    ok, detail, _target = asyncio.run(monitor._probe(item))

    assert ok is False
    assert detail == "探测超时（超过 1 秒）"


# ----------------------------------------------------------------------
# 11) A delivered summary only clears the matching same-named rule
# ----------------------------------------------------------------------


def test_summary_delivered_matches_name_and_reason(tmp_path) -> None:
    engine = AlertEngine(
        ScriptedPlugin(),
        StateStore(tmp_path / "state.json"),
        {"alerts": {"enabled": True, "targets": ["123456"]}},
    )
    engine.state.set(
        "alerts",
        {
            "rules": {
                "a": {
                    "label": "站点",
                    "alerting": False,
                    "recovery_awaits_summary": True,
                    "pending_recovery": "A",
                    "summary_reason": "HTTP 503",
                },
                "b": {
                    "label": "站点",
                    "alerting": False,
                    "recovery_awaits_summary": True,
                    "pending_recovery": "B",
                    "summary_reason": "连接超时",
                },
            }
        },
    )

    engine._on_summary_delivered([("站点", "HTTP 503")])

    rules = (engine.state.get("alerts", {}) or {}).get("rules", {})
    assert not rules["a"].get("recovery_awaits_summary")
    # B's own summary has not been delivered; its recovery must keep waiting.
    assert rules["b"].get("recovery_awaits_summary")
    assert rules["b"].get("pending_recovery")


# ----------------------------------------------------------------------
# 12) An aborted newest call keeps a recent pending recovery, then expires
# ----------------------------------------------------------------------


def test_provider_pending_recovery_survives_aborted_and_expires(
    tmp_path, monkeypatch
) -> None:
    plugin = ScriptedPlugin()
    engine = _provider_engine(tmp_path, plugin)
    now = time.time()
    engine.state.set(
        "alerts",
        {
            "provider_watch": {
                "alerting": False,
                "pending_recovery": "✅ 已恢复 · LLM 服务",
                "recovery_retry_count": 1,
                "recovery_next_attempt": now - 60,
            }
        },
    )
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (0, "aborted", 1),
    )

    asyncio.run(engine._check_provider_quality())
    watch = (engine.state.get("alerts", {}) or {}).get("provider_watch", {})
    assert watch.get("pending_recovery")

    watch["recovery_next_attempt"] = now - 25 * 3600
    engine._provider_last_check = 0
    asyncio.run(engine._check_provider_quality())
    assert not watch.get("pending_recovery")


# ----------------------------------------------------------------------
# 13) A report window that contains a DB rebuild is skipped
# ----------------------------------------------------------------------


def test_report_window_lost_after_db_rebuild() -> None:
    reporter = object.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._db_repaired_at = datetime(2026, 11, 4, 0, 5, tzinfo=TZ).timestamp()

    assert reporter._report_window_lost("2026-11-04") is True
    # A week ending on the repair day also contains lost samples.
    assert reporter._report_window_lost("2026-11-04", 7) is True
    # The following day is entirely after the rebuild and must send.
    assert reporter._report_window_lost("2026-11-05") is False


def test_reporter_detects_db_deleted_while_stopped(tmp_path) -> None:
    """A recreated DB is a loss when the delivery history says we ran before."""

    state = MemoryState()
    state.data = {"report_delivery": {"schema": 2, "last_attempt_day": "2026-11-02"}}
    plugin = ScriptedPlugin()
    plugin.state = state  # type: ignore[attr-defined]
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"enabled": True, "targets": []},
    }

    first = ReportStore(tmp_path / "report.db")
    first.add_resource(int(time.time()) - 3600, cpu=42.0)
    for suffix in ("", "-wal", "-shm"):
        path = first.path.with_name(first.path.name + suffix)
        if path.exists():
            path.unlink()

    recreated = ReportStore(tmp_path / "report.db")
    assert recreated.created_new is True
    reporter = DailyReporter(plugin, recreated, config)

    assert reporter._db_repaired_at
    assert reporter._report_window_lost("2026-11-03") is True


# ----------------------------------------------------------------------
# 14) Windows DNS: only "host not found" is a config error
# ----------------------------------------------------------------------


def test_windows_dns_config_error_classification() -> None:
    from astrbot_plugin_statuscard.monitor import _looks_like_config_error

    assert _looks_like_config_error("gaierror: [Errno 11001] getaddrinfo failed")
    # WSATRY_AGAIN is a temporary resolver failure, not a typo.
    assert not _looks_like_config_error("gaierror: [Errno 11002] getaddrinfo failed")
