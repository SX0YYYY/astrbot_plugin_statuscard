"""v2.20.5：掉线补发、配置错误分类与回执判定的回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_statuscard.delivery import DeliveryResult, DeliveryService
from astrbot_plugin_statuscard.onebot_receipt import extract_receipt
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore


def _reporter(tmp_path: Path) -> DailyReporter:
    reporter = DailyReporter(
        object(),
        ReportStore(tmp_path / "report.db"),
        # The offline-recovery queue only applies to an enabled report
        # (v2.20.23); the fixture must model the real "enabled" deployment.
        {"display": {}, "daily_report": {"enabled": True}},
    )
    reporter._save_delivery_state = lambda: None  # type: ignore[attr-defined]
    return reporter


def test_schedule_retry_offline_keeps_report_without_budget(tmp_path: Path) -> None:
    """An offline failure must not spend the retry budget or drop the report."""

    reporter = _reporter(tmp_path)
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._daily_given_up_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0

    for _ in range(6):
        assert reporter._schedule_retry("2026-10-30", transport_offline=True) is True
    assert reporter._pending_retry_count == 0
    assert reporter._pending_recovery_day == "2026-10-30"
    assert reporter._daily_given_up_day is None
    assert reporter._pending_retry is not None


def test_offline_recovery_only_keeps_the_latest_report_day(tmp_path: Path) -> None:
    """A multi-day outage must not flush several reports after reconnecting."""

    reporter = _reporter(tmp_path)
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_recovery_day = None
    reporter._pending_recovery_since = 0.0

    for day in ("2026-10-28", "2026-10-29", "2026-10-30"):
        reporter._schedule_retry(day, transport_offline=True)
    assert reporter._pending_recovery_day == "2026-10-30"

    assert reporter.notify_offline_recovery() is True
    assert reporter._pending_retry is not None
    assert reporter._pending_retry[0] == "2026-10-30"
    # Nothing older is queued anywhere.
    assert reporter._pending_recovery_day == "2026-10-30"


def test_notify_offline_recovery_drops_very_old_reports(tmp_path: Path) -> None:
    reporter = _reporter(tmp_path)
    reporter._pending_recovery_day = "2026-10-01"
    reporter._pending_recovery_since = 0.0
    reporter._pending_retry = None
    # ``_pending_recovery_since`` is "now" for a fresh queue; a 0 timestamp
    # would make the age enormous, which is exactly the stale case.
    assert reporter.notify_offline_recovery() is False
    assert reporter._pending_recovery_day is None
    assert reporter._pending_retry is None


def test_transport_offline_flag_on_api_not_available() -> None:
    """A whole-delivery failure caused by ApiNotAvailable is marked offline."""

    class _State:
        def set(self, *a, **k):
            pass

        def save(self):
            pass

    class _Context:
        async def send_message(self, target, chain):
            raise RuntimeError("ApiNotAvailable")

        def __init__(self):
            pass

    import astrbot_plugin_statuscard.delivery as delivery_module

    original = delivery_module.send_direct
    delivery_module.send_direct = lambda *a, **k: _async_not_ok()
    try:
        service = DeliveryService(
            _Context(), _State(), lambda: object(), lambda payload: payload
        )
        result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "x"}))
    finally:
        delivery_module.send_direct = original
    assert result.ok is False
    assert result.transport_offline is True


async def _async_not_ok():
    from astrbot_plugin_statuscard.onebot_receipt import OnebotReceipt

    return OnebotReceipt(ok=False, error="ApiNotAvailable", attempted=False)


def test_extract_receipt_none_is_uncertain() -> None:
    receipt = extract_receipt(None)
    assert receipt.ok is False
    assert receipt.attempted is True


def test_http_probe_config_typo_is_classified() -> None:
    """A URL without a scheme is a configuration error, not an outage."""

    from astrbot_plugin_statuscard.monitor import CONFIG_ERROR_PREFIX

    class _Plugin:
        context = None

    class _State:
        def section(self, *a, **k):
            import contextlib

            return contextlib.nullcontext({})

        def get(self, *a, **k):
            return {}

        def set(self, *a, **k):
            pass

        def save(self):
            pass

    from astrbot_plugin_statuscard.monitor import ServiceMonitor

    monitor = ServiceMonitor(_Plugin(), _State(), {})
    ok, detail, _target = asyncio.run(
        monitor._probe(
            {
                "__template_key": "http",
                "name": "demo",
                "url": "example.com/health",
                "timeout": 3,
            }
        )
    )
    assert ok is False
    assert detail.startswith(CONFIG_ERROR_PREFIX)


def test_delivery_result_state_carries_transport_flag() -> None:
    result = DeliveryResult(ok=False, transport_offline=True)
    assert result.as_state()["transport_offline"] is True


def test_legacy_given_up_marker_is_migrated(tmp_path: Path) -> None:
    """An upgrade must not skip the evening's report because of an old marker.

    Before v2.20.5 ``daily_given_up_day`` held the calendar day of the
    attempt, while the new code compares against the report day. With the
    live state (given_up=10-31, report day=10-31) the catch-up refused and
    the 21:00 run was pushed to the next day.
    """

    from types import SimpleNamespace

    from astrbot_plugin_statuscard.state import StateStore

    state = StateStore(tmp_path / "state.json")
    state.set(
        "report_delivery",
        {
            "last_sent_day": "2026-10-29",
            "last_attempt_day": "2026-10-30",
            "daily_given_up_day": "2026-10-31",
            "pending_retry_day": "2026-10-31",
        },
    )
    state.save()
    reporter = DailyReporter(
        SimpleNamespace(state=state),
        ReportStore(tmp_path / "report.db"),
        {"display": {"timezone": "Asia/Shanghai"}},
    )
    assert reporter._daily_given_up_day == "2026-10-30"
    assert reporter._pending_retry_day is None
    # The old attempt marker names the report day already; it stays.
    assert reporter._last_attempt_day == "2026-10-30"
    # The conversion persisted itself (schema 2): a reload must not shift the
    # marker a second time.
    again = DailyReporter(
        SimpleNamespace(state=state),
        ReportStore(tmp_path / "report2.db"),
        {"display": {"timezone": "Asia/Shanghai"}},
    )
    assert again._daily_given_up_day == "2026-10-30"
    assert state.get("report_delivery", {}).get("schema") == 2
