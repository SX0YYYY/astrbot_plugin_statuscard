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

from .utils import retry_delay

SendFn = Callable[[str], Awaitable[Any]]


@dataclass
class _Bucket:
    """One notification kind's open aggregation window."""

    until: float = 0.0
    pending: list[tuple[str, str]] = field(default_factory=list)
    flush_task: asyncio.Task | None = None
    send: SendFn | None = None
    retries: int = 0
    # Earliest time a failed summary may be retried. A 1-second retry loop
    # never survived a real channel outage, while the single-alert path waits
    # through the same backoff schedule -- the summary must too.
    retry_at: float = 0.0
    # Overflow items trimmed off the buffer. They are withdrawn (never
    # delivered), but the summary must still count them honestly.
    dropped_count: int = 0
    # Called with the batch when a summary is abandoned for good. The caller
    # then withdraws "alerted" so the user does not receive a recovery notice
    # for an alert that never reached the chat.
    on_abandon: Callable[[list[tuple[str, str]]], None] | None = None
    # The first alert of this window is still being sent. A slow channel can
    # take longer than the window, and the summary must not appear before the
    # alert it summarizes.
    first_inflight: bool = False
    first_started: float = 0.0


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

    def window_remaining(self, kind: str) -> float:
        """Seconds until the open window for ``kind`` closes (0 if none).

        Queued recovery notices use this to schedule themselves *after* the
        summary: a fixed 15-second delay raced the flush whenever the
        sampling interval was shorter than the window.
        """

        bucket = self._buckets.get(str(kind))
        if bucket is None:
            return 0.0
        return max(0.0, bucket.until - time.time())

    def _bucket(self, kind: str) -> _Bucket:
        return self._buckets.setdefault(str(kind), _Bucket())

    async def push(
        self,
        label: str,
        reason: str,
        text: str,
        send: SendFn,
        kind: str = "default",
        on_abandon: Callable[[list[tuple[str, str]]], None] | None = None,
    ) -> Any | None:
        """Deliver ``text`` now, or buffer it for the window summary.

        Returns the delivery result when the caller must treat this as its
        own send (the first alert of a window, or aggregation disabled);
        returns ``None`` when the item was buffered and will be covered by
        the summary.

        ``on_abandon`` is invoked with the buffered batch if the summary can
        never be delivered, so the owning rules can stop claiming the user was
        notified.
        """

        key = str(kind or "default")
        bucket = self._bucket(key)
        if on_abandon is not None:
            bucket.on_abandon = on_abandon
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
                bucket.first_inflight = True
                bucket.first_started = time.time()
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
                    dropped = len(bucket.pending)
                    if dropped:
                        orphaned = list(bucket.pending)
                        bucket.pending.clear()
                        logger.warning(
                            "首条告警发送异常，已丢弃 %d 项缓冲（%s）",
                            dropped,
                            key,
                        )
                        # Those rules will never be covered by a summary:
                        # withdraw "alerted" or their recovery notice claims
                        # an alert the user never saw.
                        if bucket.on_abandon is not None:
                            bucket.on_abandon(orphaned)
                    else:
                        bucket.pending.clear()
                raise
            finally:
                bucket.first_inflight = False
        bucket.pending.append((label, reason))
        # Keep a bounded buffer (max_items are listed, the rest are counted).
        self._trim_pending(bucket, key)
        # A summary send in progress blocks _schedule_flush; without this the
        # alerts that arrive afterwards sit in pending with no task to flush
        # them (the send's finally only sees a buffer that was empty then).
        self._schedule_if_pending(key)
        return None

    def _trim_pending(self, bucket: _Bucket, key: str) -> list[tuple[str, str]]:
        """Drop the oldest overflows and account for them honestly.

        Dropped items can never reach the chat, so their rules must not claim
        "notified"; the summary text still counts them (``另有 N 项未列出``),
        otherwise the event log shows more failures than the message lists.
        """

        overflow = len(bucket.pending) - self.max_items * 2
        if overflow <= 0:
            return []
        dropped = bucket.pending[:overflow]
        del bucket.pending[:overflow]
        bucket.dropped_count += len(dropped)
        if bucket.on_abandon is not None:
            bucket.on_abandon(dropped)
        logger.warning(
            "聚合缓冲已满，丢弃最早 %d 项汇总（%s，放弃后按未送达处理）",
            len(dropped),
            key,
        )
        return dropped

    def _schedule_flush(self, kind: str, delay: float | None = None) -> None:
        if self._stopped:
            return
        bucket = self._buckets.get(kind)
        if bucket is None or bucket.send is None:
            return
        if bucket.flush_task and not bucket.flush_task.done():
            return
        if delay is None:
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
        now = time.time()
        if bucket.until <= now:
            bucket.until = now + 1.0
        if bucket.retry_at > now:
            # A failed summary is inside its backoff window. Retrying every
            # second let a short channel blip exhaust all attempts before the
            # channel recovered; wait as long as the single-alert path does.
            self._schedule_flush(kind, bucket.retry_at - now)
            return
        self._schedule_flush(kind)

    async def _flush_later(self, kind: str, delay: float) -> None:
        cancelled = False
        try:
            await asyncio.sleep(delay)
            await self.flush(kind)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("告警汇总任务失败（%s）", kind)
        finally:
            bucket = self._buckets.get(kind)
            if bucket is not None:
                bucket.flush_task = None
                # A cancelled task must not reschedule itself: during event
                # loop shutdown each new task would be cancelled again and
                # spawn the next one, never draining. A stop() sets the flag
                # anyway; this guards plain task cancellation too.
                if not cancelled and not self._stopped:
                    # A new alert that arrived while the summary was being
                    # sent has already opened a fresh window; the send
                    # blocked this task from scheduling it (it was still
                    # running), so pick it up here.
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
        if bucket.first_inflight and time.time() - bucket.first_started < 60:
            # The first alert is still in flight (a slow channel can exceed
            # the aggregation window). Posting the summary now would show it
            # before the alert it summarizes; wait a little. The 60s cap keeps
            # a hung send from holding the buffer forever.
            self._schedule_flush(key, 5.0)
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
        if not pending:
            return
        if sender is None:
            # The first send raised before the window could be reused: the
            # buffered items would otherwise be dropped without a trace while
            # their rules stay latched (the user gets a recovery, never an
            # alert).
            logger.warning(
                "告警汇总缺少发送函数，丢弃 %d 项缓冲（%s）", len(pending), key
            )
            if bucket.on_abandon is not None:
                bucket.on_abandon(list(pending))
            return
        total = len(pending) + bucket.dropped_count
        lines = [f"⚠️ 告警汇总 · {total} 项异常", "──────────────"]
        for label, reason in pending[: self.max_items]:
            lines.append(f"• {label}: {reason}")
        extra = total - self.max_items
        if extra > 0:
            lines.append(f"… 另有 {extra} 项未列出")
        lines.append(
            f"以上 {total} 项在 {self.window_seconds} 秒聚合窗口内集中出现，"
            "已合并推送；各项均已单独记录，恢复时按正常流程通知。"
        )
        try:
            result = await sender("\n".join(lines))
            ok = bool(getattr(result, "ok", result))
            if ok:
                bucket.retries = 0
                bucket.retry_at = 0.0
                bucket.dropped_count = 0
                return
            if getattr(result, "uncertain_targets", None):
                # Possibly already in the chat: retrying would duplicate it.
                logger.warning("告警汇总发送未确认（%s，%d 项）", key, len(pending))
                # The episode is over either way: a fresh window must start
                # with a clean retry budget and counter, or the next summary
                # inherits this one's leftovers.
                bucket.retries = 0
                bucket.retry_at = 0.0
                bucket.dropped_count = 0
                return
            self._requeue(bucket, pending, key, "发送失败")
        except asyncio.CancelledError:
            # Reload/shutdown while the summary was in flight: the batch was
            # already taken from bucket.pending, so put it back before
            # propagating -- stop() would otherwise see an empty buffer and
            # the rules would keep claiming "notified" after the restart.
            self._requeue(bucket, pending, key, "任务被取消")
            raise
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
        notices. Retries follow the same backoff as the single-alert path
        (about 30s, then 120s) so a short channel blip no longer burns the
        whole budget before the channel recovers; two retries are still the
        limit, then the batch is abandoned with a warning.
        """

        if bucket.retries >= 2:
            logger.warning("告警汇总%s，%d 项放弃重试（%s）", why, len(pending), key)
            if bucket.on_abandon is not None:
                bucket.on_abandon(list(pending))
            # Reset the bucket: without this, the next episode's summary
            # inherits retries=2 and gives up on its FIRST failure (zero
            # retries for the rest of the process), and dropped_count leaks
            # into its totals.
            bucket.retries = 0
            bucket.retry_at = 0.0
            bucket.dropped_count = 0
            return
        bucket.retries += 1
        bucket.retry_at = time.time() + retry_delay(bucket.retries)
        bucket.pending[:0] = pending
        self._trim_pending(bucket, key)
        logger.warning(
            "告警汇总%s，%d 项放回队列等待重试（%s，第 %d 次，约 %d 秒后）",
            why,
            len(pending),
            key,
            bucket.retries,
            round(bucket.retry_at - time.time()),
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
            if bucket.pending:
                # Reload/shutdown: these items were never posted. Let their
                # rules know so the next recovery is not announced as if the
                # alert had arrived.
                if bucket.on_abandon is not None:
                    bucket.on_abandon(list(bucket.pending))
                bucket.pending.clear()
