from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import psutil
from astrbot.api import logger

from .collectors import (
    ensure_system_cpu,
    now_in_timezone,
    prime_process_cpu,
    sample_system_cpu,
)
from .state import StateStore


def _message_stats_dir() -> Path | None:
    """Return the installed message_stats data directory when available.

    The plugin intentionally reads another plugin's public JSON data instead
    of registering a global message listener. A global listener would mark
    every group message as "awake" in AstrBot and can change LLM behaviour.
    """

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = (
            Path(get_astrbot_data_path()) / "plugin_data" / "message_stats" / "groups"
        )
        if base.is_dir():
            return base
    except Exception:
        pass

    # Useful for standalone tests on the development machine.
    fallback = (
        Path.home() / ".astrbot" / "data" / "plugin_data" / "message_stats" / "groups"
    )
    return fallback if fallback.is_dir() else None


def read_message_stats_total() -> tuple[int | None, str | None]:
    """Sum per-user message_count from astrbot_plugin_message_stats."""

    base = _message_stats_dir()
    if base is None:
        return None, None

    total = 0
    latest: str | None = None
    try:
        for file in base.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("跳过无法解析的统计文件 %s: %s", file.name, exc)
                continue
            if not isinstance(data, dict):
                continue
            for user in data.get("users", []):
                if not isinstance(user, dict):
                    continue
                try:
                    total += int(user.get("message_count", 0) or 0)
                except (TypeError, ValueError):
                    continue
            updated = data.get("last_updated")
            if isinstance(updated, str) and (latest is None or updated > latest):
                latest = updated
    except Exception as exc:
        logger.debug("读取 message_stats 数据失败: %s", exc)
        return None, None
    return total, latest


