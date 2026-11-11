"""Regression tests for the v2.20.21 fix batch.

Each test drives the production path that changed: the path-bound disk rule
keys and their one-time migration, the notification-cooldown retry for a
sustained outage, the queued LLM alert merged into its recovery, the
silence-window recovery (one event row per episode), recovery state persisted
before the slow send, the whole-window outage row in the report, the
evidence-free receipt, dirty sample timestamps, the report text truncation,
the fade fingerprint, bare host names in redaction, NaN thresholds in the
audit, range normalisation and the independent provider-watch loop.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard.alerts import AlertEngine, _disk_path_from_label
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.onebot_receipt import extract_receipt
from astrbot_plugin_statuscard.page_api import _normalize_range
from astrbot_plugin_statuscard.redactor import Redactor, normalize_rules
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.t2i_renderer import _fade_fingerprint

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
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = None
        self.state = None
        self.saves_at_send: list[int] = []

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append(text)
        if self.state is not None:
            self.saves_at_send.append(self.state.saves)
        return DeliveryResult(ok=True, ok_targets=list(targets))


def _make_engine(tmp_path, config=None, plugin=None):
    state = MemoryState()
    plugin = plugin or ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {"alerts": {"targets": ["default:GroupMessage:1"]}}
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    engine = AlertEngine(plugin, state, cfg, store)
    return engine, state, store, plugin


# ----------------------------------------------------------------------
# A1: disk rules are bound to the mount path, not to the list position
# ----------------------------------------------------------------------


def test_disk_path_label_round_trip() -> None:
    assert _disk_path_from_label("磁盘使用率 (/mnt/a)") == "/mnt/a"
    assert _disk_path_from_label("磁盘使用率") == ""
    assert _disk_path_from_label(None) == ""


def test_disk_key_migration_does_not_move_latch_to_another_mount(
    tmp_path, monkeypatch
) -> None:
    config = {
        "alerts": {"enabled": True, "sustain_samples": 1},
        "display": {"disk_paths": ["/mnt/b"]},
    }
    engine, state, _store, _plugin = _make_engine(tmp_path, config)
    now = time.time()
    state.setdefault("alerts", {}).setdefault("rules", {})["disk"] = {
        "label": "磁盘使用率 (/mnt/a)",
        "alerting": True,
        "alerted": True,
        "alert_since": now - 100,
        "count": 0,
        "last_seen": now - 5,
    }
    monkeypatch.setattr(alerts_module, "cached_cpu_percent", lambda: 1.0)
    monkeypatch.setattr(
        alerts_module,
        "memory_stat",
        lambda: SimpleNamespace(percent=1.0, available=1, used=1, total=1),
    )
    monkeypatch.setattr(
        alerts_module,
        "swap_stat",
        lambda: SimpleNamespace(percent=1.0, used=1, total=1),
    )
    monkeypatch.setattr(
        alerts_module,
        "disk_usage_bounded",
        lambda paths: [SimpleNamespace(percent=5.0, used=1, total=20, exception="")],
    )

    asyncio.run(engine._sample_once())

    rules = state.get("alerts")["rules"]
    # The historical position-bound key is gone...
    assert "disk" not in rules
    # ...the new mount has its own fresh rule (not the old mount's latch)...
    assert "disk:/mnt/b" in rules
    assert rules["disk:/mnt/b"].get("alerting") is not True
    # ...and the old mount's episode was closed instead of being re-labelled.
    assert "disk:/mnt/a" not in rules


# ----------------------------------------------------------------------
# A2: a sustained outage that began inside the cooldown is announced later
# ----------------------------------------------------------------------


class _OfflineClient:
    async def call_action(self, action, **kwargs):
        return {"online": False}


def test_offline_alert_retries_after_cooldown(tmp_path, monkeypatch) -> None:
    engine, state, _store, plugin = _make_engine(tmp_path)
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: _OfflineClient()
    )
    now = time.time()
    state.setdefault("alerts", {})["offline"] = {
        "alerting": True,
        "alerted": False,
        "since": now - 7200,
        "last_sent": now - 3600,
        "count": 0,
    }

    asyncio.run(engine._check_offline())

    st = state.get("alerts")["offline"]
    assert st["alerted"] is True
    assert st["last_sent"] >= now
    assert any("冷却结束后的补发" in text for text in plugin.calls)


# ----------------------------------------------------------------------
# A3: a silence-window recovery must not add a second alert row
# ----------------------------------------------------------------------


def test_monitor_silence_recovery_writes_one_alert_row(tmp_path) -> None:
    state = MemoryState()
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        ScriptedPlugin(), state, {"monitors": {"silence_enabled": True}}, store, None
    )
    monitor._is_silenced = lambda: True  # type: ignore[assignment]
    item = {"__template_key": "http", "name": "站点", "url": "http://example.com/"}
    key = monitor._item_key(item)
    now = time.time()
    state.setdefault("monitors", {}).setdefault("items", {})[key] = {
        "alerting": True,
        "alerted": False,
        "since": now - 100,
        "last_error": "HTTP 500",
        "fails": 0,
        "last_sent": now - 100,
    }

    async def probe(one):
        return True, "HTTP 200", one["url"]

    monitor._probe = probe  # type: ignore[assignment]
    asyncio.run(monitor._check_item(item))

    # ``_record_event`` stamps the day with the CONFIGURED timezone, not the
    # runner's UTC date. Hard-coding "2026-11-04" passed locally at 23:xx
    # Asia/Shanghai but failed in CI, where 16:15 UTC is already 11-05 there
    # (the project's recurring UTC-vs-config-timezone trap).
    from astrbot_plugin_statuscard.collectors import now_in_timezone

    today = now_in_timezone({"display": {"timezone": "Asia/Shanghai"}}).strftime(
        "%Y-%m-%d"
    )
    rows = store.list_events(today, limit=50)
    rows = [row for row in rows if row["name"] == "站点"]
    alert_rows = [row for row in rows if row["status"] in {"alert", "failed"}]
    recovery_rows = [row for row in rows if row["status"] == "recovery"]
    assert len(alert_rows) == 0
    assert len(recovery_rows) == 1


# ----------------------------------------------------------------------
# A4: the closed episode reaches the disk before the slow recovery send
# ----------------------------------------------------------------------


def test_resource_recovery_saves_before_send(tmp_path) -> None:
    engine, state, _store, plugin = _make_engine(
        tmp_path, {"alerts": {"sustain_samples": 1}}
    )
    now = time.time()
    state.setdefault("alerts", {}).setdefault("rules", {})["ram"] = {
        "alerting": True,
        "alerted": True,
        "alert_since": now - 60,
        "last_sent": now - 60,
        "count": 0,
    }

    asyncio.run(engine._evaluate("ram", "内存使用率", 10.0, 85.0, "%", "10.0%"))

    assert plugin.calls
    assert plugin.saves_at_send
    assert plugin.saves_at_send[0] >= 1


# ----------------------------------------------------------------------
# A5: a queued LLM alert is delivered together with its recovery
# ----------------------------------------------------------------------


def test_provider_queued_alert_merged_into_recovery(tmp_path, monkeypatch) -> None:
    engine, state, _store, plugin = _make_engine(
        tmp_path, {"alerts": {"provider_watch": {"enabled": True, "threshold": 3}}}
    )
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda *args, **kwargs: ((0, "completed", True, None)),
    )
    now = time.time()
    state.setdefault("alerts", {})["provider_watch"] = {
        "alerting": True,
        "alerted": False,
        "pending_alert_text": "⚠️ LLM 服务连续失败",
        "last_streak": 3,
        "last_alert_ts": now - 3600,
        "next_attempt": now + 900,
    }

    asyncio.run(engine._check_provider_quality())

    assert plugin.calls
    assert "LLM 服务连续失败" in plugin.calls[0]
    assert "已恢复" in plugin.calls[0]
    watch = state.get("alerts")["provider_watch"]
    assert watch.get("alerting") is False
    assert not watch.get("pending_alert_text")


# ----------------------------------------------------------------------
# A12: a receipt with no retcode and no message_id is not evidence
# ----------------------------------------------------------------------


def test_receipt_status_only_is_uncertain() -> None:
    receipt = extract_receipt({"status": "ok"})
    assert receipt.ok is False
    assert receipt.attempted is True


# ----------------------------------------------------------------------
# B1: an outage covering the whole report window is visible
# ----------------------------------------------------------------------


def test_report_whole_window_outage_row(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    start = int(datetime(2026, 11, 2, 10, 0, tzinfo=TZ).timestamp())
    end = int(datetime(2026, 11, 4, 9, 0, tzinfo=TZ).timestamp())
    assert store.add_event(
        start, "2026-11-02", "service", "站点", "连接超时", "alert", "http://x"
    )
    assert store.add_event(
        end, "2026-11-04", "service", "站点", "恢复", "recovery", "http://x"
    )
    assert store.resolve_event(
        end, "站点", end, kind="service", target="http://x", close_all=True
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    section = builder._anomaly_section(date(2026, 11, 3), date(2026, 11, 2))

    assert section["count"] == 1
    assert section["available"] is True
    assert section["empty_reason"] is None
    assert any("该窗口内持续异常" in item["detail"] for item in section["items"])


# ----------------------------------------------------------------------
# B1 counter-example: a historical orphan is NOT a whole-window outage
# ----------------------------------------------------------------------


def test_report_historical_orphan_is_not_whole_window(tmp_path) -> None:
    store = ReportStore(tmp_path / "report.db")
    start = int(datetime(2026, 10, 20, 16, 42, tzinfo=TZ).timestamp())
    recovered = int(datetime(2026, 10, 20, 17, 48, tzinfo=TZ).timestamp())
    stamped = int(datetime(2026, 10, 30, 22, 29, tzinfo=TZ).timestamp())
    assert store.add_event(
        start, "2026-10-20", "offline", "Bot 连接", "连接离线（推送失败）", "failed"
    )
    assert store.add_event(
        recovered, "2026-10-20", "offline", "Bot 连接", "离线时长", "recovery"
    )
    # close_all stamps every historical open row of the rule (the pre-v2.20.0
    # orphan shape).
    assert store.resolve_event(
        stamped, "Bot 连接", stamped, kind="offline", close_all=True
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    section = builder._anomaly_section(date(2026, 10, 21), date(2026, 10, 20))

    assert section["count"] == 0
    assert section["items"] == []


def test_report_failed_orphan_does_not_fill_gap_days(tmp_path) -> None:
    """An orphan without ANY recovery event must not become a daily row.

    close_all stamps it with the later real recovery; the gap days before
    that recovery are not covered by the outage and must stay clean.
    """

    store = ReportStore(tmp_path / "report.db")
    orphan = int(datetime(2026, 10, 2, 10, 0, tzinfo=TZ).timestamp())
    real = int(datetime(2026, 10, 20, 9, 0, tzinfo=TZ).timestamp())
    recovered = int(datetime(2026, 10, 21, 1, 0, tzinfo=TZ).timestamp())
    assert store.add_event(
        orphan, "2026-10-02", "offline", "Bot 连接", "连接离线", "failed"
    )
    assert store.add_event(
        real, "2026-10-20", "offline", "Bot 连接", "连接离线", "alert"
    )
    assert store.add_event(
        recovered, "2026-10-21", "offline", "Bot 连接", "恢复", "recovery"
    )
    assert store.resolve_event(
        recovered, "Bot 连接", recovered, kind="offline", close_all=True
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    for day in (date(2026, 10, 5), date(2026, 10, 15), date(2026, 10, 19)):
        section = builder._anomaly_section(day, day - timedelta(days=1))
        assert section["count"] == 0, (day, section["items"])

    # The real outage (started before the window, recovered inside it) still
    # shows up normally.
    section = builder._anomaly_section(date(2026, 10, 21), date(2026, 10, 20))
    assert section["count"] == 1


def test_report_alert_orphan_with_later_alert_is_not_whole_window(tmp_path) -> None:
    """An alert orphan is excluded when a later alert owns the same stamp."""

    store = ReportStore(tmp_path / "report.db")
    orphan = int(datetime(2026, 10, 2, 10, 0, tzinfo=TZ).timestamp())
    real = int(datetime(2026, 10, 20, 9, 0, tzinfo=TZ).timestamp())
    recovered = int(datetime(2026, 10, 21, 1, 0, tzinfo=TZ).timestamp())
    assert store.add_event(
        orphan, "2026-10-02", "offline", "Bot 连接", "连接离线", "alert"
    )
    assert store.add_event(
        real, "2026-10-20", "offline", "Bot 连接", "连接离线", "alert"
    )
    assert store.add_event(
        recovered, "2026-10-21", "offline", "Bot 连接", "恢复", "recovery"
    )
    assert store.resolve_event(
        recovered, "Bot 连接", recovered, kind="offline", close_all=True
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    section = builder._anomaly_section(date(2026, 10, 5), date(2026, 10, 4))

    assert section["count"] == 0
    assert section["items"] == []


def test_report_whole_window_outage_keeps_alert_start(tmp_path) -> None:
    """A failed retry after the alert must not hide the whole-window row."""

    store = ReportStore(tmp_path / "report.db")
    alert_ts = int(datetime(2026, 10, 30, 10, 0, tzinfo=TZ).timestamp())
    failed_ts = int(datetime(2026, 10, 30, 10, 5, tzinfo=TZ).timestamp())
    recovered = int(datetime(2026, 11, 5, 9, 0, tzinfo=TZ).timestamp())
    assert store.add_event(
        alert_ts, "2026-10-30", "offline", "Bot 连接", "连接离线", "alert"
    )
    assert store.add_event(
        failed_ts, "2026-10-30", "offline", "Bot 连接", "连接离线（重试失败）", "failed"
    )
    assert store.add_event(
        recovered, "2026-11-05", "offline", "Bot 连接", "恢复", "recovery"
    )
    assert store.resolve_event(
        recovered, "Bot 连接", recovered, kind="offline", close_all=True
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})

    section = builder._anomaly_section(date(2026, 11, 2), date(2026, 11, 1))

    assert section["count"] == 1
    rows = [item for item in section["items"] if "该窗口内持续异常" in item["detail"]]
    assert len(rows) == 1
    assert rows[0]["time"] == "00:00"


# ----------------------------------------------------------------------
# B5: a non-numeric sample timestamp cannot abort the whole report
# ----------------------------------------------------------------------


def test_first_sample_ts_survives_text_timestamp(tmp_path) -> None:
    path = tmp_path / "report.db"
    store = ReportStore(path)
    assert store.ensure_ready()
    with store._lock:
        con = store._connect()
        con.execute(
            "INSERT INTO process_samples (ts, metric, rank, name, pid, cpu, mem_mb) "
            "VALUES ('oops', 'cpu', 0, 'x', 0, 0, 0)"
        )
        con.commit()
        con.close()
    assert store.first_sample_ts("process_samples") is None


# ----------------------------------------------------------------------
# B9: the text fallback keeps the window/timezone line when truncated
# ----------------------------------------------------------------------


def test_report_text_truncation_keeps_window_line() -> None:
    from astrbot_plugin_statuscard.report_view import build_report_text

    view = {
        "sections": ["resource"],
        "day": "2026-11-03",
        "span_days": 1,
        "window": "2026-11-03 00:00–24:00",
        "timezone": "Asia/Shanghai",
        "window_label": "昨日",
        "resources": {
            "available": True,
            "yesterday": {
                "cpu": {"avg_text": "1" * 600, "peak_text": "2" * 600},
                "ram": {"avg_text": "3" * 600, "peak_text": "4" * 600},
                "swap": {"avg_text": "5" * 600},
                "disk": {"avg_text": "6" * 600},
                "load1": {"avg_text": "7" * 600},
            },
        },
    }
    text = build_report_text(view)
    assert "窗口:" in text
    assert "内容已截断" in text
    assert text.endswith("（Asia/Shanghai）")


# ----------------------------------------------------------------------
# B11: the fade cache distinguishes images sharing length and prefix
# ----------------------------------------------------------------------


def test_fade_fingerprint_uses_whole_image() -> None:
    prefix = b"\x00" * 512
    first = prefix + b"a" * 1024
    second = prefix + b"b" * 1024
    assert len(first) == len(second)
    assert first[:512] == second[:512]
    assert _fade_fingerprint(first, 650) != _fade_fingerprint(second, 650)


# ----------------------------------------------------------------------
# C4: bare dotted host names are masked too
# ----------------------------------------------------------------------


def test_redactor_masks_bare_dotted_hostname() -> None:
    redactor = Redactor(normalize_rules(["hostname"]))
    masked = redactor.mask_address_tokens("监测项 nas.local 连接失败")
    assert "nas.local" not in masked
    assert "已隐藏" in masked


# ----------------------------------------------------------------------
# C10: a NaN threshold is reported instead of silently disabled
# ----------------------------------------------------------------------


def test_audit_reports_nan_threshold() -> None:
    issues = run_audit(
        {
            "alerts": {
                "enabled": True,
                "cpu_threshold": float("nan"),
                "targets": ["default:GroupMessage:1"],
            }
        }
    )
    assert any(issue.code == "alert_threshold_zero" for issue in issues)


# ----------------------------------------------------------------------
# C19: a range with surrounding whitespace is not silently defaulted
# ----------------------------------------------------------------------


def test_normalize_range_strips_whitespace() -> None:
    assert _normalize_range(" 30d ", "24h") == "30d"
    assert _normalize_range(" 7d ", "7d") == "7d"


# ----------------------------------------------------------------------
# D1: enabling only the LLM watch starts its own loop
# ----------------------------------------------------------------------


def test_provider_watch_runs_without_resource_alerts(tmp_path) -> None:
    engine, _state, _store, _plugin = _make_engine(
        tmp_path,
        {
            "alerts": {
                "enabled": False,
                "provider_watch": {"enabled": True, "threshold": 3},
            }
        },
    )

    async def scenario() -> None:
        engine.start()
        assert engine._provider_task is not None
        assert engine._provider_task.done() is False
        await engine.stop()

    asyncio.run(scenario())
