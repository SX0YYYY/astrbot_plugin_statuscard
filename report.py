from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import timedelta
from typing import Any

import psutil
from astrbot.api import logger

from .collectors import (
    cached_cpu_percent,
    cfg_bool,
    cfg_get,
    cfg_int,
    collect_bot_info,
    connection_test,
    memory_stat,
    now_in_timezone,
    swap_stat,
)
from .i18n import tr
from .report_store import ReportStore
from .report_view import (
    DEFAULT_SECTIONS,
    VALID_SECTIONS,
    DailyReportBuilder,
    build_report_text,
)
from .sampler import read_message_stats_total
from .state import StateStore
from .utils import normalize_targets
from .version import resolve_astrbot_version


def _collect_process_samples(top_n: int) -> dict[str, list[dict[str, Any]]]:
    """Return separate CPU and memory top lists from one process snapshot."""

    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            proc.cpu_percent(None)
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # The first call only seeds the per-process CPU baseline; a short gap is
    # enough for a five-minute sampling interval.
    time.sleep(0.5)
    items: list[dict[str, Any]] = []
    for proc in procs:
        try:
            info = proc.info
            pid = int(info.get("pid") or 0)
            name = str(info.get("name") or f"pid {pid}")[:80]
            cpu = float(proc.cpu_percent(None) or 0.0)
            mem_mb = float(proc.memory_info().rss or 0) / 1048576.0
            items.append({"pid": pid, "name": name, "cpu": cpu, "mem_mb": mem_mb})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not items:
        return {}

    def ranked(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"rank": index + 1, **item} for index, item in enumerate(values[:top_n])
        ]

    return {
        "cpu": ranked(sorted(items, key=lambda item: item["cpu"], reverse=True)),
        "mem": ranked(sorted(items, key=lambda item: item["mem_mb"], reverse=True)),
    }


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
        self._last_website = 0.0
        self._last_process = 0.0
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
            "日报数据底座已启动：资源/网站/进程采样 %ss，资源保留 %s 天，"
            "事件保留 %s 天",
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
        if now - self._last_website >= self.resource_interval:
            await self._sample_websites(now)
            self._last_website = now
        if now - self._last_process >= self.resource_interval:
            await self._sample_processes(now)
            self._last_process = now
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

    async def _sample_websites(self, now: float) -> None:
        try:
            targets = cfg_get(self.config, "display.website_targets", None)
            results = await connection_test(targets, cache_seconds=0)
            samples = [
                {
                    "name": item.name,
                    "ok": item.status != "ERR",
                    "latency_ms": item.delay,
                    "status": item.status,
                }
                for item in results
            ]
            await asyncio.to_thread(self.store.add_website_samples, int(now), samples)
        except Exception:
            logger.exception("网站延迟采样失败")

    async def _sample_processes(self, now: float) -> None:
        top_n = max(
            3,
            min(
                10,
                cfg_int(self.config, "daily_report.process_top_n", 5),
            ),
        )
        try:
            samples = await asyncio.to_thread(_collect_process_samples, top_n)
        except Exception:
            logger.exception("Top 进程采样失败")
            return
        for metric, rows in samples.items():
            if rows:
                await asyncio.to_thread(
                    self.store.add_process_samples,
                    int(now),
                    metric,
                    rows,
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
        bot = await collect_bot_info(self.plugin.context, None, self.config, recv_state)
        friends = bot.get("friends")
        groups = bot.get("groups")
        members = bot.get("members")
        complete = all(value is not None for value in (friends, groups, members))
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
            "process_top_n": max(
                3,
                min(
                    10,
                    cfg_int(self.config, "daily_report.process_top_n", 5),
                ),
            ),
            "store": self.store.stats(),
        }


