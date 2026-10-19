from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import psutil

from .collectors import (
    cached_cpu_percent,
    cfg_bool,
    cfg_get,
    cfg_int,
    collect_bot_info,
    memory_stat,
    now_in_timezone,
    swap_stat,
)
from .report_store import ReportStore
from .sampler import read_message_stats_total
from .state import StateStore

logger = logging.getLogger("astrbot_plugin_statuscard")


class ReportCollector:
    """Collect the small amount of history a daily report needs.

    Phase 1 only records data: resource samples, one daily QQ/receive snapshot,
    and alert/service events. It does not send anything.
    """

    def __init__(
        self,
        plugin: Any,
        state: StateStore,
        config: Any,
        store: ReportStore,
    ):
        self.plugin = plugin
        self.state = state
        self.config = config
        self.store = store
        self.resource_interval = max(
            60,
            min(
                1800,
                cfg_int(config, "daily_report.resource_sample_seconds", 300),
            ),
        )
        self.retention_days = max(
            7, min(365, cfg_int(config, "daily_report.retention_days", 30))
        )
        self.event_retention_days = max(
            30,
            min(
                730,
                cfg_int(config, "daily_report.event_retention_days", 90),
            ),
        )
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_resource = 0.0
        self._last_prune = 0.0
        self._last_snapshot_attempt = 0.0

    def start(self) -> None:
        if not cfg_bool(self.config, "daily_report.collect", True):
            logger.info("日报数据收集未启用（daily_report.collect=false）")
            return
        if not self.store.available:
            logger.warning("日报数据库不可用，跳过历史数据收集")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "日报数据底座已启动：资源采样 %ss，资源保留 %s 天，事件保留 %s 天",
            self.resource_interval,
            self.retention_days,
            self.event_retention_days,
        )

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # Sampler seeds the system CPU snapshot asynchronously; a short wait
        # avoids recording a misleading 0% first resource sample.
        await asyncio.sleep(2)
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("日报数据采集失败")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        now = time.time()
        if now - self._last_resource >= self.resource_interval:
            await self._sample_resource(now)
            self._last_resource = now
        await self._maybe_snapshot()
        if now - self._last_prune >= 86400:
            await asyncio.to_thread(
                self.store.prune,
                self.retention_days,
                self.event_retention_days,
                int(now),
            )
            self._last_prune = now

    async def _sample_resource(self, now: float) -> None:
        cpu = cached_cpu_percent()
        mem = memory_stat()
        swap = swap_stat()
        disk_pct: float | None = None
        try:
            disk_paths = cfg_get(self.config, "display.disk_paths", None)
            if isinstance(disk_paths, list) and disk_paths:
                disk_path = str(disk_paths[0])
            else:
                disk_path = os.path.abspath(os.sep)
            disk_pct = float(psutil.disk_usage(disk_path).percent)
        except Exception:
            disk_pct = None
        load1: float | None = None
        if hasattr(os, "getloadavg"):
            try:
                load1 = float(os.getloadavg()[0])
            except Exception:
                load1 = None
        net = self.state.get("net", {}) or {}
        disk_io = self.state.get("disk_io", {}) or {}
        await asyncio.to_thread(
            self.store.add_resource,
            int(now),
            float(cpu),
            float(mem.percent),
            float(swap.percent),
            disk_pct,
            load1,
            float(net.get("up_rate") or 0.0),
            float(net.get("down_rate") or 0.0),
            float(disk_io.get("read_rate") or 0.0),
            float(disk_io.get("write_rate") or 0.0),
        )

    async def _maybe_snapshot(self) -> None:
        now = now_in_timezone(self.config)
        day = now.strftime("%Y-%m-%d")
        if now.hour == 0 and now.minute < 5:
            return
        if self.store.snapshot_complete(day):
            return
        if time.time() - self._last_snapshot_attempt < 300:
            return
        self._last_snapshot_attempt = time.time()

        recv_total, _ = await asyncio.to_thread(read_message_stats_total)
        recv_state = self.state.get("recv", {}) or {}
        bot = await collect_bot_info(
            self.plugin.context, None, self.config, recv_state
        )
        friends = bot.get("friends")
        groups = bot.get("groups")
        members = bot.get("members")
        complete = all(
            value is not None for value in (friends, groups, members)
        )
        await asyncio.to_thread(
            self.store.record_snapshot,
            day,
            friends,
            groups,
            members,
            recv_total,
            complete,
            int(time.time()),
        )
        if complete:
            logger.info(
                "日报每日快照已记录：%s（好友=%s，群=%s，群员=%s，接收累计=%s）",
                day,
                friends,
                groups,
                members,
                recv_total,
            )
        else:
            logger.warning(
                "日报每日快照不完整：%s（OneBot 或 message_stats 暂不可用，稍后重试）",
                day,
            )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._running,
            "resource_interval": self.resource_interval,
            "retention_days": self.retention_days,
            "event_retention_days": self.event_retention_days,
            "store": self.store.stats(),
        }
