from __future__ import annotations

import asyncio

from astrbot_plugin_statuscard.onebot_receipt import (
    extract_receipt,
    parse_target,
    receipt_from_exception,
    send_direct,
)


def test_parse_target_splits_parts() -> None:
    assert parse_target("default:GroupMessage:123456") == (
        "default",
        "GroupMessage",
        "123456",
    )
    assert parse_target("default:PrivateMessage:123456")[1] == "PrivateMessage"
    assert parse_target("not-a-target") is None


def test_extract_receipt_success() -> None:
    receipt = extract_receipt(
        {"status": "ok", "retcode": 0, "data": {"message_id": 998877}}
    )
    assert receipt.ok is True
    assert receipt.retcode == 0
    assert receipt.message_id == 998877
    assert receipt.error == ""


def test_extract_receipt_accepts_unwrapped_aiocqhttp_data() -> None:
    """``aiocqhttp`` returns ``result['data']`` directly, not the envelope."""

    receipt = extract_receipt({"message_id": 4242})
    assert receipt.ok is True
    assert receipt.retcode == 0
    assert receipt.message_id == 4242
    assert receipt.attempted is True


def test_extract_receipt_handles_empty_data() -> None:
    receipt = extract_receipt({})
    assert receipt.ok is True
    assert receipt.attempted is True


def test_extract_receipt_failure_keeps_reason() -> None:
    receipt = extract_receipt(
        {"status": "failed", "retcode": 100, "message": "group not found"}
    )
    assert receipt.ok is False
    assert receipt.retcode == 100
    assert "group not found" in receipt.error
    # status=failed means OneBot refused the action: nothing was posted, so
    # the delivery layer may safely retry (v2.20.1).
    assert receipt.attempted is False
    assert receipt.rejected is True


def test_receipt_from_exception_reads_action_failed() -> None:
    class _ActionFailed(Exception):
        def __init__(self, result: dict) -> None:
            super().__init__("ActionFailed")
            self.result = result

    exc = _ActionFailed(
        {"status": "failed", "retcode": 100, "message": "group not found"}
    )
    receipt = receipt_from_exception(exc)
    assert receipt is not None
    assert receipt.ok is False
    assert receipt.retcode == 100
    assert receipt.attempted is False
    assert receipt.rejected is True


def test_receipt_from_exception_ignores_transport_error() -> None:
    receipt = receipt_from_exception(RuntimeError("connection closed"))
    assert receipt is None


def test_extract_receipt_handles_garbage() -> None:
    receipt = extract_receipt("not a dict")
    assert receipt.ok is False
    assert receipt.error
    assert receipt.attempted is True


class _FakeClient:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **kwargs: object) -> object:
        self.calls.append((action, kwargs))
        return self.payload


def test_send_direct_uses_group_action() -> None:
    client = _FakeClient({"status": "ok", "retcode": 0, "data": {"message_id": 1}})
    receipt = asyncio.run(
        send_direct(
            client,
            "default:GroupMessage:123456",
            [{"type": "text", "data": {"text": "hi"}}],
        )
    )
    assert receipt.ok is True
    action, kwargs = client.calls[0]
    assert action == "send_group_msg"
    assert kwargs["group_id"] == 123456


def test_send_direct_with_aiocqhttp_unwrapped_data() -> None:
    """``aiocqhttp`` returns the ``data`` field, so no retcode is present."""

    client = _FakeClient({"message_id": 555})
    receipt = asyncio.run(
        send_direct(
            client,
            "default:GroupMessage:123456",
            [{"type": "text", "data": {"text": "hi"}}],
        )
    )
    assert receipt.ok is True
    assert receipt.retcode == 0
    assert receipt.message_id == 555
    assert receipt.attempted is True


def test_send_direct_marks_rejected_on_protocol_error() -> None:
    class _FailingClient:
        async def call_action(self, action: str, **kwargs: object) -> object:
            class _ActionFailed(Exception):
                def __init__(self) -> None:
                    super().__init__("ActionFailed")
                    self.result = {
                        "status": "failed",
                        "retcode": 100,
                        "message": "group not found",
                    }

            raise _ActionFailed()

    receipt = asyncio.run(
        send_direct(
            _FailingClient(),
            "default:GroupMessage:123456",
            [{"type": "text", "data": {"text": "hi"}}],
        )
    )
    assert receipt.ok is False
    assert receipt.retcode == 100
    assert receipt.attempted is False
    assert receipt.rejected is True
    assert "group not found" in receipt.error


def test_send_direct_uses_private_action() -> None:
    client = _FakeClient({"status": "ok", "retcode": 0, "data": {}})
    asyncio.run(
        send_direct(
            client,
            "default:PrivateMessage:123456",
            [{"type": "text", "data": {"text": "hi"}}],
        )
    )
    action, kwargs = client.calls[0]
    assert action == "send_private_msg"
    assert kwargs["user_id"] == 123456


def test_send_direct_rejects_unsupported_target() -> None:
    client = _FakeClient({"status": "ok", "retcode": 0})
    receipt = asyncio.run(send_direct(client, "default:TempMessage:123", []))
    assert receipt.ok is False
    assert client.calls == []


def test_send_direct_supports_friend_message() -> None:
    # The schema tells users to write default:FriendMessage:<QQ> for private
    # delivery; treating it as unsupported meant private alerts never got a
    # receipt (and a framework rejection silently lost them).
    client = _FakeClient({"status": "ok", "retcode": 0, "data": {}})
    receipt = asyncio.run(send_direct(client, "default:FriendMessage:123", []))
    assert receipt.ok is True
    assert client.calls[0][0] == "send_private_msg"


def test_send_direct_skips_other_platform_target() -> None:
    client = _FakeClient({"status": "ok", "retcode": 0})
    receipt = asyncio.run(send_direct(client, "telegram:GroupMessage:123", []))
    assert receipt.ok is False
    assert receipt.skipped
    # Never send a telegram target through the QQ client: it would land in
    # whichever QQ group happens to have the same number.
    assert client.calls == []


def test_send_direct_rejects_non_numeric_session() -> None:
    client = _FakeClient({"status": "ok", "retcode": 0})
    receipt = asyncio.run(send_direct(client, "default:GroupMessage:abc", []))
    assert receipt.ok is False
    assert client.calls == []


def test_send_direct_reports_transport_error() -> None:
    class _BrokenClient:
        async def call_action(self, action: str, **kwargs: object) -> object:
            raise RuntimeError("connection closed")

    receipt = asyncio.run(
        send_direct(
            _BrokenClient(),
            "default:GroupMessage:123456",
            [{"type": "text", "data": {"text": "hi"}}],
        )
    )
    assert receipt.ok is False
    assert "connection closed" in receipt.error
