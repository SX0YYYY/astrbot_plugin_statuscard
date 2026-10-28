"""Regression tests for the v2.13.3 delivery-recording and history batch."""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import onebot_receipt
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore

TARGET = "default:GroupMessage:123456789"


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
    """Minimal plugin double whose sends return a DeliveryResult."""

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


def _uncertain_result(target: str = TARGET) -> DeliveryResult:
    return DeliveryResult(
        ok=False,
        failed_targets=[target],
        uncertain_targets=[target],
        receipts={
            target: {
                "ok": False,
                "retcode": None,
                "message_id": None,
                "error": "TimeoutError",
            }
        },
    )


def test_unconfirmed_image_is_recorded_as_unconfirmed(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _ResultPlugin(_uncertain_result())
    reporter = DailyReporter(plugin, store, _report_config())
    reporter._build_view = lambda day=None: _sample_view()

    asyncio.run(reporter._send_scheduled())

    runs = store.list_report_runs(limit=5)
    assert runs
    assert runs[0]["detail"] == "image unconfirmed"
    # The message may already be in the chat; a text copy would duplicate it.
    assert plugin.texts == []
    assert reporter._last_sent_day == "2026-10-26"


def test_unconfirmed_text_is_recorded_as_unconfirmed(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _ResultPlugin(_uncertain_result())
    config = _report_config()
    config["daily_report"]["with_image"] = False
    reporter = DailyReporter(plugin, store, config)
    reporter._build_view = lambda day=None: _sample_view()

    asyncio.run(reporter._send_scheduled())

    runs = store.list_report_runs(limit=5)
    assert runs
    assert runs[0]["detail"] == "text unconfirmed"
    assert reporter._last_sent_day == "2026-10-26"


def test_v2133_direct_send_timeout_is_longer() -> None:
    assert onebot_receipt.DIRECT_SEND_TIMEOUT == 20.0
    default = (
        inspect.signature(onebot_receipt.send_direct).parameters["timeout"].default
    )
    assert default == onebot_receipt.DIRECT_SEND_TIMEOUT


def _page_api(tmp_path: Path, config: dict) -> page_api_module.PageApi:
    plugin = SimpleNamespace(
        config=config,
        report_store=ReportStore(tmp_path / "report.db"),
        state=StateStore(tmp_path / "state.json"),
    )
    return page_api_module.PageApi(plugin)


def _patch_request(monkeypatch, query: dict | None = None) -> None:
    monkeypatch.setattr(page_api_module, "_request", SimpleNamespace(query=query or {}))
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)


def test_history_network_uses_resource_samples(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {}})
    now = int(time.time())
    for offset in (300, 600, 900):
        assert api.plugin.report_store.add_resource(
            now - offset, cpu=10.0, net_up=1.5, net_down=2.5
        )
    _patch_request(monkeypatch)

    payload = asyncio.run(api.history())

    assert payload["network_note_kind"] == "db"
    assert len(payload["network"]) == 3
    assert payload["network"][0]["up"] == 1.5
    assert payload["network"][0]["down"] == 2.5
    # Both charts come from the same downsampled rows, so their timestamps line up.
    assert [item["ts"] for item in payload["network"]] == [
        item["ts"] for item in payload["points"]
    ]


def test_history_network_falls_back_to_state(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {}})
    now = int(time.time())
    api.plugin.state.set("net", {"history": [[now - 60, 3.0, 4.0]]})
    _patch_request(monkeypatch)

    payload = asyncio.run(api.history())

    assert payload["network_note_kind"] == "state"
    assert payload["network"][0]["up"] == 3.0
    assert payload["network"][0]["down"] == 4.0


def test_history_network_respects_redaction(tmp_path: Path, monkeypatch) -> None:
    api = _page_api(tmp_path, {"display": {"page_redact": ["network"]}})
    assert api.plugin.report_store.add_resource(
        int(time.time()) - 300, cpu=10.0, net_up=1.5, net_down=2.5
    )
    _patch_request(monkeypatch)

    payload = asyncio.run(api.history())

    assert payload["network"] == []
    assert payload["network_note_kind"] == "redacted"


def test_history_network_and_points_stay_aligned_when_downsampled(
    tmp_path: Path, monkeypatch
) -> None:
    api = _page_api(tmp_path, {"display": {}})
    now = int(time.time())
    for index in range(300):
        assert api.plugin.report_store.add_resource(
            now - 300 * (300 - index),
            cpu=float(index % 100),
            net_up=float(index),
            net_down=float(index) / 2,
        )
    _patch_request(monkeypatch)

    payload = asyncio.run(api.history())

    assert len(payload["points"]) == 120
    assert len(payload["network"]) == 120
    assert [item["ts"] for item in payload["network"]] == [
        item["ts"] for item in payload["points"]
    ]
