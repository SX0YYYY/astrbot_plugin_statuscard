"""Regression tests for the single v2.14.0 reliability/quality batch."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest
from astrbot_plugin_statuscard import collectors, version
from astrbot_plugin_statuscard import report as report_module
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.bg_provider import _detect_image_mime
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder

TARGET = "default:GroupMessage:123456789"
SECOND = "default:GroupMessage:2"


def _report_config() -> dict:
    return {
        "daily_report": {
            "enabled": True,
            "targets": [TARGET],
            "sections": ["resource"],
            "with_image": True,
        }
    }


def _sample_view() -> dict:
    return {"day": "2026-10-26", "sections": ["resource"]}


class _ResultPlugin:
    def __init__(self, result: DeliveryResult) -> None:
        self.result = result
        self.images: list[tuple[list[str], str, str]] = []
        self.texts: list[tuple[list[str], str, str]] = []

    async def render_report_image(self, view: dict) -> str:
        return "/tmp/report.png"

    async def send_image(
        self, targets: list[str], url: str, source: str = "unknown"
    ) -> DeliveryResult:
        self.images.append((list(targets), url, source))
        return self.result

    async def send_text(
        self, targets: list[str], text: str, source: str = "unknown"
    ) -> DeliveryResult:
        self.texts.append((list(targets), text, source))
        return self.result


def test_partial_delivery_is_marked_in_report_runs(tmp_path: Path) -> None:
    result = DeliveryResult(
        ok=True,
        ok_targets=[TARGET],
        failed_targets=[SECOND],
        uncertain_targets=[SECOND],
    )
    assert result.partial is True
    assert result.as_state()["partial"] is True

    store = ReportStore(tmp_path / "report.db")
    plugin = _ResultPlugin(result)
    reporter = DailyReporter(plugin, store, _report_config())
    reporter._build_view = lambda day=None: _sample_view()

    asyncio.run(reporter._send_scheduled())

    runs = store.list_report_runs(limit=5)
    assert runs[0]["detail"] == "image (partial)"


def test_db_unavailable_skips_the_report(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.available = False
    plugin = _ResultPlugin(DeliveryResult(ok=True, ok_targets=[TARGET]))
    reporter = DailyReporter(plugin, store, _report_config())
    reporter._build_view = lambda day=None: _sample_view()

    asyncio.run(reporter._send_scheduled())

    assert plugin.images == []
    assert [item[2] for item in plugin.texts] == ["report_db_unavailable"]
    # report_runs lives in the same unavailable database, so the skip cannot
    # be persisted there; the operator notice and the error log are the record.
    assert store.list_report_runs(limit=5) == []


def test_cross_day_retry_keeps_report_day(tmp_path: Path) -> None:
    """A text retry that crosses midnight keeps the pinned report day.

    It used to be abandoned with a "cross-day" row even though the image path
    had already been fixed to send the original day; the operator lost the
    whole report for a one-off failure at 23:5x.
    """

    store = ReportStore(tmp_path / "report.db")
    reporter = DailyReporter(
        _ResultPlugin(DeliveryResult(ok=True, ok_targets=[TARGET])),
        store,
        _report_config(),
    )
    reporter._running = True
    reporter._pending_retry = ("2026-10-26", 0)
    reporter._pending_retry_day = "2000-01-01"
    calls: list[object] = []

    async def fake_send(day=None):
        calls.append(day)

    reporter._send_scheduled = fake_send

    with (
        mock.patch.object(
            report_module.asyncio, "sleep", side_effect=asyncio.CancelledError
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(reporter._loop())

    assert calls == ["2026-10-26"]
    runs = store.list_report_runs(limit=5)
    assert all(run["detail"] != "send failed (cross-day)" for run in runs)


def test_resolve_event_close_all(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1000, "2026-10-20", "service", "Nginx", "down", "alert", "x")
    store.add_event(2000, "2026-10-20", "service", "Nginx", "down", "alert", "x")

    assert (
        store.resolve_event(
            3000,
            "Nginx",
            3000,
            kind="service",
            target="x",
            close_all=True,
        )
        is True
    )
    events = store.list_events("2026-10-20", limit=10)
    alerts = [item for item in events if item.get("status") == "alert"]
    assert alerts and all(item.get("resolved_ts") == 3000 for item in alerts)


def test_summary_has_enabled_flags(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-20", sections=["resource"])
    summary = view["summary"]
    assert summary["anomaly_enabled"] is False
    assert summary["token_enabled"] is False
    assert summary["qq_enabled"] is False


def test_anomaly_items_carry_english_slugs(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1000, "2026-10-20", "service", "Nginx", "down", "alert", "x")
    store.add_event(2000, "2026-10-20", "service", "Nginx", "up", "recovery", "x")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day="2026-10-20", sections=["anomaly"])
    items = view["anomalies"]["items"]
    assert items
    assert all(item["kind_slug"] == "service" for item in items)
    assert {item["status_key"] for item in items} == {"alert", "recovery"}


def test_version_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(version, "_version_cache", None)

    def probe() -> str:
        calls["n"] += 1
        return "v4.29.3"

    monkeypatch.setattr(version, "_resolve_from_context", lambda context: probe())
    monkeypatch.setattr(version, "_resolve_from_astrbot_module", lambda: None)
    monkeypatch.setattr(version, "_resolve_from_package_metadata", lambda: None)

    assert version.resolve_astrbot_version(object()) == "v4.29.3"
    assert version.resolve_astrbot_version(object()) == "v4.29.3"
    assert calls["n"] == 1


def test_background_mime_detects_bmp_and_unknown(tmp_path: Path) -> None:
    assert _detect_image_mime(b"BM" + b"\x00" * 30) == "image/bmp"
    assert _detect_image_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10) == "image/png"
    assert _detect_image_mime(b"not an image at all") == "application/octet-stream"


def test_avatar_cache_round_trip(tmp_path: Path) -> None:
    path = collectors._avatar_cache_path(tmp_path, "https://example.invalid/a.png")
    assert path is not None
    collectors._write_avatar_cache(path, b"avatar")
    assert collectors._read_avatar_cache(path, 3600) == b"avatar"
    assert collectors._read_avatar_cache(path, -1) is None
    assert collectors._read_avatar_cache(path, None) == b"avatar"


def test_audit_flags_unknown_redact_rule() -> None:
    issues = run_audit({"display": {"redact": ["hostname", "hostnames"]}})
    codes = {issue.code for issue in issues}
    assert "unknown_redact_rule" in codes


def test_report_loop_survives_uncaught_send_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReportStore(tmp_path / "report.db")
    reporter = DailyReporter(
        _ResultPlugin(DeliveryResult(ok=True, ok_targets=[TARGET])),
        store,
        _report_config(),
    )
    reporter._running = True
    calls = {"n": 0}

    async def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("boom")

    reporter._send_scheduled = boom  # type: ignore[method-assign]
    monkeypatch.setattr(
        report_module,
        "now_in_timezone",
        lambda config: datetime(2026, 10, 20, 20, 59, 59),
    )
    monkeypatch.setattr(reporter, "_time_text", lambda: "21:00")
    sleep = mock.AsyncMock(side_effect=[None, asyncio.CancelledError])
    with (
        mock.patch.object(report_module.asyncio, "sleep", sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(reporter._loop())

    assert calls["n"] == 1
