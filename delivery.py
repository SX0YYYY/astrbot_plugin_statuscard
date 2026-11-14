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
from .platform_support import ensure_group_scene, find_adapter, parse_umo
from .utils import mask_chat_ids, mask_umo

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
    # ValueError deliberately NOT in this list (v2.20.0): adapters can raise
    # it while parsing a response that was already on the wire, so treating
    # it as "definitely not sent" risks a duplicate message. It now counts
    # as uncertain -- the module never repeats a delivery it cannot rule out.
    # PermissionError used to be here as well, but it is an OSError
    # subclass and a socket write can fail with it *after* the frame left
    # the process: retrying such a delivery can post the same message
    # twice, which is exactly what the module avoids everywhere else.
)


# AstrBot resolves the target string before anything reaches an adapter:
# ``MessageSesion.from_str`` raises ValueError("不合法的 session 字符串: ...")
# for a malformed UMO, and the aiocqhttp dispatcher raises
# ValueError("无法发送消息：缺少有效的数字 session_id(...)") for a non-numeric
# id. Both happen while the request is still local, so they are "definitely
# not sent" even though ValueError is otherwise treated as uncertain.
_UMO_ERROR_HINTS: tuple[str, ...] = (
    "不合法的 session 字符串",
    "无法发送消息",
    "无效的 session",
)


def _is_local_umo_error(exc: BaseException) -> bool:
    """True when the framework rejected the *target string* before sending."""

    if not isinstance(exc, ValueError):
        return False
    text = str(exc)
    return any(hint in text for hint in _UMO_ERROR_HINTS)


# Markers that prove the transport itself is gone rather than the message
# being wrong. ``ApiNotAvailable`` is raised by aiocqhttp when no reverse-WS
# client is registered; ``InvalidURL``/``API root url invalid`` mean the
# configured endpoint is unusable. A delivery that failed this way will fail
# identically until the connection comes back, so callers must not spend
# their finite retry budget on it (v2.20.5).
_OFFLINE_MARKERS: tuple[str, ...] = (
    "apinotavailable",
    "invalidurl",
    "api root url invalid",
    "未找到匹配平台",
)


def _looks_offline(text: Any) -> bool:
    """True when an error text describes an unreachable transport."""

    value = str(text or "").lower()
    return bool(value) and any(marker in value for marker in _OFFLINE_MARKERS)