class DailyReporter:
    """Scheduled daily report builder, sender and manual preview entry."""

    def __init__(self, plugin: Any, store: ReportStore, config: Any):
        self.plugin = plugin
        self.store = store
        self.config = config
        self.builder = DailyReportBuilder(store, config)
        self._task: asyncio.Task | None = None
        self._running = False
        self._pending_retry: tuple[str, float] | None = None
        self._pending_retry_count = 0
        self._pending_retry_day: str | None = None
        self._next_run_text: str | None = None
        self._disabled_logged = False
        self._weekly_task: asyncio.Task | None = None
        self._weekly_running = False
        self._weekly_pending: float | None = None
        self._weekly_retry_count = 0
        self._weekly_next_run_text: str | None = None
        self._last_sent_day: str | None = None
        self._weekly_last_sent_week: str | None = None
        # Retry plans and de-dup markers used to live only in memory, so a
        # plugin reload (which every config save triggers) dropped a queued
        # retry — the weekly report could silently vanish for the whole week.
        # They are persisted through state.json and restored here.
        self._restore_delivery_state()

    # ------------------------------------------------------------------
    # persisted delivery state
    # ------------------------------------------------------------------

    def _restore_delivery_state(self) -> None:
        state = getattr(getattr(self, "plugin", None), "state", None)
        saved: Any = {}
        try:
            if state is not None:
                saved = state.get("report_delivery", {}) or {}
        except Exception:
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        self._last_sent_day = saved.get("last_sent_day") or None
        self._weekly_last_sent_week = saved.get("weekly_last_sent_week") or None
        self._pending_retry_count = int(saved.get("pending_retry_count") or 0)
        self._pending_retry_day = saved.get("pending_retry_day") or None
        self._weekly_retry_count = int(saved.get("weekly_retry_count") or 0)
        pending = saved.get("pending_retry")
        if isinstance(pending, dict) and pending.get("day") is not None:
            try:
                self._pending_retry = (
                    str(pending["day"]),
                    float(pending.get("due") or 0),
                )
            except (TypeError, ValueError):
                self._pending_retry = None
        else:
            self._pending_retry = None
        weekly_pending = saved.get("weekly_pending")
        try:
            self._weekly_pending = float(weekly_pending) if weekly_pending else None
        except (TypeError, ValueError):
            self._weekly_pending = None

    def _save_delivery_state(self) -> None:
        state = getattr(getattr(self, "plugin", None), "state", None)
        if state is None or not hasattr(state, "set"):
            return
        pending = None
        if self._pending_retry is not None:
            pending = {
                "day": self._pending_retry[0],
                "due": self._pending_retry[1],
            }
        try:
            state.set(
                "report_delivery",
                {
                    "last_sent_day": self._last_sent_day,
                    "pending_retry": pending,
                    "pending_retry_count": self._pending_retry_count,
                    "pending_retry_day": self._pending_retry_day,
                    "weekly_last_sent_week": self._weekly_last_sent_week,
                    "weekly_pending": self._weekly_pending,
                    "weekly_retry_count": self._weekly_retry_count,
                },
            )
            state.save()
        except Exception:
            logger.debug("持久化日报投递状态失败", exc_info=True)

    # ------------------------------------------------------------------
    # configuration helpers (read per call so edits apply after reload)
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        return cfg_bool(self.config, "daily_report.enabled", False)

    def _with_image(self) -> bool:
        return cfg_bool(self.config, "daily_report.with_image", True)

    def _time_text(self) -> str:
        return str(
            cfg_get(self.config, "daily_report.time", "21:00") or "21:00"
        ).strip()

    def _history_days(self) -> int:
        return max(7, min(30, cfg_int(self.config, "daily_report.history_days", 7)))

    def _weekly_enabled(self) -> bool:
        return cfg_bool(self.config, "daily_report.weekly_enabled", False)

    def _weekly_time_text(self) -> str:
        return str(
            cfg_get(self.config, "daily_report.weekly_time", "21:30") or "21:30"
        ).strip()

    def _sections(self) -> list[str] | None:
        """Return selected sections, or None when the key is unset.

        ``None`` means "use the built-in defaults"; an empty list means the
        user explicitly cleared every module, so nothing should be rendered
        or sent.
        """

        raw = cfg_get(self.config, "daily_report.sections", None)
        if raw is None:
            return list(DEFAULT_SECTIONS)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return list(DEFAULT_SECTIONS)
        allowed = {str(item) for item in raw if isinstance(item, str)}
        return [item for item in VALID_SECTIONS if item in allowed]

    def _targets(self) -> list[str]:
        raw = cfg_get(self.config, "daily_report.targets", [])
        if not raw:
            raw = cfg_get(self.config, "alerts.targets", [])
        platform_id = str(
            cfg_get(self.config, "alerts.platform_id", "default") or "default"
        )
        return normalize_targets(raw, platform_id)

    def _build_view(self, day=None, span_days: int = 1) -> dict[str, Any]:
        meta = {
            "host": socket.gethostname(),
            "astrbot_version": resolve_astrbot_version(
                getattr(self.plugin, "context", None)
            ),
        }
        return self.builder.build(
            day=day,
            history_days=self._history_days(),
            sections=self._sections(),
            meta=meta,
            span_days=span_days,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _compute_next_run(self) -> str | None:
        """Return the next scheduled send time, or None if config is invalid."""

        try:
            time_text = self._time_text()
            hour, minute = [int(part) for part in time_text.split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            now = now_in_timezone(self.config)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return None

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("日报定时任务异常退出: %r", exc, exc_info=exc)

    def start(self) -> None:
        if not self._enabled():
            logger.info("日报定时发送未启用（daily_report.enabled=false）")
            return
        targets = self._targets()
        if not targets:
            logger.warning("日报已启用，但推送目标为空；发送时会跳过")
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)
        self._next_run_text = self._compute_next_run()
        if self._next_run_text:
            logger.info("日报将在 %s 发送", self._next_run_text)
        logger.info(
            "日报定时发送已启动：%s，目标 %d 个，内容=%s，历史=%d 天",
            self._time_text(),
            len(targets),
            self._sections() or [],
            self._history_days(),
        )
        self._start_weekly()

    def _start_weekly(self) -> None:
        if not self._weekly_enabled():
            logger.info("周报未启用（daily_report.weekly_enabled=false）")
            return
        if self._weekly_running:
            return
        self._weekly_running = True
        self._weekly_task = asyncio.create_task(self._weekly_loop())
        self._weekly_task.add_done_callback(self._on_weekly_task_done)
        self._weekly_next_run_text = self._compute_next_weekly_run()
        if self._weekly_next_run_text:
            logger.info("周报将在 %s 发送", self._weekly_next_run_text)

    def _compute_next_weekly_run(self) -> str | None:
        """Next Sunday at ``daily_report.weekly_time`` in display timezone."""

        try:
            time_text = self._weekly_time_text()
            hour, minute = [int(part) for part in time_text.split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
        except Exception:
            return None
        now = now_in_timezone(self.config)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        target += timedelta(days=(6 - target.weekday()) % 7)
        if target <= now:
            target += timedelta(days=7)
        return target.strftime("%Y-%m-%d %H:%M")

    def _on_weekly_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("周报定时任务异常退出: %r", exc, exc_info=exc)

    async def stop(self) -> None:
        self._running = False
        self._weekly_running = False
        for attr in ("_task", "_weekly_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _loop(self) -> None:
        last_logged = None
        warned = False
        while self._running:
            try:
                if not self._enabled():
                    if not self._disabled_logged:
                        logger.warning("日报定时任务暂停：daily_report.enabled=false")
                        self._disabled_logged = True
                    self._clear_retry()
                    await asyncio.sleep(60)
                    continue
                self._disabled_logged = False
                if self._pending_retry:
                    _day, due = self._pending_retry
                    if time.time() < due:
                        await asyncio.sleep(60)
                        continue
                    self._pending_retry = None
                    await self._send_scheduled()
                    await asyncio.sleep(60)
                    continue
                time_text = self._time_text()
                try:
                    hour, minute = [int(part) for part in time_text.split(":", 1)]
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError("out of range")
                    warned = False
                except Exception:
                    if not warned:
                        logger.warning(
                            "日报发送时间格式非法：%s（应为 HH:MM）",
                            time_text,
                        )
                        warned = True
                    await asyncio.sleep(60)
                    continue
                tz_name = str(
                    cfg_get(self.config, "display.timezone", "Asia/Shanghai")
                    or "Asia/Shanghai"
                )
                now = now_in_timezone(self.config)
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                if last_logged != target:
                    self._next_run_text = target.strftime("%Y-%m-%d %H:%M")
                    logger.info(
                        "日报将在 %s 发送（%.0f 秒后）",
                        self._next_run_text,
                        max(1.0, (target - now).total_seconds()),
                    )
                    last_logged = target
                wait = max(1.0, (target - now).total_seconds())
                if wait > 60:
                    await asyncio.sleep(60)
                    continue
                await asyncio.sleep(wait)
                if not self._enabled():
                    continue
                if (
                    self._time_text() != time_text
                    or str(
                        cfg_get(self.config, "display.timezone", "Asia/Shanghai")
                        or "Asia/Shanghai"
                    )
                    != tz_name
                ):
                    continue
                # A fresh scheduled run starts a new attempt series, so the
                # retry counter of a previous evening must not carry over.
                self._pending_retry_count = 0
                self._pending_retry_day = self._today()
                self._save_delivery_state()
                await self._send_scheduled()
                last_logged = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("日报定时任务失败")
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    async def _weekly_loop(self) -> None:
        last_logged = None
        while self._weekly_running:
            try:
                if not self._weekly_enabled():
                    self._weekly_next_run_text = None
                    await asyncio.sleep(60)
                    continue
                if self._weekly_pending is not None:
                    if time.time() < self._weekly_pending:
                        await asyncio.sleep(60)
                        continue
                    self._weekly_pending = None
                    await self._send_weekly_scheduled()
                    await asyncio.sleep(60)
                    continue
                try:
                    hour, minute = [
                        int(part) for part in self._weekly_time_text().split(":", 1)
                    ]
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError("out of range")
                except Exception:
                    logger.warning(
                        "周报发送时间格式非法：%s（应为 HH:MM）",
                        self._weekly_time_text(),
                    )
                    await asyncio.sleep(60)
                    continue
                now = now_in_timezone(self.config)
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                target += timedelta(days=(6 - target.weekday()) % 7)
                if target <= now:
                    target += timedelta(days=7)
                if last_logged != target:
                    self._weekly_next_run_text = target.strftime("%Y-%m-%d %H:%M")
                    logger.info(
                        "周报将在 %s 发送（%.0f 秒后）",
                        self._weekly_next_run_text,
                        max(1.0, (target - now).total_seconds()),
                    )
                    last_logged = target
                wait = max(1.0, (target - now).total_seconds())
                if wait > 60:
                    await asyncio.sleep(60)
                    continue
                await asyncio.sleep(wait)
                await self._send_weekly_scheduled()
                last_logged = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("周报定时任务失败")
                await asyncio.sleep(60)

    def _schedule_weekly_retry(self) -> bool:
        self._weekly_retry_count += 1
        if self._weekly_retry_count > 3:
            self._weekly_pending = None
            self._weekly_retry_count = 0
            self._save_delivery_state()
            logger.warning("周报连续发送失败 3 次，停止重试")
            return False
        delay = 600 * self._weekly_retry_count
        self._weekly_pending = time.time() + delay
        self._save_delivery_state()
        logger.warning(
            "周报发送失败，%d 分钟后重试（第 %d 次）",
            delay // 60,
            self._weekly_retry_count,
        )
        return True

    async def _send_weekly_scheduled(self) -> None:
        if self._sections() is not None and not self._sections():
            logger.warning("周报内容为空（daily_report.sections=[]），跳过发送")
            return
        try:
            view = self._build_view(span_days=7)
        except Exception:
            logger.exception("周报数据聚合失败")
            if not self._schedule_weekly_retry():
                await self._notify_give_up(self._targets(), "--", weekly=True)
            return
        iso = now_in_timezone(self.config).date().isocalendar()
        week_key = f"{iso.year}-W{iso.week:02d}"
        if self._weekly_last_sent_week == week_key:
            logger.info("周报 %s 已发送过，跳过重复发送", week_key)
            return
        targets = self._targets()
        if not targets:
            logger.warning("周报推送目标为空，跳过发送")
            self._schedule_weekly_retry()
            return
        day = str(view.get("day") or "")
        if self._with_image():
            image_url = None
            try:
                image_url = await self.plugin.render_report_image(view)
            except Exception:
                logger.exception("周报渲染失败，回退文字摘要")
            if image_url and await self.plugin.send_image(
                targets, image_url, source="weekly_report"
            ):
                self._weekly_last_sent_week = week_key
                self._record(day, True, "weekly-image")
                self._weekly_pending = None
                self._weekly_retry_count = 0
                self._save_delivery_state()
                logger.info("周报已发送（图片）：%s", targets)
                return
        if await self.plugin.send_text(
            targets, build_report_text(view), source="weekly_report"
        ):
            self._weekly_last_sent_week = week_key
            self._record(day, True, "weekly-text")
            self._weekly_pending = None
            self._weekly_retry_count = 0
            self._save_delivery_state()
            logger.info("周报已发送（文字）：%s", targets)
        else:
            self._record(day, False, "weekly send failed")
            logger.warning("周报发送失败：%s", targets)
            if not self._schedule_weekly_retry():
                await self._notify_give_up(targets, day, weekly=True)

    async def _notify_give_up(
        self, targets: list[str], day: str, weekly: bool = False
    ) -> None:
        """Best-effort notice when all retries are exhausted.

        The push channel itself just failed, so this may also fail; it is
        still worth one attempt before the report silently waits for the
        next scheduled slot.
        """

        if not targets:
            return
        key = "weekly_retry_give_up" if weekly else "report_retry_give_up"
        try:
            await self.plugin.send_text(
                targets, tr(self.config, key, day=day or "--"), source="report_give_up"
            )
        except Exception:
            logger.debug("发送放弃重试提示失败", exc_info=True)

    def _schedule_retry(self, day: str) -> bool:
        """Remember a failed delivery and queue the next attempt.

        Returns ``True`` when another attempt was scheduled and ``False``
        when the retry budget for today is exhausted. The counter is scoped
        to a single day; without that scope a few unlucky evenings in a row
        would push the counter past the limit and every later evening would
        be abandoned after its very first failure.
        """

        today = self._today()
        if self._pending_retry_day != today:
            self._pending_retry_day = today
            self._pending_retry_count = 0
        self._pending_retry_count += 1
        if self._pending_retry_count > 3:
            self._pending_retry = None
            self._pending_retry_count = 0
            self._save_delivery_state()
            logger.warning("日报 %s 连续发送失败 3 次，停止重试", day or "--")
            return False
        delay = 600 * self._pending_retry_count
        self._pending_retry = (day, time.time() + delay)
        self._save_delivery_state()
        logger.warning(
            "日报 %s 发送失败，%d 分钟后重试（第 %d 次）",
            day or "--",
            delay // 60,
            self._pending_retry_count,
        )
        return True

    def _clear_retry(self) -> None:
        self._pending_retry = None
        self._pending_retry_count = 0
        self._pending_retry_day = None
        self._save_delivery_state()

    def _today(self) -> str:
        return now_in_timezone(self.config).strftime("%Y-%m-%d")

    async def _send_scheduled(self) -> None:
        targets = self._targets()
        if self._sections() is not None and not self._sections():
            logger.warning("日报内容为空（daily_report.sections=[]），跳过发送")
            self._record("", False, "sections empty")
            self._clear_retry()
            return
        try:
            view = self._build_view()
        except Exception:
            logger.exception("日报数据聚合失败")
            self._record("", False, "build failed")
            if not self._schedule_retry(""):
                await self._notify_give_up(targets, "", weekly=False)
            return
        day = str(view.get("day") or "")
        if not targets:
            logger.warning("日报推送目标为空，跳过发送")
            self._record(day, False, "targets empty")
            self._schedule_retry(day)
            return
        if self._last_sent_day == day:
            logger.info("日报 %s 已发送过，跳过重复发送", day)
            self._clear_retry()
            return
        if self._with_image():
            image_url = None
            try:
                image_url = await self.plugin.render_report_image(view)
            except Exception:
                logger.exception("日报渲染失败，回退文字摘要")
            if image_url and await self.plugin.send_image(
                targets, image_url, source="report"
            ):
                self._last_sent_day = day
                self._record(day, True, "image")
                self._clear_retry()
                self._save_delivery_state()
                logger.info("日报已发送（图片）：%s", targets)
                return
        if await self.plugin.send_text(
            targets, build_report_text(view), source="report"
        ):
            self._last_sent_day = day
            self._record(day, True, "text")
            self._clear_retry()
            self._save_delivery_state()
            logger.info("日报已发送（文字）：%s", targets)
        else:
            self._record(day, False, "send failed")
            logger.warning("日报发送失败：%s", targets)
            if not self._schedule_retry(day):
                await self._notify_give_up(targets, day, weekly=False)

    async def preview(
        self, target_umo: str | None, span_days: int = 1
    ) -> tuple[bool, str]:
        if not target_umo:
            return False, tr(self.config, "report_no_session")
        span = max(1, min(30, int(span_days or 1)))
        prefix = "report" if span == 1 else "weekly"
        source = "report_preview" if span == 1 else "weekly_preview"
        try:
            view = self._build_view(span_days=span)
        except Exception:
            logger.exception("日报预览数据聚合失败")
            return False, tr(self.config, f"{prefix}_preview_failed")
        if self._with_image():
            image_url = None
            try:
                image_url = await self.plugin.render_report_image(view)
            except Exception:
                logger.exception("日报预览渲染失败，回退文字摘要")
            if image_url and await self.plugin.send_image(
                [target_umo], image_url, source=source
            ):
                return True, tr(self.config, f"{prefix}_preview_sent")
        if await self.plugin.send_text(
            [target_umo], build_report_text(view), source=source
        ):
            return True, tr(self.config, f"{prefix}_preview_sent_text")
        return False, tr(self.config, f"{prefix}_preview_send_failed")

    def _record(self, day: str, ok: bool, detail: str) -> None:
        try:
            self.store.add_report_run(day, int(time.time()), ok, detail)
        except Exception:
            logger.exception("写入日报发送记录失败")

    def status(self) -> dict[str, Any]:
        try:
            last_run = self.store.latest_report_run()
        except Exception:
            last_run = None
        return {
            "enabled": self._enabled(),
            "running": self._running,
            "time": self._time_text(),
            "targets": self._targets(),
            "sections": self._sections(),
            "with_image": self._with_image(),
            "history_days": self._history_days(),
            "next_run": self._next_run_text,
            "last_run": last_run,
            "weekly_enabled": self._weekly_enabled(),
            "weekly_time": self._weekly_time_text(),
            "weekly_next_run": self._weekly_next_run_text,
        }
