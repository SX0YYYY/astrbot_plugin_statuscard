from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

TARGET_RE = re.compile(r"^(?P<platform>[^:]+):(?P<kind>[A-Za-z_]+):(?P<id>.+)$")


@dataclass
class OnebotReceipt:
    """Result of a direct OneBot send, including the protocol receipt.

    ``attempted`` tells whether the request actually reached OneBot. When it
    is ``True`` the message may already be in the chat even though ``ok`` is
    ``False``, so the caller must not re-send it.

    ``rejected`` marks the stronger case where OneBot answered with
    ``status: failed``: the action was refused outright and nothing reached
    the chat, so retrying (or falling back to the framework path) cannot
    produce a duplicate message.
    """

    ok: bool = False
    retcode: int | None = None
    message_id: Any = None
    error: str = ""
    skipped: str = ""
    attempted: bool = False
    rejected: bool = False


@dataclass
class DirectSendOutcome:
    """Aggregate result of sending one payload to several targets."""

    ok: bool = False
    receipts: dict[str, OnebotReceipt] = field(default_factory=dict)

    @property
    def ok_targets(self) -> list[str]:
        return [t for t, r in self.receipts.items() if r.ok]

    @property
    def failed_targets(self) -> list[str]:
        return [t for t, r in self.receipts.items() if not r.ok]


def parse_target(target: str) -> tuple[str, str, str] | None:
    """Split ``platform:MessageType:session_id`` into its parts."""

    match = TARGET_RE.match(str(target or "").strip())
    if not match:
        return None
    return (
        match.group("platform"),
        match.group("kind"),
        match.group("id"),
    )


def extract_receipt(payload: Any) -> OnebotReceipt:
    """Turn a OneBot action response into a receipt.

    ``aiocqhttp``'s ``call_action`` already unwraps the protocol envelope: it
    returns ``result["data"]`` on success and raises ``ActionFailed`` on
    failure. The raw envelope (``{"status", "retcode", "data"}``) is still
    accepted for callers that talk to OneBot directly.
    """

    if payload is None:
        return OnebotReceipt(ok=True, retcode=0, attempted=True)
    if not isinstance(payload, dict):
        return OnebotReceipt(
            ok=False, error=f"unexpected response: {payload!r}", attempted=True
        )
    retcode = payload.get("retcode")
    try:
        retcode_int = int(retcode) if retcode is not None else None
    except (TypeError, ValueError):
        retcode_int = None
    status = str(payload.get("status") or "").lower()
    data = payload.get("data")
    message_id = None
    if isinstance(data, dict):
        message_id = data.get("message_id")
    elif "message_id" in payload:
        message_id = payload.get("message_id")
    # OneBot 11 retcode=1 means "accepted, async reply may follow". Treating
    # it as a hard failure made every send on an async implementation log a
    # warning and count as uncertain; keep the explicit failed status as the
    # only hard-failure path (v2.20.0).
    if status == "failed":
        # The action was refused before anything was posted (wrong group,
        # muted bot, media upload failure). ``attempted=False`` tells the
        # delivery layer that a retry cannot duplicate the message; the old
        # code marked this ``attempted=True`` and every such fault silently
        # disappeared from the alert path (v2.20.1).
        error = str(
            payload.get("message")
            or payload.get("wording")
            or payload.get("error")
            or f"retcode={retcode_int}"
        )
        return OnebotReceipt(
            ok=False,
            retcode=retcode_int,
            message_id=message_id,
            error=error,
            attempted=False,
            rejected=True,
        )
    if retcode_int is not None and retcode_int not in (0, 1):
        # A non-zero retcode without status=failed may still mean the request
        # reached the implementation (some adapters quote business errors on
        # an otherwise accepted action), so keep it in the uncertain bucket.
        error = str(
            payload.get("message")
            or payload.get("wording")
            or payload.get("error")
            or f"retcode={retcode_int}"
        )
        return OnebotReceipt(
            ok=False,
            retcode=retcode_int,
            message_id=message_id,
            error=error,
            attempted=True,
        )
    if "status" not in payload and retcode_int is None:
        # ``aiocqhttp`` already unwrapped the envelope: the returned dict is
        # the ``data`` field, so reaching here means the action succeeded.
        return OnebotReceipt(ok=True, retcode=0, message_id=message_id, attempted=True)
    # retcode=1 (async accepted) still counts as delivered-ish: the send
    # was accepted by the protocol implementation (v2.20.0).
    ok = retcode_int in (0, 1, None) and status in ("", "ok", "async", "success")
    error = ""
    if not ok:
        error = str(
            payload.get("message")
            or payload.get("wording")
            or payload.get("error")
            or f"retcode={retcode_int}"
        )
    return OnebotReceipt(
        ok=ok,
        retcode=retcode_int,
        message_id=message_id,
        error=error,
        attempted=True,
    )


def receipt_from_exception(exc: BaseException) -> OnebotReceipt | None:
    """Build a receipt from an ``aiocqhttp`` failure exception.

    ``aiocqhttp`` raises ``ActionFailed`` when OneBot answers with
    ``status: failed``. The exception carries the raw envelope, so the
    protocol error code can still be reported. Other exceptions mean the
    request never reached OneBot, and return ``None``.
    """

    raw = getattr(exc, "result", None)
    if isinstance(raw, dict) and (
        "retcode" in raw or str(raw.get("status", "")).lower() == "failed"
    ):
        receipt = extract_receipt(raw)
        if not receipt.ok:
            # Keep ``attempted=False`` for an explicit status=failed: the
            # message provably never left the plugin and may be retried.
            if not receipt.rejected:
                receipt.attempted = True
            return receipt
    text = str(exc)
    if "ActionFailed" in type(exc).__name__ or "action failed" in text.lower():
        return OnebotReceipt(ok=False, error=text or "ActionFailed", attempted=True)
    return None