class Sampler:
    """Periodic, cheap counter sampler for network, disk IO and message stats."""

    def __init__(
        self,
        state: StateStore,
        interval_seconds: int = 60,
        history_size: int = 60,
        config: Any = None,
    ):
        self.state = state
        self.config = config
        # Keep in sync with the display.sample_seconds slider (15-600).
        self.interval_seconds = max(15, min(600, int(interval_seconds or 60)))
        self.history_size = max(30, int(history_size or 60))
        self._task: asyncio.Task | None = None
        self._proc_task: asyncio.Task | None = None
        self._primed = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever())
            self._task.add_done_callback(self._on_task_done)
        if self._proc_task is None or self._proc_task.done():
            self._proc_task = asyncio.create_task(self._process_cpu_loop())
            self._proc_task.add_done_callback(self._on_task_done)

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("采样任务异常退出: %r", exc, exc_info=exc)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        proc_task = self._proc_task
        self._proc_task = None
        if proc_task and not proc_task.done():
            proc_task.cancel()
            try:
                await proc_task
            except asyncio.CancelledError:
                pass

    async def _process_cpu_loop(self) -> None:
        """Keep fresh system/process CPU snapshots for cards and alerts."""

        try:
            await asyncio.to_thread(prime_process_cpu)
        except Exception:
            logger.exception("初始化进程 CPU 采样失败")
        try:
            await asyncio.to_thread(ensure_system_cpu)
        except Exception:
            logger.exception("初始化系统 CPU 采样失败")

        while True:
            await asyncio.sleep(10)
            try:
                await asyncio.to_thread(prime_process_cpu)
                await asyncio.to_thread(sample_system_cpu)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("刷新 CPU 采样失败")

    def _prime(self) -> None:
        if self._primed:
            return
        now = time.time()
        try:
            counters = psutil.net_io_counters()
        except Exception:
            logger.warning("读取网络计数器失败，网络速率暂按 0 处理", exc_info=True)
            counters = None
        with self.state.section("net") as net:
            net.setdefault("last_ts", now)
            net.setdefault("last_sent", int(counters.bytes_sent) if counters else 0)
            net.setdefault("last_recv", int(counters.bytes_recv) if counters else 0)
            net.setdefault("month", now_in_timezone(self.config).strftime("%Y-%m"))
            net.setdefault("month_up", 0)
            net.setdefault("month_down", 0)
            net.setdefault("history", [])
            net.setdefault("updated_at", now)

        with self.state.section("disk_io") as disk:
            try:
                dio = psutil.disk_io_counters()
                if dio is not None:
                    disk.setdefault("last_ts", now)
                    disk.setdefault("last_read", int(dio.read_bytes))
                    disk.setdefault("last_write", int(dio.write_bytes))
            except Exception:
                pass
            disk.setdefault("read_rate", 0.0)
            disk.setdefault("write_rate", 0.0)

        self.state.setdefault("recv", {})
        self.state.save()
        self._primed = True

    async def run_forever(self) -> None:
        try:
            self._prime()
        except Exception:
            # A restricted container can refuse net_io_counters(); sampled
            # rates then stay at 0, but the loop must keep running.
            logger.exception("采样初始化失败，将继续尝试后续采样")
        # Give the counters a short window so the first card has a real rate.
        await asyncio.sleep(1.0)
        while True:
            try:
                # The sample pass reads the message_stats JSON files from
                # disk; run it off the event loop so large deployments do
                # not stall message handling every interval.
                await asyncio.to_thread(self.sample)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状态采样失败")
            await asyncio.sleep(self.interval_seconds)

    def sample(self) -> None:
        now = time.time()
        self._sample_network(now)
        self._sample_disk_io(now)
        self._sample_message_stats(now)
        self.state.set("sampler_updated_at", now)
        self.state.save()

    def _sample_network(self, now: float) -> None:
        interface = ""
        try:
            with self.state.section("net") as net:
                interface = str(net.get("interface") or "")
        except Exception:
            interface = ""

        counters = None
        try:
            per_nic = psutil.net_io_counters(pernic=True) or {}
        except Exception:
            per_nic = {}
        if interface and interface in per_nic:
            counters = per_nic[interface]
        elif per_nic:
            # Pick the busiest non-loopback interface once and keep it: the
            # card labels the line with a single name, while totalling every
            # interface (docker bridges, loopback) reads about twice as high
            # on a containerised bot.
            best = ""
            best_total = -1
            for name, item in per_nic.items():
                if name.startswith("lo"):
                    continue
                total = int(item.bytes_sent) + int(item.bytes_recv)
                if total > best_total:
                    best, best_total = name, total
            if best:
                interface = best
                counters = per_nic[best]
        if counters is None:
            try:
                counters = psutil.net_io_counters()
                interface = ""
            except Exception:
                logger.debug("读取网络计数器失败，本轮跳过网络采样", exc_info=True)
                return
        if counters is None:
            return
        with self.state.section("net") as net:
            previous_iface = str(net.get("interface") or "")
            switched = bool(interface) and previous_iface not in ("", interface)
            if interface:
                net["interface"] = interface
            last_ts = float(net.get("last_ts") or now)
            last_sent = int(net.get("last_sent") or 0)
            last_recv = int(net.get("last_recv") or 0)

            # A wall-clock step back (NTP correction) would make dt tiny and
            # blow the rate up a millionfold; the interval is >= 15s anyway.
            dt = max(1.0, now - last_ts)
            sent_delta = max(0, int(counters.bytes_sent) - last_sent)
            recv_delta = max(0, int(counters.bytes_recv) - last_recv)

            # A restart leaves last_ts hours behind. The delta then covers the
            # whole downtime: the derived rate is meaningless, and crediting
            # it to the month accumulator would book hours of traffic to the
            # moment of restart (or to the new month, right after a rollover).
            stale = (now - last_ts) > max(300.0, self.interval_seconds * 5)
            if switched:
                # The counters moved to a different scale; this round only
                # establishes the new baseline.
                sent_delta = 0
                recv_delta = 0
                stale = False
            up_rate = 0.0 if stale else sent_delta / dt
            down_rate = 0.0 if stale else recv_delta / dt

            month = now_in_timezone(self.config).strftime("%Y-%m")
            net["interval"] = self.interval_seconds
            if net.get("month") != month:
                net["month"] = month
                net["month_up"] = 0
                net["month_down"] = 0
                logger.info("已进入 %s，本月流量统计已重置", month)
            if not stale:
                net["month_up"] = int(net.get("month_up") or 0) + sent_delta
                net["month_down"] = int(net.get("month_down") or 0) + recv_delta

                history = net.get("history")
                if not isinstance(history, list):
                    history = []
                history.append([now, round(up_rate, 1), round(down_rate, 1)])
                net["history"] = history[-self.history_size :]

            net["last_ts"] = now
            net["last_sent"] = int(counters.bytes_sent)
            net["last_recv"] = int(counters.bytes_recv)
            net["up_rate"] = up_rate
            net["down_rate"] = down_rate
            net["updated_at"] = now

    def _sample_disk_io(self, now: float) -> None:
        try:
            counters = psutil.disk_io_counters()
        except Exception:
            counters = None
        if counters is None:
            return
        with self.state.section("disk_io") as disk:
            last_ts = float(disk.get("last_ts") or now)
            last_read = int(disk.get("last_read") or 0)
            last_write = int(disk.get("last_write") or 0)
            # See _sample_network: keep the rate sane across clock steps.
            dt = max(1.0, now - last_ts)
            read_rate = max(0, int(counters.read_bytes) - last_read) / dt
            write_rate = max(0, int(counters.write_bytes) - last_write) / dt
            disk["last_ts"] = now
            disk["last_read"] = int(counters.read_bytes)
            disk["last_write"] = int(counters.write_bytes)
            disk["read_rate"] = read_rate
            disk["write_rate"] = write_rate
            disk["updated_at"] = now

    def _sample_message_stats(self, now: float) -> None:
        total, source_updated = read_message_stats_total()
        with self.state.section("recv") as recv:
            recv["source"] = "message_stats"
            recv["scope"] = "message_stats 跟踪群"
            if total is None:
                recv.setdefault("baseline", 0)
                recv.setdefault("accum", 0)
                recv.setdefault("last_total", None)
                recv["available"] = False
                recv["updated_at"] = now
                return

            last_total = recv.get("last_total")
            if not isinstance(last_total, int):
                recv["baseline"] = total
                recv["baseline_at"] = now
            elif total < last_total:
                # The source plugin was reset or its data directory was rebuilt.
                recv["baseline"] = total
                recv["baseline_at"] = now
                recv["accum"] = 0

            baseline = int(recv.get("baseline") or 0)
            recv["last_total"] = total
            recv["accum"] = max(0, total - baseline)
            recv["available"] = True
            recv["source_updated_at"] = source_updated
            recv["updated_at"] = now
