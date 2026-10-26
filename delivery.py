"""One place that delivers a message to a chat and reports what happened.

The plugin pushes several kinds of messages - resource alerts, service
monitoring, the daily status image, the daily report and the recovery
notices. They all share the same hard requirement: a message that might
already be in the chat must never be sent a second time, because the user
would see it twice.

This module owns that decision. It sends through the OneBot action API
first, because only that path returns the protocol receipt (``retcode`` and
``message_id``) which is the only way to tell whether QQ really accepted
the message. It falls back to the normal AstrBot send path only when the
request provably never left the plugin.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .onebot_receipt import send_direct, should_retry_send


@dataclass
class DeliveryResult:
    """Per-target outcome of one logical delivery."""

    ok: bool = False
    ok_targets: list[str] = field(default_factory=list)
    failed_targets: list[str] = field(default_factory=list)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    retcodes: dict[str, int | None] = field(default_factory=dict)

    def as_state(self) -> dict[str, Any]:
        """The shape stored in ``state.json`` for the diagnostics command."""

        return {
            "ok": self.ok,
            "ok_targets": list(self.ok_targets),
            "failed_targets": list(self.failed_targets),
            "receipts": dict(self.receipts),
            "retcodes": dict(self.retcodes),
        }


@dataclass
class Delivery:
    """One message that is waiting to be delivered."""

    kind: str
    source: str
    targets: list[str]
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    attempts: int = 0
    last_error: str = ""

    def to_state(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "targets": list(self.targets),
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }


class DeliveryService:
    """Send messages to a chat and keep track of the protocol receipts.

    The service is deliberately small: it knows how to reach the OneBot
    client, how to build a message chain for the fallback path and how to
    record the outcome. Everything else - what to send and when - belongs
    to the caller.
    """

    def __init__(
        self,
        context: Any,
        state: Any,
        get_client: Callable[[], Any],
        build_chain: Callable[[dict[str, Any]], Any],
    ):
        self.context = context
        self.state = state
        self._get_client = get_client
        self._build_chain = build_chain
        self._queue: list[Delivery] = []

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    async def send(
        self,
        targets: list[str],
        payload: dict[str, Any],
        kind: str = "text",
        source: str = "unknown",
    ) -> DeliveryResult:
        """Send one payload to every target and report the outcome.

        ``payload`` holds the message content, for example
        ``{"text": "..."}`` or ``{"image": "https://..."}``.
        """

        if not targets:
            logger.warning("主动推送目标为空，跳过发送")
            return DeliveryResult(ok=False)

        client = self._safe_client()
        result = DeliveryResult()
        for target in targets:
            receipt = None
            if client is not None:
                receipt = await send_direct(client, target, self._segments(payload))
            if receipt is not None:
                result.receipts[target] = {
                    "ok": receipt.ok,
                    "retcode": receipt.retcode,
                    "message_id": receipt.message_id,
                    "error": receipt.error,
                }
                result.retcodes[target] = receipt.retcode
                if receipt.ok:
                    result.ok_targets.append(target)
                    continue
                if receipt.attempted:
                    # OneBot answered with an error: the message may already
                    # be in the chat, so re-sending would duplicate it.
                    logger.warning(
                        "目标 %s 的 OneBot 直发返回错误（%s），不再重发以免重复",
                        target,
                        receipt.error or "未知错误",
                    )
                    result.failed_targets.append(target)
                    continue
            sent = await self._send_via_astrbot(target, payload)
            if sent:
                result.ok_targets.append(target)
            else:
                result.failed_targets.append(target)
        result.ok = bool(result.ok_targets)
        self.record(kind, targets, result, source=source)
        return result

    async def _send_via_astrbot(self, target: str, payload: dict[str, Any]) -> bool:
        """Send through the normal AstrBot path, retrying only safe cases.

        A transport error can also mean the message arrived and only the
        acknowledgement was lost, so the send is repeated only when the
        failure proves that the message never left the plugin.
        """

        label = "图片" if payload.get("image") else "消息"
        for attempt in range(3):
            try:
                # Bound the framework path as well: a half-open WebSocket can
                # otherwise park a monitor/alert/daily loop on this await.
                sent = await asyncio.wait_for(
                    self.context.send_message(target, self._build_chain(payload)),
                    timeout=60,
                )
                if sent:
                    return True
                logger.warning("主动%s未找到匹配平台: %s", label, target)
                return False
            except TimeoutError:
                logger.warning(
                    "主动发送%s到 %s 超时（60 秒，可能已送达，不再重发以免重复）",
                    label,
                    target,
                )
                return False
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                if not should_retry_send(exc):
                    logger.warning(
                        "主动发送%s到 %s 未确认送达（可能已发出，不重发以免重复）: %s",
                        label,
                        target,
                        detail,
                    )
                    return False
                if attempt >= 2:
                    logger.warning(
                        "主动发送%s到 %s 失败（已重试 %d 次）: %s",
                        label,
                        target,
                        attempt,
                        detail,
                    )
                    return False
                logger.warning(
                    "主动发送%s到 %s 失败（第 %d 次），%.0f 秒后重试: %s",
                    label,
                    target,
                    attempt + 1,
                    attempt + 1,
                    detail,
                )
                await asyncio.sleep(attempt + 1)
        return False

    def _safe_client(self) -> Any:
        try:
            return self._get_client()
        except Exception:
            return None

    @staticmethod
    def _segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the raw OneBot segments for a payload."""

        image = payload.get("image")
        if image:
            return [{"type": "image", "data": {"file": str(image)}}]
        return [{"type": "text", "data": {"text": str(payload.get("text") or "")}}]

    # ------------------------------------------------------------------
    # bookkeeping
    # ------------------------------------------------------------------

    def record(
        self,
        kind: str,
        targets: list[str],
        result: DeliveryResult,
        source: str = "unknown",
    ) -> None:
        """Store the outcome of a delivery in the plugin state."""

        try:
            self.state.set(
                "delivery",
                {
                    "ts": time.time(),
                    "kind": kind,
                    "ok": result.ok,
                    "target_count": len(targets),
                    "source": source,
                    **result.as_state(),
                },
            )
            self.state.save()
        except Exception as exc:
            logger.warning("记录主动推送结果失败: %s", exc)

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------

    def enqueue(self, delivery: Delivery) -> None:
        """Remember a message that could not be delivered yet."""

        self._queue.append(delivery)

    def dequeue(self, source: str | None = None) -> list[Delivery]:
        """Take the queued messages, optionally of one source only."""

        if source is None:
            pending = list(self._queue)
            self._queue.clear()
            return pending
        pending = [item for item in self._queue if item.source == source]
        for item in pending:
            self._queue.remove(item)
        return pending

    def queued(self, source: str | None = None) -> list[Delivery]:
        """The queued messages without removing them."""

        if source is None:
            return list(self._queue)
        return [item for item in self._queue if item.source == source]

    def clear_queue(self) -> None:
        self._queue.clear()