def is_definitely_not_sent(exc: BaseException) -> bool:
    """Tell whether the request provably never left the plugin.

    ``aiocqhttp`` raises these when the OneBot endpoint could not be
    reached at all, so the message was definitely not delivered and
    re-sending is safe. Every other failure - including a WebSocket
    timeout after the request frame was already written - may have been
    delivered, so it must not be retried.
    """

    name = type(exc).__name__
    if name in ("ApiNotAvailable", "InvalidURL"):
        return True
    text = str(exc)
    return "API root url invalid" in text


def is_action_rejected(exc: BaseException) -> bool:
    """OneBot answered ``ActionFailed``: the action was refused, nothing sent.

    This is stronger than "uncertain": the protocol implementation did not
    execute the action, so a retry (or the framework fallback) cannot produce
    a duplicate message. Treating it as uncertain silently dropped alerts when
    the framework path rejected the same target (muted bot, wrong group).
    """

    name = type(exc).__name__
    if "ActionFailed" in name:
        return True
    return "action failed" in str(exc).lower()


def should_retry_send(exc: BaseException) -> bool:
    """Tell whether a failed send may be retried safely.

    A send is only retried while the failure proves that the message never
    left the plugin. Once the request was handed to the platform, a
    transport error can still mean the message arrived and only the
    acknowledgement was lost, so retrying would show the same message
    several times in the chat.
    """

    return is_definitely_not_sent(exc)


# The QQ side can queue a send for several seconds during busy hours: a 21:00
# report timed out at 10s while NapCat was still busy with other groups and
# never delivered. 20s covers that without blocking a failed send for long.
DIRECT_SEND_TIMEOUT = 20.0


async def send_direct(
    client: Any,
    target: str,
    segments: list[dict[str, Any]],
    timeout: float = DIRECT_SEND_TIMEOUT,
) -> OnebotReceipt:
    """Send raw OneBot segments to one target and return the receipt.

    Only ``GROUP_MESSAGE``/``PRIVATE_MESSAGE`` targets are supported. The
    function never raises: transport problems are reported inside the receipt
    so callers can keep sending to the remaining targets.
    """

    parsed = parse_target(target)
    if parsed is None:
        return OnebotReceipt(ok=False, error=f"无法解析目标: {target}")
    platform, kind, session_id = parsed
    if str(platform or "").lower() not in ("", "default", "aiocqhttp"):
        # Another platform instance (telegram/another bot): this OneBot client
        # must not send it, or the message lands in a same-numbered QQ chat.
        # Return a skipped receipt so the delivery layer uses the framework
        # path, which routes by the full UMO.
        return OnebotReceipt(
            ok=False,
            skipped=f"platform:{platform}",
            error=f"目标属于其他平台: {platform}",
        )
    kind_lower = kind.lower()
    if kind_lower not in ("groupmessage", "privatemessage", "friendmessage"):
        return OnebotReceipt(ok=False, error=f"不支持的目标类型: {kind}")
    if not str(session_id).isdigit():
        return OnebotReceipt(ok=False, error=f"会话 ID 不是数字: {session_id}")
    action = "send_group_msg" if kind_lower == "groupmessage" else "send_private_msg"
    key = "group_id" if kind_lower == "groupmessage" else "user_id"
    try:
        payload = await asyncio.wait_for(
            client.call_action(action, **{key: int(session_id), "message": segments}),
            timeout=timeout,
        )
    except Exception as exc:
        receipt = receipt_from_exception(exc)
        if receipt is not None:
            logger.warning(
                "OneBot 直发被拒绝 %s -> %s：retcode=%s，%s",
                action,
                target,
                receipt.retcode,
                receipt.error,
            )
            return receipt
        detail = str(exc) or exc.__class__.__name__
        if is_definitely_not_sent(exc):
            # The request provably never reached OneBot, so re-sending is
            # safe: the caller may fall back to the normal AstrBot path.
            logger.warning("OneBot 直发未送达 %s -> %s: %s", action, target, detail)
            return OnebotReceipt(ok=False, error=detail, attempted=False)
        # The request frame was already written to the WebSocket, so the
        # message may be in the chat even though the receipt was lost.
        # Re-sending would duplicate it in the group.
        logger.warning(
            "OneBot 直发回执丢失 %s -> %s（消息可能已送达，不重发）: %s",
            action,
            target,
            detail,
        )
        return OnebotReceipt(ok=False, error=detail, attempted=True)
    receipt = extract_receipt(payload)
    if receipt.ok:
        logger.info(
            "OneBot 直发成功 %s -> %s（message_id=%s，retcode=%s）",
            action,
            target,
            receipt.message_id,
            receipt.retcode,
        )
    else:
        logger.warning(
            "OneBot 直发被拒绝 %s -> %s：retcode=%s，%s",
            action,
            target,
            receipt.retcode,
            receipt.error,
        )
    return receipt
