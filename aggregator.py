"""Merge bursts of alerts into one summary after the first one delivers.

A real outage fans out: one tunnel goes down and four HTTP monitors fail at
once, a full disk trips the disk rule and the load rule together. The first
alert of an episode still goes out immediately, but further alerts that
arrive while the window is open are merged into a single summary message
instead of posting a burst. Every item is still recorded as an event by its
own rule, so the daily report and the event log stay complete; a window of
0 disables the behaviour entirely.

Buffers are kept per notification kind (resource, service, ...). The two
engines share one aggregator, and the notification center may route the two
kinds to different chats; a single shared buffer would send a service
summary to whichever target happened to open the window first.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

SendFn = Callable[[str], Awaitable[Any]]


@dataclass
class _Bucket:
    """One notification kind's open aggregation window."""

    until: float = 0.0
    pending: list[tuple[str, str]] = field(default_factory=list)
    flush_task: asyncio.Task | None = None
    send: SendFn | None = None
    retries: int = 0


class AlertAggregator:
    """Collect follow-up alerts for a short window and post one summary."""

    def __init__(self, window_seconds: int = 0, max_items: int = 8) -> None:
        self.window_seconds = max(0, int(window_seconds))
        self.max_items = max(2, min(20, int(max_items)))
        self._buckets: dict[str, _Bucket] = {}
        self._stopped = False

    @property
    def active(self) -> bool:
        """True while at least one follow-up window is open."""

        if self.window_seconds <= 0:
            return False
        now = time.time()
        return any(bucket.until > now for bucket in self._buckets.values())

    def active_for(self, kind: str) -> bool:
        """True while follow-up alerts of ``kind`` should be buffered."""

        bucket = self._buckets.get(str(kind))
        return bool(
            self.window_seconds > 0
            and bucket is not None
            and time.time() < bucket.until
        )

    def _bucket(self, kind: str) -> _Bucket:
        return self._buckets.setdefault(str(kind), _Bucket())

    async def push(
        self,
        label: str,
        reason: str,
        text: str,
        send: SendFn,
        kind: str = "default",
    ) -> Any | None:
        """Deliver ``text`` now, or buffer it for the window summary.

        Returns the delivery result when the caller must treat this as its
        own send (the first alert of a window, or aggregation disabled);
        returns ``None`` when the item was buffered and will be covered by
        the summary.
        """

        key = str(kind or "default")
        bucket = self._bucket(key)
        if not (self.window_seconds > 0 and time.time() < bucket.until):
            # Claim the window *before* awaiting ``send``. Claiming it after
            # the await meant concurrent alerts (monitor.check_all probes all
            # targets in parallel) both took this "first alert" branch, and
            # whichever send returned last then reset ``bucket.pending`` and
            # silently dropped every follow-up buffered in between -- while
            # those rules still latched ``alerting=True`` and would later send
            # a recovery notice for a fault the user never saw.
            if self.window_seconds > 0:
                bucket.until = time.time() + self.window_seconds
                bucket.send = send
                self._schedule_flush(key)
            try:
                return await send(text)
            except BaseException:
                # Nothing reached the chat and nothing is buffered yet:
                # release the window so the next alert is sent on its own
                # instead of waiting for a summary that would never exist.
                if self.window_seconds > 0:
                    bucket.until = 0.0
                    bucket.send = None
                raise
        bucket.pending.append((label, reason))
        # Keep a bounded buffer (max_items are listed, the rest are counted).
        del bucket.pending[: max(0, len(bucket.pending) - self.max_items * 2)]
        return None

    def _schedule_flush(self, kind: str) -> None:
        if self._stopped:
            return
        bucket = self._buckets.get(kind)
        if bucket is None or bucket.send is None:
            return
        if bucket.flush_task and not bucket.flush_task.done():
            return
        delay = max(1.0, bucket.until - time.time())
        bucket.flush_task = asyncio.create_task(self._flush_later(kind, delay))

    def _schedule_if_pending(self, kind: str) -> None:
        """Make sure buffered follow-ups get a task, even if the window ended.

        When a summary send is slow, new alerts can arrive and open a fresh
        window that expires before the send returns. Waiting for another
        window would strand them; send one short summary instead.
        """

        bucket = self._buckets.get(kind)
        if bucket is None or not bucket.pending or bucket.send is None:
            return
        if bucket.until <= time.time():
            bucket.until = time.time() + 1.0
        self._schedule_flush(kind)

    async def _flush_later(self, kind: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.flush(kind)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("告警汇总任务失败（%s）", kind)
        finally:
            bucket = self._buckets.get(kind)
            if bucket is not None:
                bucket.flush_task = None
                # A new alert that arrived while the summary was being sent
                # has already opened a fresh window; the send blocked this
                # task from scheduling it (it was still running), so pick it
                # up here.
                self._schedule_if_pending(kind)

    async def flush(
        self,
        kind: str | SendFn = "default",
        send: SendFn | None = None,
    ) -> None:
        """Post one summary for everything buffered and close the window."""

        # Backwards compatible call style: ``flush(send_fn)``.
        if callable(kind):
            send, kind = kind, "default"
        key = str(kind or "default")
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        pending, bucket.pending = bucket.pending, []
        bucket.until = 0.0
        current = asyncio.current_task()
        if (
            bucket.flush_task
            and not bucket.flush_task.done()
            and bucket.flush_task is not current
        ):
            bucket.flush_task.cancel()
        sender = send or bucket.send
        if not pending or sender is None:
            return
        lines = [f"⚠️ 告警汇总 · {len(pending)} 项异常", "──────────────"]
        for label, reason in pending[: self.max_items]:
            lines.append(f"• {label}: {reason}")
        extra = len(pending) - self.max_items
        if extra > 0:
            lines.append(f"… 另有 {extra} 项未列出")
        lines.append(
            f"以上 {len(pending)} 项在 {self.window_seconds} 秒聚合窗口内集中出现，"
            "已合并推送；各项均已单独记录，恢复时仍会照常通知。"
        )
        try:
            result = await sender("\n".join(lines))
            ok = bool(getattr(result, "ok", result))
            if ok:
                bucket.retries = 0
                return
            if getattr(result, "uncertain_targets", None):
                # Possibly already in the chat: retrying would duplicate it.
                logger.warning("告警汇总发送未确认（%s，%d 项）", key, len(pending))
                return
            self._requeue(bucket, pending, key, "发送失败")
        except Exception:
            self._requeue(bucket, pending, key, "发送异常")
            logger.exception("告警汇总发送失败（%s）", key)
        finally:
            # Alerts that arrived during the send opened a new window and
            # need a task of their own once this one is no longer "running".
            self._schedule_if_pending(key)

    def _requeue(
        self,
        bucket: _Bucket,
        pending: list[tuple[str, str]],
        key: str,
        why: str,
    ) -> None:
        """Put an undelivered summary back instead of losing the batch.

        A failed summary used to be dropped silently while every rule had
        already latched ``alerting`` -- the user only ever saw the recovery
        notices. Two retries are allowed, then the batch is abandoned with a
        warning so a permanently broken channel cannot loop forever.
        """

        if bucket.retries >= 2:
            logger.warning("告警汇总%s，%d 项放弃重试（%s）", why, len(pending), key)
            return
        bucket.retries += 1
        bucket.pending[:0] = pending
        del bucket.pending[: max(0, len(bucket.pending) - self.max_items * 2)]
        logger.warning(
            "告警汇总%s，%d 项放回队列等待重试（%s，第 %d 次）",
            why,
            len(pending),
            key,
            bucket.retries,
        )

    async def stop(self) -> None:
        """Cancel pending summary tasks (plugin reload / shutdown)."""

        self._stopped = True
        tasks = []
        for bucket in self._buckets.values():
            if bucket.flush_task and not bucket.flush_task.done():
                bucket.flush_task.cancel()
                tasks.append(bucket.flush_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for bucket in self._buckets.values():
            bucket.flush_task = None