def _is_offline_exc(exc: BaseException) -> bool:
    """True when a thrown transport error proves the channel is down."""

    name = type(exc).__name__
    if name in ("ApiNotAvailable", "InvalidURL"):
        return True
    return "api root url invalid" in str(exc).lower()


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
    # The transport itself was unreachable (no reverse-WS client, invalid
    # URL): every target failed for the same reason and retrying cannot help
    # until the connection comes back. Callers keep their retry budget for
    # content errors instead of burning it during an outage (v2.20.5).
    transport_offline: bool = False

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
            "transport_offline": self.transport_offline,
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
        platform_id: str = "",
    ):
        self.context = context
        self.state = state
        self._get_client = get_client
        self._build_chain = build_chain
        # A custom platform instance id (``alerts.platform_id``) is not
        # "another platform": with it, every target used to be skipped by
        # the OneBot direct path, silently losing retcode/message_id
        # receipts and the "status=failed means not delivered" distinction
        # (v2.20.21).
        self._platform_id = str(platform_id or "").strip()

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
        offline_hits = False
        for target in targets:
            primed, prime_error = self._prime_official_target(target)
            if not primed:
                logger.warning("目标 %s：%s，跳过发送", mask_umo(target), prime_error)
                result.failed_targets.append(target)
                continue
            receipt = None
            if direct_ok:
                receipt = await send_direct(
                    client,
                    target,
                    self._segments(payload),
                    platform_id=self._platform_id,
                )
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
                if _looks_offline(receipt.error):
                    offline_hits = True
                if receipt.rejected:
                    # OneBot refused the action outright (status=failed), so
                    # nothing reached the chat. Try the framework path: it
                    # may use a different send route, and a retry is safe.
                    logger.warning(
                        "目标 %s 的 OneBot 直发被明确拒绝（%s），尝试框架发送",
                        mask_umo(target),
                        mask_chat_ids(receipt.error or "未知错误"),
                    )
                    result.failed_targets.append(target)
                    result.rejected_targets.append(target)
                elif receipt.attempted:
                    # OneBot answered with an error without proving the send
                    # failed: the message may already be in the chat, so
                    # re-sending would duplicate it.
                    logger.warning(
                        "目标 %s 的 OneBot 直发返回错误（%s），不再重发以免重复",
                        mask_umo(target),
                        mask_chat_ids(receipt.error or "未知错误"),
                    )
                    result.failed_targets.append(target)
                    result.uncertain_targets.append(target)
                    continue
            sent, uncertain, offline = await self._send_via_astrbot(target, payload)
            if offline:
                offline_hits = True
            if sent:
                result.ok_targets.append(target)
                # The framework path delivered what OneBot rejected; the
                # target is no longer failed/rejected.
                if target in result.failed_targets:
                    result.failed_targets.remove(target)
                if target in result.rejected_targets:
                    result.rejected_targets.remove(target)
                # Keep the stored receipt in sync: the direct path already
                # stored "ok": false, and the diagnostics command printed a
                # failed OneBot receipt right next to a successful delivery
                # verdict (v2.20.20).
                previous = result.receipts.get(target)
                if isinstance(previous, dict):
                    result.receipts[target] = {
                        **previous,
                        "ok": True,
                        "via": "framework",
                    }
            else:
                if target not in result.failed_targets:
                    result.failed_targets.append(target)
                if uncertain:
                    result.uncertain_targets.append(target)
        result.ok = bool(result.ok_targets)
        # Only a total failure counts as "the transport is down": a partial
        # success means the channel works and the remaining errors are
        # target-specific (muted bot, wrong group).
        result.transport_offline = offline_hits and not result.ok
        self.record(kind, targets, result, source=source)
        return result

    def _prime_official_target(self, target: str) -> tuple[bool, str]:
        """Prime the official adapter's group scene cache before sending.

        AstrBot's official adapter drops a group send when its in-memory scene
        map has no entry -- the normal state right after a restart, before
        that group posts again -- and reports success anyway. Priming the
        cache keeps a scheduled report from being silently lost. Non-official
        platforms and unknown targets pass through unchanged.
        """

        parsed = parse_umo(target)
        if parsed is None:
            return True, ""
        platform_id = parsed[0]
        adapter = find_adapter(self.context, None, platform_id)
        if adapter is None:
            # No live instance for this UMO prefix: let the framework report
            # "no matching platform" instead of hiding a configuration bug.
            return True, ""
        ok, _failed = ensure_group_scene(adapter, [target])
        if ok:
            return True, ""
        return False, "官方机器人场景缓存注入失败（目标可能不是群 UMO）"

    async def _send_via_astrbot(
        self, target: str, payload: dict[str, Any]
    ) -> tuple[bool, bool, bool]:
        """Send through the normal AstrBot path.

        Returns ``(sent, uncertain, offline)``. ``uncertain`` is True when the
        message may have reached the chat without an acknowledgement (timeout
        or a non-retryable transport error); the caller must then treat it as
        delivered and must not repeat it. ``offline`` is True when the failure
        proves the transport itself is unreachable (ApiNotAvailable/InvalidURL
        or no matching platform), so a caller can keep its retry budget for a
        later attempt instead of spending it during an outage.

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
                    return True, False, False
                # A falsy return has two very different causes: no platform
                # instance matches the target's prefix, OR the official
                # adapter dropped the message because its in-memory scene
                # cache was empty (platform_support.send_by_session returns
                # None silently). Naming both keeps the operator from chasing
                # a "wrong platform" that is actually a lost cache (v2.21.3).
                logger.warning(
                    "主动%s未找到匹配平台（或官方机器人会话缓存丢失被跳过）: %s",
                    label,
                    mask_umo(target),
                )
                return False, False, True
            except TimeoutError:
                logger.warning(
                    "主动发送%s到 %s 超时（60 秒，可能已送达，不再重发以免重复）",
                    label,
                    mask_umo(target),
                )
                return False, True, False
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
                    return False, False, False
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
                    return False, False, False
                if _is_local_umo_error(exc):
                    # A hand-written target such as "default:GroupMessage:abc"
                    # (or a lowercase/unknown message type) is rejected by the
                    # framework before a request exists. Treating it as
                    # uncertain marked a misconfigured target as delivered --
                    # no retry, no failed event, and the recovery notice later
                    # claimed the alert had arrived.
                    logger.error(
                        "主动发送%s到 %s 的目标格式无效（请求未发出）: %s",
                        label,
                        target,
                        detail,
                    )
                    return False, False, False
                if not should_retry_send(exc):
                    logger.warning(
                        "主动发送%s到 %s 未确认送达（可能已发出，不重发以免重复）: %s",
                        label,
                        target,
                        detail,
                    )
                    return False, True, False
                if attempt >= 2:
                    logger.warning(
                        "主动发送%s到 %s 失败（已重试 %d 次）: %s",
                        label,
                        target,
                        attempt,
                        detail,
                    )
                    return False, False, _is_offline_exc(exc)
                logger.warning(
                    "主动发送%s到 %s 失败（第 %d 次），%.0f 秒后重试: %s",
                    label,
                    target,
                    attempt + 1,
                    attempt + 1,
                    detail,
                )
                await asyncio.sleep(attempt + 1)
        return False, False, False

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
            # alert had failed. The offline broadcast is excluded too --
            # its source is "offline_broadcast", which does not start with
            # "broadcast", so it used to overwrite the slot with the result
            # of the last group it posted to.
            if not str(source).startswith(
                ("verify", "broadcast", "offline_broadcast")
            ) and source not in ("test", "report_preview", "weekly_preview"):
                self.state.set("delivery_real", payload)
            self.state.save()
        except Exception as exc:
            logger.warning("记录主动推送结果失败: %s", exc)
