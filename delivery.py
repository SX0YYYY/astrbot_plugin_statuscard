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

from .onebot_receipt import is_action_rejected, send_direct, should_retry_send

# Failures that happen before the request can reach the platform: a broken
# message chain (Comp.Image on a missing file), a framework signature change,
# a bad local path. None of them can have delivered anything, so they must be
# reported as "definitely not sent" instead of "maybe already sent" -- the
# latter silently disables alerts and produces recovery notices for faults the
# user was never told about.
_LOCAL_SEND_ERRORS: tuple[type[BaseException], ...] = (
    TypeError,
    AttributeError,
    NameError,
    ImportError,
    FileNotFoundError,
    IsADirectoryError,
    PermissionError,
    # ValueError deliberately NOT in this list (v2.20.0): adapters can raise
    # it while parsing a response that was already on the wire, so treating
    # it as "definitely not sent" risks a duplicate message. It now counts
    # as uncertain -- the module never repeats a delivery it cannot rule out.
)


@dataclass
class DeliveryResult:
    """Per-target outcome of one logical delivery.

    ``uncertain_targets`` are the targets whose message may have reached the
    chat even though no success was confirmed (a timeout, or a OneBot error
    after the request was already sent). Callers must treat them as delivered
    and must not retry, otherwise the chat shows the message twice.
    """

    ok: bool = False
    ok_targets: list[str] = field(default_factory=list)
    failed_targets: list[str] = field(default_factory=list)
    uncertain_targets: list[str] = field(default_factory=list)
    # OneBot answered status=failed: definitely not delivered, safe to retry.
    rejected_targets: list[str] = field(default_factory=list)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    retcodes: dict[str, int | None] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Truthy when at least one target definitely accepted the message."""

        return self.ok

    @property
    def retryable(self) -> bool:
        """False when a target may already have received the message."""

        return not self.ok and not self.uncertain_targets

    @property
    def partial(self) -> bool:
        """True when at least one target succeeded and another failed.

        ``ok`` stays True (the message did reach someone) but callers should
        surface the partial success instead of recording a clean delivery.
        """

        return self.ok and bool(self.failed_targets)

    def as_state(self) -> dict[str, Any]:
        """The shape stored in ``state.json`` for the diagnostics command."""

        return {
            "ok": self.ok,
            "partial": self.partial,
            "ok_targets": list(self.ok_targets),
            "failed_targets": list(self.failed_targets),
            "uncertain_targets": list(self.uncertain_targets),
            "rejected_targets": list(self.rejected_targets),
            "receipts": dict(self.receipts),
            "retcodes": dict(self.retcodes),
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
        image = str(payload.get("image") or "")
        # A local Pillow fallback card is a host path, not a URL. The OneBot
        # container may not see that path (NapCat is often a separate
        # container), so go straight to the framework path, which reads the
        # file and uploads the bytes. Only URL images use the direct path.
        direct_ok = client is not None and (
            not image or image.lower().startswith(("http://", "https://"))
        )
        result = DeliveryResult()
        for target in targets:
            receipt = None
            if direct_ok:
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
                if receipt.rejected:
                    # OneBot refused the action outright (status=failed), so
                    # nothing reached the chat. Try the framework path: it
                    # may use a different send route, and a retry is safe.
                    logger.warning(
                        "目标 %s 的 OneBot 直发被明确拒绝（%s），尝试框架发送",
                        target,
                        receipt.error or "未知错误",
                    )
                    result.failed_targets.append(target)
                    result.rejected_targets.append(target)
                elif receipt.attempted:
                    # OneBot answered with an error without proving the send
                    # failed: the message may already be in the chat, so
                    # re-sending would duplicate it.
                    logger.warning(
                        "目标 %s 的 OneBot 直发返回错误（%s），不再重发以免重复",
                        target,
                        receipt.error or "未知错误",
                    )
                    result.failed_targets.append(target)
                    result.uncertain_targets.append(target)
                    continue
            sent, uncertain = await self._send_via_astrbot(target, payload)
            if sent:
                result.ok_targets.append(target)
                # The framework path delivered what OneBot rejected; the
                # target is no longer failed/rejected.
                if target in result.failed_targets:
                    result.failed_targets.remove(target)
                if target in result.rejected_targets:
                    result.rejected_targets.remove(target)
            else:
                if target not in result.failed_targets:
                    result.failed_targets.append(target)
                if uncertain:
                    result.uncertain_targets.append(target)
        result.ok = bool(result.ok_targets)
        self.record(kind, targets, result, source=source)
        return result

    async def _send_via_astrbot(
        self, target: str, payload: dict[str, Any]
    ) -> tuple[bool, bool]:
        """Send through the normal AstrBot path.

        Returns ``(sent, uncertain)``. ``uncertain`` is True when the message
        may have reached the chat without an acknowledgement (timeout or a
        non-retryable transport error); the caller must then treat it as
        delivered and must not repeat it.

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
                    return True, False
                logger.warning("主动%s未找到匹配平台: %s", label, target)
                return False, False
            except TimeoutError:
                logger.warning(
                    "主动发送%s到 %s 超时（60 秒，可能已送达，不再重发以免重复）",
                    label,
                    target,
                )
                return False, True
            except Exception as exc:
                detail = str(exc) or exc.__class__.__name__
                if isinstance(exc, _LOCAL_SEND_ERRORS):
                    # Raised before anything reached the platform: a broken
                    # message chain, a framework signature change, a missing
                    # local file. Treating these as "maybe already sent" made
                    # every alert a silent no-op (no retry, no failure event,
                    # and recovery notices for faults nobody was told about).
                    logger.error(
                        "主动发送%s到 %s 在本地失败（请求未发出）: %s",
                        label,
                        target,
                        detail,
                    )
                    return False, False
                if is_action_rejected(exc):
                    # OneBot's framework answered ActionFailed: the action
                    # was refused, so the message never reached the chat and a
                    # retry/alert is safe (v2.20.2).
                    logger.warning(
                        "主动发送%s到 %s 被平台明确拒绝（请求未执行，未送达）: %s",
                        label,
                        target,
                        detail,
                    )
                    return False, False
                if not should_retry_send(exc):
                    logger.warning(
                        "主动发送%s到 %s 未确认送达（可能已发出，不重发以免重复）: %s",
                        label,
                        target,
                        detail,
                    )
                    return False, True
                if attempt >= 2:
                    logger.warning(
                        "主动发送%s到 %s 失败（已重试 %d 次）: %s",
                        label,
                        target,
                        attempt,
                        detail,
                    )
                    return False, False
                logger.warning(
                    "主动发送%s到 %s 失败（第 %d 次），%.0f 秒后重试: %s",
                    label,
                    target,
                    attempt + 1,
                    attempt + 1,
                    detail,
                )
                await asyncio.sleep(attempt + 1)
        return False, False

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
            payload = {
                "ts": time.time(),
                "kind": kind,
                "ok": result.ok,
                "target_count": len(targets),
                "source": source,
                **result.as_state(),
            }
            self.state.set("delivery", payload)
            # Keep the last *real* notification separately: a dashboard
            # "verify" click used to overwrite the single slot, so the
            # diagnostics command showed a successful verify while the real
            # alert had failed.
            if not str(source).startswith(("verify", "broadcast")) and source != "test":
                self.state.set("delivery_real", payload)
            self.state.save()
        except Exception as exc:
            logger.warning("记录主动推送结果失败: %s", exc)
