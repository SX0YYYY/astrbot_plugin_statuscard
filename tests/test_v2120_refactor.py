"""Tests for the shared redaction and delivery helpers.

These modules exist so that the status card, the daily report and the Web
dashboard cannot drift apart: the same rule must hide the same value
everywhere, and a message that may already be in the chat must never be
sent twice.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_statuscard.delivery import DeliveryService
from astrbot_plugin_statuscard.redactor import (
    HIDDEN,
    Redactor,
    normalize_rules,
    read_rules,
)
from astrbot_plugin_statuscard.report_store import ReportStore

# ----------------------------------------------------------------------
# Redactor
# ----------------------------------------------------------------------


def test_normalize_rules_ignores_unknown_names() -> None:
    assert normalize_rules(["hostname", "not-a-rule", "  "]) == {"hostname"}
    assert normalize_rules("process") == {"process"}
    assert normalize_rules(None) == set()
    assert normalize_rules(123) == set()


def test_read_rules_follows_the_dotted_path() -> None:
    config = {"display": {"redact": ["hostname", "process"]}}
    assert read_rules(config, "display.redact") == {"hostname", "process"}
    assert read_rules(config, "display.page_redact") == set()


def test_read_rules_never_inherits_another_surface() -> None:
    """A missing switch means "hide nothing", not "use the card rules"."""

    config = {"display": {"redact": ["hostname"]}}
    assert read_rules(config, "display.page_redact") == set()
    assert read_rules(config, "display.nonexistent") == set()


def test_redactor_flags_each_rule() -> None:
    redactor = Redactor(["hostname", "qq_stats"])
    assert redactor.hostname is True
    assert redactor.qq_stats is True
    assert redactor.process is False
    assert redactor.network is False
    assert "hostname" in redactor
    assert redactor.sorted_rules == ["hostname", "qq_stats"]


def test_apply_card_hides_the_configured_details() -> None:
    view = {
        "overview": [{"label": "主机名", "value": "server-01"}],
        "system_info": [{"label": "Host", "value": "server-01"}],
        "process": {"top": [{"name": "python3"}]},
        "network": {"up": "1 KB/s", "interface": "eth0"},
        "bot": {"friends": 10, "groups": 3, "members": 100},
    }
    Redactor(["hostname", "process", "network", "qq_stats"]).apply_card(view)

    assert view["overview"][0]["value"] == HIDDEN
    assert view["system_info"][0]["value"] == HIDDEN
    assert view["process"]["top"][0]["name"] == HIDDEN
    assert view["process"]["redacted"] is True
    assert view["network"]["up"] == "--"
    assert view["network"]["interface"] == HIDDEN
    assert view["network"]["chart"]["empty"] is True
    assert view["bot"]["friends"] is None
    assert view["redacted"] == ["hostname", "network", "process", "qq_stats"]


def test_apply_card_leaves_everything_alone_without_rules() -> None:
    view = {"overview": [{"label": "主机名", "value": "server-01"}]}
    Redactor([]).apply_card(view)
    assert view["overview"][0]["value"] == "server-01"
    assert "redacted" not in view


def test_apply_report_hides_the_qq_section() -> None:
    section = {
        "available": True,
        "days": ["2026-10-20"],
        "labels": ["10-20"],
        "yesterday": {"friends": 22},
        "previous": {},
        "reset_days": [],
        "sent_available": True,
        "snapshot_note": "note",
        "recv_note": "note",
        "charts": {"friends": []},
        "note": "",
    }
    Redactor(["qq_stats"]).apply_report(section, "qq")
    assert section["available"] is False
    assert section["empty_reason"] == "已按脱敏配置隐藏 QQ 统计"
    assert section["yesterday"] == {}
    assert section["charts"] == {}


def test_apply_report_hides_the_process_names() -> None:
    section = {"top": [{"name": "python3", "cpu_text": "1.0%"}]}
    Redactor(["process"]).apply_report(section, "process")
    assert section["top"][0]["name"] == HIDDEN


def test_dashboard_host_is_hidden_by_the_hostname_rule() -> None:
    assert Redactor(["hostname"]).dashboard_host("server-01") == HIDDEN
    assert Redactor([]).dashboard_host("server-01") == "server-01"


# ----------------------------------------------------------------------
# DeliveryService
# ----------------------------------------------------------------------


class _FakeContext:
    """A context that records what was sent."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def send_message(self, target, chain):
        self.calls.append(target)
        if self.error is not None:
            raise self.error
        return True


