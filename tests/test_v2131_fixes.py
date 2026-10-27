"""Regression tests for the v2.13.1 P1 fixes.

Covers:
* the delivery result distinguishes "unsent" from "may already be in chat"
* resource alerts/offline detection use that distinction (no duplicate sends)
* recover_notify=false no longer suppresses recovery events / resolved_ts
* the retry budget resets after a recovery instead of leaking across episodes
* a locked database is never renamed as if it were corrupt
* snapshot_ready keeps retrying a complete snapshot with no recv_total
* package counts fall back to the persisted sysinfo cache after a reload
* the CSV export honours the dashboard network redaction
* the events endpoint reports truncation instead of silently hiding rows
* weekly dispatch starts even when the daily report is disabled
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import collectors as collectors_module
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.delivery import DeliveryResult, DeliveryService
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore

# ----------------------------------------------------------------------
# delivery: timeout is uncertain, not a retryable failure
# ----------------------------------------------------------------------


class _TimeoutContext:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, target, chain) -> bool:
        self.calls += 1
        raise TimeoutError()


def test_delivery_timeout_is_uncertain_not_retryable(tmp_path: Path) -> None:
    context = _TimeoutContext()
    service = DeliveryService(
        context=context,
        state=StateStore(tmp_path / "state.json"),
        get_client=lambda: None,
        build_chain=lambda payload: payload,
    )
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "x"}))
    assert result.ok is False
    assert result.uncertain_targets == ["default:GroupMessage:1"]
    assert result.retryable is False
    assert bool(result) is False
    assert context.calls == 1


class _UncertainPlugin:
    async def send_text(self, targets, text, source="unknown"):
        return DeliveryResult(ok=False, uncertain_targets=list(targets))


def test_alert_treats_uncertain_as_delivered(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        }
    }
    engine = AlertEngine(_UncertainPlugin(), state, config)
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["alerting"] is True
    assert rule["retry_count"] == 0
    assert rule["next_attempt"] == 0
    assert rule["last_error"] == ""


# ----------------------------------------------------------------------
# recovery notification preference must not gate the event log
# ----------------------------------------------------------------------


class _OkPlugin:
    async def send_text(self, targets, text, source="unknown") -> bool:
        return True


def test_recover_notify_off_still_writes_event(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
            "recover_notify": False,
        }
    }
    engine = AlertEngine(_OkPlugin(), state, config, report_store=store)
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    asyncio.run(engine._evaluate("cpu", "CPU", 10.0, 85.0, "%", "10.0%"))
    events = store.list_events_range(0, int(time.time()) + 60, limit=20)
    alert_rows = [row for row in events if row["status"] == "alert"]
    recovery_rows = [row for row in events if row["status"] == "recovery"]
    assert len(alert_rows) == 1
    assert len(recovery_rows) == 1
    assert alert_rows[0]["resolved_ts"] is not None


# ----------------------------------------------------------------------
# the retry budget is per episode, not accumulated across recoveries
# ----------------------------------------------------------------------


class _AlwaysFailingPlugin:
    async def send_text(self, targets, text, source="unknown") -> bool:
        return False


def test_retry_budget_resets_after_recovery(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
            "recover_notify": False,
        }
    }
    engine = AlertEngine(_AlwaysFailingPlugin(), state, config)
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["retry_count"] == 1
    rule["next_attempt"] = 0
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    assert rule["retry_count"] == 2

    asyncio.run(engine._evaluate("cpu", "CPU", 10.0, 85.0, "%", "10.0%"))
    assert rule["retry_count"] == 0
    assert not rule.get("alerting")

    rule["next_attempt"] = 0
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    # A new episode starts at 1: the stale count must not make it give up.
    assert rule["retry_count"] == 1
    assert not rule.get("alerting")


# ----------------------------------------------------------------------
# offline detection: "no connection" is not the same as "unknown"
# ----------------------------------------------------------------------


class ApiNotAvailable(Exception):
    pass


class NetworkError(Exception):
    pass


class _OfflinePlugin:
    context = None

    async def send_text(self, targets, text, source="unknown") -> bool:
        return True


def _run_offline(monkeypatch, tmp_path: Path, exc: BaseException):
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        }
    }
    engine = AlertEngine(_OfflinePlugin(), state, config)
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )

    async def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(alerts_module, "_call_action", boom)
    asyncio.run(engine._check_offline())
    return state


def test_api_not_available_counts_as_offline(monkeypatch, tmp_path: Path) -> None:
    state = _run_offline(monkeypatch, tmp_path, ApiNotAvailable())
    offline = state.data["alerts"]["offline"]
    assert offline["alerting"] is True


def test_network_error_stays_unknown(monkeypatch, tmp_path: Path) -> None:
    state = _run_offline(monkeypatch, tmp_path, NetworkError())
    offline = state.data["alerts"]["offline"]
    assert not offline.get("alerting")
    assert offline["unreachable"] == 1


# ----------------------------------------------------------------------
# database recovery must not treat a lock as corruption
# ----------------------------------------------------------------------


def test_locked_database_is_not_renamed(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")

    def boom() -> None:
        raise sqlite3.OperationalError("database is locked")

    store._init_db = boom  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        store._init_db_with_recovery()
    assert not list(tmp_path.glob("report.db.corrupt-*"))


def test_snapshot_ready_requires_recv_total(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.record_snapshot("2026-10-25", 22, 8, 2119, None, True, 1)
    assert store.snapshot_ready("2026-10-25") is False
    assert store.record_snapshot("2026-10-25", 22, 8, 2119, 50000, False, 2)
    assert store.snapshot_ready("2026-10-25") is True


# ----------------------------------------------------------------------
# sysinfo cache: persisted values must survive a reload
# ----------------------------------------------------------------------


def test_package_counts_fall_back_to_persisted(monkeypatch) -> None:
    saved = collectors_module._pkg_cache
    collectors_module._pkg_cache = {}
    state = SimpleNamespace(
        get=lambda key, default=None: {
            "ts": time.time(),
            "dpkg": 100,
            "pip": 50,
        }
    )
    try:
        assert collectors_module._package_counts(state) == (100, 50)
    finally:
        collectors_module._pkg_cache = saved


# ----------------------------------------------------------------------
# dashboard: export redaction, event truncation, timezone
# ----------------------------------------------------------------------


def test_export_resources_honours_network_redact(tmp_path: Path, monkeypatch) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = int(time.time())
    assert store.add_resource(now, cpu=1.0, net_up=2.0, net_down=3.0)
    plugin = SimpleNamespace(
        config={"display": {"page_redact": ["network"]}},
        report_store=store,
    )
    api = page_api_module.PageApi(plugin)
    monkeypatch.setattr(
        page_api_module,
        "_request",
        SimpleNamespace(query={"kind": "resources", "range": "24h"}),
    )
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)
    payload = asyncio.run(api.export())
    assert payload["ok"] is True
    row = payload["rows"][0]
    assert row["net_up"] is None
    assert row["net_down"] is None
    assert row["cpu"] == 1.0


def test_events_reports_truncation(tmp_path: Path, monkeypatch) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = int(time.time())
    day = date.today().isoformat()
    for index in range(201):
        assert store.add_event(
            now - index, day, "service", f"n{index}", "detail", "alert"
        )
    plugin = SimpleNamespace(config={"display": {}}, report_store=store)
    api = page_api_module.PageApi(plugin)
    monkeypatch.setattr(
        page_api_module, "_request", SimpleNamespace(query={"days": "7"})
    )
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)
    payload = asyncio.run(api.events())
    assert len(payload["items"]) == 200
    assert payload["truncated"] is True
    assert payload["counts"]["alert"] == 201


def test_timezone_name_is_a_valid_iana_key() -> None:
    assert (
        page_api_module._timezone_name({"display": {"timezone": "Asia/Shanghai"}})
        == "Asia/Shanghai"
    )


# ----------------------------------------------------------------------
# weekly dispatch has its own switch
# ----------------------------------------------------------------------


def test_weekly_starts_when_daily_disabled(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    config = {
        "daily_report": {
            "enabled": False,
            "weekly_enabled": True,
            "weekly_time": "21:30",
            "targets": ["123456"],
        }
    }
    reporter = DailyReporter(_OkPlugin(), store, config)

    async def run() -> None:
        reporter.start()
        assert reporter._weekly_task is not None
        await reporter.stop()

    asyncio.run(run())
