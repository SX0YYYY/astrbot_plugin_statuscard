from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

TARGET_RE = re.compile(r"^(?P<platform>[^:]+):(?P<kind>[A-Za-z_]+):(?P<id>.+)$")


@dataclass
class OnebotReceipt:
    """Result of a direct OneBot send, including the protocol receipt."""

    ok: bool = False
    retcode: int | None = None
    message_id: Any = None
    error: str = ""
    skipped: str = ""


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

    A OneBot V11 response looks like ``{"status": "ok", "retcode": 0,
    "data": {"message_id": 123}}``. Failures use ``retcode != 0`` and carry a
    ``message``/``wording`` description.
    """

    if not isinstance(payload, dict):
        return OnebotReceipt(ok=False, error=f"unexpected response: {payload!r}")
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
    ok = retcode_int == 0 and status in ("", "ok", "async", "success")
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
    )


async def send_direct(
    client: Any,
    target: str,
    segments: list[dict[str, Any]],
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
    del platform
    if kind.lower() not in ("groupmessage", "privatemessage"):
        return OnebotReceipt(ok=False, error=f"不支持的目标类型: {kind}")
    if not str(session_id).isdigit():
        return OnebotReceipt(ok=False, error=f"会话 ID 不是数字: {session_id}")
    action = "send_group_msg" if kind.lower() == "groupmessage" else "send_private_msg"
    key = "group_id" if kind.lower() == "groupmessage" else "user_id"
    try:
        payload = await asyncio.wait_for(
            client.call_action(action, **{key: int(session_id), "message": segments}),
            timeout=10,
        )
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        logger.warning("OneBot 直发失败 %s -> %s: %s", action, target, detail)
        return OnebotReceipt(ok=False, error=detail)
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
