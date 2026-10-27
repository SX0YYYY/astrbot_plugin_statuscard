"""Merge bursts of alerts into one summary after the first one delivers.

A real outage fans out: one tunnel goes down and four HTTP monitors fail at
once, a full disk trips the disk rule and the load rule together. The first
alert of an episode still goes out immediately, but further alerts that
arrive while the window is open are merged into a single summary message
instead of posting a burst. Every item is still recorded as an event by its
own rule, so the daily report and the event log stay complete; a window of
0 disables the behaviour entirely.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

SendFn = Callable[[str], Awaitable[Any]]


class AlertAggregator:
    """Collect follow-up alerts for a short window and post one summary."""

    def __init__(self, window_seconds: int = 0, max_items: int = 8) -> None:
        self.window_seconds = max(0, int(window_seconds))
        self.max_items = max(2, min(20, int(max_items)))
        self._until = 0.0
        self._pending: list[tuple[str, str]] = []
        self._flush_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        """True while follow-up alerts should be buffered, not sent."""

        return self.window_seconds > 0 and time.time() < self._until

    async def push(
        self,
        label: str,
        reason: str,
        text: str,
        send: SendFn,
    ) -> Any | None:
        """Deliver ``text`` now, or buffer it for the window summary.

        Returns the delivery result when the caller must treat this as its
        own send (the first alert of a window, or aggregation disabled);
        returns ``None`` when the item was buffered and will be covered by
        the summary.
        """

        if not self.active:
            result = await send(text)
            if self.window_seconds > 0:
                self._until = time.time() + self.window_seconds
                self._pending = [(label, reason)]
                self._schedule_flush(send)
            return result
        self._pending.append((label, reason))
        del self._pending[: max(0, len(self._pending) - self.max_items * 2)]
        return None

    def _schedule_flush(self, send: SendFn) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        delay = max(1.0, self._until - time.time())
        self._flush_task = asyncio.create_task(self._flush_later(delay, send))

    async def _flush_later(self, delay: float, send: SendFn) -> None:
        await asyncio.sleep(delay)
        await self.flush(send)

    async def flush(self, send: SendFn) -> None:
        """Post one summary for everything buffered and close the window."""

        pending, self._pending = self._pending, []
        self._until = 0.0
        if not pending:
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
            result = await send("\n".join(lines))
            ok = bool(getattr(result, "ok", result))
            uncertain = bool(getattr(result, "uncertain_targets", []))
            if not ok:
                logger.warning("告警汇总发送未确认或失败（%d 项）", len(pending))
                del uncertain
        except Exception:
            logger.exception("告警汇总发送失败")