class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        return None


def _service(context, client=None) -> DeliveryService:
    return DeliveryService(
        context,
        _FakeState(),
        lambda: client,
        lambda payload: payload,
    )


def test_delivery_records_the_protocol_receipt() -> None:
    class _Client:
        async def call_action(self, action, **params):
            return {"message_id": 4242}

    service = _service(_FakeContext(), _Client())
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "hi"}))

    assert result.ok is True
    assert result.ok_targets == ["default:GroupMessage:1"]
    assert result.retcodes == {"default:GroupMessage:1": 0}
    assert result.receipts["default:GroupMessage:1"]["message_id"] == 4242


def test_delivery_does_not_resend_after_a_lost_receipt() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def call_action(self, action, **params):
            self.calls += 1
            raise RuntimeError("WebSocket API call timeout")

    context = _FakeContext()
    client = _Client()
    service = _service(context, client)
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "hi"}))

    assert result.ok is False
    # The request reached OneBot, so the fallback must not run.
    assert client.calls == 1
    assert context.calls == []


def test_delivery_falls_back_when_the_request_never_left() -> None:
    class _Client:
        async def call_action(self, action, **params):
            raise RuntimeError("API root url invalid")

    context = _FakeContext()
    service = _service(context, _Client())
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "hi"}))

    assert result.ok is True
    assert context.calls == ["default:GroupMessage:1"]


def test_delivery_retries_only_safe_failures() -> None:
    context = _FakeContext(error=RuntimeError("API root url invalid"))
    service = _service(context, None)
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "hi"}))
    assert result.ok is False
    assert len(context.calls) == 3


def test_delivery_does_not_retry_a_transport_error() -> None:
    context = _FakeContext(error=ConnectionResetError("websocket closed"))
    service = _service(context, None)
    result = asyncio.run(service.send(["default:GroupMessage:1"], {"text": "hi"}))
    assert result.ok is False
    assert len(context.calls) == 1


def test_delivery_without_targets_is_not_ok() -> None:
    service = _service(_FakeContext())
    result = asyncio.run(service.send([], {"text": "hi"}))
    assert result.ok is False
    assert result.ok_targets == []


def test_delivery_records_the_outcome_in_the_state() -> None:
    state = _FakeState()
    service = DeliveryService(
        _FakeContext(), state, lambda: None, lambda payload: payload
    )
    asyncio.run(
        service.send(["default:GroupMessage:1"], {"text": "hi"}, source="alert")
    )
    recorded = state.data["delivery"]
    assert recorded["source"] == "alert"
    assert recorded["kind"] == "text"
    assert recorded["ok"] is True
    assert recorded["ok_targets"] == ["default:GroupMessage:1"]


# ----------------------------------------------------------------------
# resolved_ts
# ----------------------------------------------------------------------


def test_resolve_event_closes_the_open_alert(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1000, "2026-10-20", "resource", "CPU 使用率", "high", "alert")
    store.add_event(2000, "2026-10-20", "resource", "CPU 使用率", "ok", "recovery")

    assert store.resolve_event(2000, "CPU 使用率", 2000) is True
    events = store.list_events("2026-10-20", limit=10)
    alerts = [item for item in events if item.get("status") == "alert"]
    assert alerts and alerts[0].get("resolved_ts") == 2000
