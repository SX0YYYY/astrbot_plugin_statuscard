from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import date, timedelta
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
    disk_usage_bounded,
    memory_stat,
    now_in_timezone,
    resolve_targets,
    swap_stat,
    website_ok,
)
from .delivery import DeliveryResult
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


def _delivered(result: Any) -> bool:
    """True only when the platform confirmed delivery.

    A timeout or a lost acknowledgement means the message may or may not be in
    the chat. Callers must handle that with ``_uncertain`` and record it as
    unconfirmed instead of reporting a clean success. Plain booleans keep test
    doubles working.
    """

    if isinstance(result, DeliveryResult):
        return result.ok
    return bool(result)


def _website_ok(status: Any) -> bool:
    """Available means 2xx/3xx (same rule as the status card).

    A 502/500 is the server answering while the endpoint is broken; the old
    rule only treated a transport error as downtime, so a dead backend behind
    a live proxy still reported "可用率 100%". The status card used to keep the
    old rule, so one probe could be green there and 0% in the report.
    """

    return website_ok(status)


def _uncertain(result: Any) -> bool:
    """True when a send may have reached the chat without confirmation.

    The message is not retried (that could post it twice), but it must not be
    logged or recorded as a confirmed delivery either.
    """

    return (
        isinstance(result, DeliveryResult)
        and not result.ok
        and bool(result.uncertain_targets)
    )


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
            name = str(info.get("name") or f"(pid {pid})")[:80]
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
        self._snapshot_attempts: dict[str, int] = {}

    def start(self) -> None:
        if not cfg_bool(self.config, "daily_report.collect", True):
            logger.info("日报数据收集未启用（daily_report.collect=false）")
            return
        if not self.store.available:
            # Keep the loop alive: it retries the database every minute, so a
            # full disk or a permission fix no longer requires a plugin reload.
            logger.warning("日报数据库暂不可用，将在后台每分钟重试初始化")
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
        await asyncio.to_thread(self._backfill_from_cache)
        while self._running:
            try:
                if not self.store.ensure_ready():
                    await asyncio.sleep(60)
                    continue
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
            paths: list[str] = []
            if isinstance(disk_paths, list):
                paths = [str(item) for item in disk_paths if str(item).strip()]
            if not paths:
                paths = [os.path.abspath(os.sep)]
            # Every mount is watched by the disk alert, so the trend must
            # follow the worst one: a filling second disk used to stay
            # invisible in the report while the alert fired.
            # statvfs blocks on a hung network mount, so it must not run on
            # the event loop (the card path already used a thread).
            rows = await asyncio.to_thread(disk_usage_bounded, paths)
            percents = [row.percent for row in rows if row.percent is not None]
            disk_pct = max(percents) if percents else None
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
        self._cache_resource_point(
            now,
            {
                "cpu": float(cpu),
                "ram": float(mem.percent),
                "swap": float(swap.percent),
                "disk": disk_pct,
                "load1": load1,
                "net_up": float(net.get("up_rate") or 0.0),
                "net_down": float(net.get("down_rate") or 0.0),
                "disk_read": float(disk_io.get("read_rate") or 0.0),
                "disk_write": float(disk_io.get("write_rate") or 0.0),
            },
        )

    RESOURCE_CACHE_MAX = 200
    RESOURCE_CACHE_TTL = 86400

    def _cache_resource_point(self, now: float, point: dict[str, Any]) -> None:
        """Persist recent samples in state.json so a reload can refill them.

        A config save reloads the plugin and drops the collector's in-memory
        schedule; without this cache the report database has a hole from the
        reload until the next sample. The process-death case cannot be
        refilled (no data exists) and is surfaced as a monitoring gap instead.
        """

        try:
            with self.state.section("resource_cache") as cache:
                if not isinstance(cache, dict):
                    return
                rows = cache.get("points")
                if not isinstance(rows, list):
                    rows = []
                rows = [row for row in rows if isinstance(row, dict)]
                rows.append({"ts": int(now), **point})
                cutoff = now - self.RESOURCE_CACHE_TTL
                rows = [
                    row
                    for row in rows[-self.RESOURCE_CACHE_MAX :]
                    if int(row.get("ts") or 0) >= cutoff
                ]
                cache["points"] = rows
            self.state.save()
        except Exception:
            logger.debug("写入采样缓存失败", exc_info=True)

    def _backfill_from_cache(self) -> None:
        """Upsert cached samples the report database is missing."""

        try:
            cache = self.state.get("resource_cache", {})
            rows = cache.get("points") if isinstance(cache, dict) else None
            if not rows:
                return
            latest = self.store.latest_resource_ts() or 0
            cutoff = time.time() - self.RESOURCE_CACHE_TTL
            pending = [
                row
                for row in rows
                if int(row.get("ts") or 0) > latest
                and int(row.get("ts") or 0) >= cutoff
            ]
            if not pending:
                return
            for row in pending:
                self.store.add_resource(
                    int(row["ts"]),
                    row.get("cpu"),
                    row.get("ram"),
                    row.get("swap"),
                    row.get("disk"),
                    row.get("load1"),
                    row.get("net_up"),
                    row.get("net_down"),
                    row.get("disk_read"),
                    row.get("disk_write"),
                )
            logger.info(
                "已从采样缓存回填 %d 个采样点（补齐重载期间的缺口）", len(pending)
            )
        except Exception:
            logger.debug("采样回填失败", exc_info=True)

    async def _sample_websites(self, now: float) -> None:
        try:
            targets = cfg_get(self.config, "display.website_targets", None)
            results = await connection_test(targets, cache_seconds=0)
            samples = [
                {
                    "name": item.name,
                    "ok": _website_ok(item.status),
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
        # Keep retrying when the OneBot counts are complete but recv_total is
        # still missing: the two sources fail independently, and the backfill
        # branch in record_snapshot is only reachable from this path.
        if self.store.snapshot_ready(day):
            return
        attempts = self._snapshot_attempts.get(day, 0)
        # Keep retrying for the whole day (5-minute cadence): OneBot being
        # unreachable between 00:05 and 01:05 used to leave friends/groups/
        # members NULL for that day and the receive delta empty for two days.
        if attempts >= 288:
            return
        if time.time() - self._last_snapshot_attempt < 300:
            return
        self._last_snapshot_attempt = time.time()
        # One key per day: the dict must not grow across a long uptime.
        self._snapshot_attempts = {day: attempts + 1}

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
        self._pending_retry: tuple[str | None, float] | None = None
        self._pending_retry_count = 0
        self._pending_retry_day: str | None = None
        self._next_run_text: str | None = None
        self._disabled_logged = False
        self._weekly_task: asyncio.Task | None = None
        self._weekly_running = False
        self._weekly_pending: float | None = None
        self._weekly_pending_report_day: str | None = None
        self._weekly_retry_count = 0
        self._weekly_next_run_text: str | None = None
        self._last_sent_day: str | None = None
        self._last_attempt_day: str | None = None
        self._weekly_last_sent_week: str | None = None
        # Week the last weekly send was ATTEMPTED in (set before any slow
        # work): a crash/reload mid-send then leaves a marker the catch-up
        # check accepts, exactly like the daily last_attempt_day. Without
        # it the very first weekly run was lost silently whenever the
        # process died inside the Sunday window (v2.20.0).
        self._weekly_attempt_week: str | None = None
        # A week whose weekly report was given up must not restart the
        # 10/20/30-minute retry cycle: the catch-up check only looked at
        # last_sent, so every abandon immediately scheduled another attempt
        # and the user got a "stopped retrying" notice every ~40 minutes.
        self._weekly_given_up_week: str | None = None
        # Empty targets / empty sections used to re-enter the send path every
        # second (3600 journal lines and hundreds of report_runs per hour).
        # A short in-memory pause keeps the loop quiet until the operator
        # fixes the config (a reload clears it).
        self._weekly_pause_until = 0.0
        self._weekly_empty_recorded_week: str | None = None
        # Render retries: a short T2I outage should not turn the report into
        # text. The attempts and the next retry time survive reloads too.
        # The report day is pinned as well: a retry that crosses midnight must
        # rebuild the same report, not the next day's empty one.
        self._image_retry_attempts = 0
        self._pending_image_retry: float | None = None
        self._pending_image_retry_report_day: str | None = None
        self._pending_image_retry_send_day: str | None = None
        self._weekly_image_attempts = 0
        self._weekly_image_pending: float | None = None
        self._weekly_image_report_day: str | None = None
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

        def _safe_int(value: Any, default: int = 0) -> int:
            # A hand-edited state.json can hold anything; a ValueError here
            # would abort plugin loading, so stay defensive like the float
            # fields below.
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return default

        self._last_sent_day = saved.get("last_sent_day") or None
        self._last_attempt_day = saved.get("last_attempt_day") or None
        self._weekly_last_sent_week = saved.get("weekly_last_sent_week") or None
        self._weekly_attempt_week = saved.get("weekly_attempt_week") or None
        self._weekly_given_up_week = saved.get("weekly_given_up_week") or None
        self._pending_retry_count = _safe_int(saved.get("pending_retry_count"))
        self._pending_retry_day = saved.get("pending_retry_day") or None
        self._weekly_retry_count = _safe_int(saved.get("weekly_retry_count"))
        self._weekly_pending_report_day = saved.get("weekly_pending_report_day") or None
        self._pending_image_retry_report_day = (
            saved.get("pending_image_retry_report_day") or None
        )
        self._pending_image_retry_send_day = (
            saved.get("pending_image_retry_send_day") or None
        )
        self._weekly_image_report_day = saved.get("weekly_image_report_day") or None
        pending = saved.get("pending_retry")
        if isinstance(pending, dict):
            try:
                # ``day`` may legitimately be null: the scheduled path queues
                # a retry for "yesterday" and must be restored as such.
                day_value = pending.get("day")
                self._pending_retry = (
                    str(day_value) if day_value is not None else None,
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
        self._image_retry_attempts = _safe_int(saved.get("image_retry_attempts"))
        self._weekly_image_attempts = _safe_int(saved.get("weekly_image_attempts"))
        for attribute, key in (
            ("_pending_image_retry", "pending_image_retry"),
            ("_weekly_image_pending", "weekly_image_pending"),
        ):
            raw = saved.get(key)
            try:
                value = float(raw) if raw else None
            except (TypeError, ValueError):
                value = None
            setattr(self, attribute, value)

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
                    "last_attempt_day": self._last_attempt_day,
                    "pending_retry": pending,
                    "pending_retry_count": self._pending_retry_count,
                    "pending_retry_day": self._pending_retry_day,
                    "weekly_last_sent_week": self._weekly_last_sent_week,
                    "weekly_attempt_week": self._weekly_attempt_week,
                    "weekly_given_up_week": self._weekly_given_up_week,
                    "weekly_pending": self._weekly_pending,
                    "weekly_pending_report_day": self._weekly_pending_report_day,
                    "weekly_retry_count": self._weekly_retry_count,
                    "image_retry_attempts": self._image_retry_attempts,
                    "pending_image_retry": self._pending_image_retry,
                    "pending_image_retry_report_day": (
                        self._pending_image_retry_report_day
                    ),
                    "pending_image_retry_send_day": self._pending_image_retry_send_day,
                    "weekly_image_attempts": self._weekly_image_attempts,
                    "weekly_image_pending": self._weekly_image_pending,
                    "weekly_image_report_day": self._weekly_image_report_day,
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
        """Return the selected sections (never None).

        An unset key yields the built-in defaults. v2.20.0: the docstring
        used to promise a ``None`` return that no code path produced.
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

    def _weekly_sections(self) -> list[str] | None:
        """Weekly report content; unset/empty means "follow the daily one"."""
        raw = cfg_get(self.config, "daily_report.weekly_sections", None)
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            return self._sections()
        allowed = {str(item) for item in raw if isinstance(item, str)}
        weekly = [item for item in VALID_SECTIONS if item in allowed]
        return weekly or self._sections()

    def _targets(self, kind: str = "daily") -> list[str]:
        picked = resolve_targets(self.config, kind)
        if picked is not None:
            return picked
        raw = cfg_get(self.config, "daily_report.targets", [])
        if not raw:
            raw = cfg_get(self.config, "alerts.targets", [])
        platform_id = str(
            cfg_get(self.config, "alerts.platform_id", "default") or "default"
        )
        return normalize_targets(raw, platform_id)

    def _build_view(
        self, day=None, span_days: int = 1, sections: list[str] | None = None
    ) -> dict[str, Any]:
        meta = {
            "host": socket.gethostname(),
            "astrbot_version": resolve_astrbot_version(
                getattr(self.plugin, "context", None)
            ),
        }
        return self.builder.build(
            day=day,
            history_days=self._history_days(),
            sections=sections if sections is not None else self._sections(),
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
        # Weekly dispatch has its own switch and must not depend on the daily
        # report being enabled: "weekly only" is a supported configuration.
        self._start_weekly()
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
                        # A disabled report must not resurrect a stale retry
                        # when the user enables it again hours later.
                        self._pending_image_retry = None
                        self._image_retry_attempts = 0
                        self._weekly_image_attempts = 0
                        self._clear_retry(clear_weekly=True)
                    await asyncio.sleep(60)
                    continue
                self._disabled_logged = False
                if self._pending_image_retry is not None:
                    if time.time() < self._pending_image_retry:
                        await asyncio.sleep(60)
                        continue
                    self._pending_image_retry = None
                    report_day = self._pending_image_retry_report_day
                    send_day = self._pending_image_retry_send_day
                    self._pending_image_retry_report_day = None
                    self._pending_image_retry_send_day = None
                    if send_day and send_day != self._today():
                        # The report day is pinned, so the retry still sends
                        # the same report rather than a new one. Dropping it
                        # meant a 23:50 schedule lost the whole report when
                        # the retry crossed midnight.
                        logger.warning(
                            "日报图片重试已跨天，继续用原报告日 %s 发送",
                            report_day or "--",
                        )
                    self._save_delivery_state()
                    try:
                        await self._send_scheduled(report_day)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("日报图片重试意外失败")
                    await asyncio.sleep(60)
                    continue
                if self._pending_retry:
                    _day, due = self._pending_retry
                    if time.time() < due:
                        await asyncio.sleep(60)
                        continue
                    self._pending_retry = None
                    if (
                        self._pending_retry_day
                        and self._pending_retry_day != self._today()
                    ):
                        # Keep the pinned report day instead of dropping the
                        # retry: the same cross-midnight rule was already
                        # fixed for images, and a text report is still the
                        # same day's report.
                        logger.warning(
                            "日报 %s 的重试已跨天，继续用原报告日发送",
                            _day or "--",
                        )
                    try:
                        await self._send_scheduled(_day)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # An unexpected error must not eat this retry slot;
                        # put the day back on the retry plan.
                        logger.exception("日报重试发送意外失败，重新排入重试")
                        if not self._schedule_retry(_day):
                            await self._notify_give_up(self._targets(), _day)
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
                    if self._daily_catch_up_allowed(self._today()):
                        logger.info(
                            "日报定时点已过且今天尚未尝试，立即补发一次（%s）",
                            self._today(),
                        )
                        target = now
                    else:
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
                self._image_retry_attempts = 0
                self._pending_image_retry = None
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
        weekly_warned = False
        while self._weekly_running:
            try:
                if not self._weekly_enabled():
                    self._weekly_next_run_text = None
                    await asyncio.sleep(60)
                    continue
                if self._weekly_image_pending is not None:
                    if time.time() < self._weekly_image_pending:
                        await asyncio.sleep(60)
                        continue
                    self._weekly_image_pending = None
                    report_day = self._weekly_image_report_day
                    self._weekly_image_report_day = None
                    if report_day and report_day != self._default_report_day():
                        # Same rule as the daily image retry: the report day is
                        # pinned, so sending it late is still the same report.
                        # Dropping it lost the whole week.
                        logger.warning(
                            "周报图片重试已跨周，继续用原报告日 %s 发送",
                            report_day,
                        )
                    self._save_delivery_state()
                    try:
                        await self._send_weekly_scheduled(report_day)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("周报图片重试意外失败")
                    await asyncio.sleep(60)
                    continue
                if self._weekly_pending is not None:
                    if time.time() < self._weekly_pending:
                        await asyncio.sleep(60)
                        continue
                    self._weekly_pending = None
                    report_day = self._weekly_pending_report_day
                    if report_day and report_day != self._default_report_day():
                        # The image retry already keeps the original report
                        # day; the text retry used to give up here, so a slow
                        # T2I failure near midnight could not fall back at all.
                        logger.info(
                            "周报文字重试仍使用原报告日 %s（跨周不改）", report_day
                        )
                    try:
                        await self._send_weekly_scheduled(report_day)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("周报重试发送意外失败，重新排入重试")
                        if not self._schedule_weekly_retry():
                            await self._notify_give_up(
                                self._targets("weekly"), "--", weekly=True
                            )
                    await asyncio.sleep(60)
                    continue
                time_text = self._weekly_time_text()
                tz_name = str(
                    cfg_get(self.config, "display.timezone", "Asia/Shanghai")
                    or "Asia/Shanghai"
                )
                try:
                    hour, minute = [int(part) for part in time_text.split(":", 1)]
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError("out of range")
                    weekly_warned = False
                except Exception:
                    # Warn once per bad value, not every minute: an unhandled
                    # loop here filled the journal with 1440 warnings a day.
                    if not weekly_warned:
                        logger.warning(
                            "周报发送时间格式非法：%s（应为 HH:MM）",
                            time_text,
                        )
                        weekly_warned = True
                    await asyncio.sleep(60)
                    continue
                now = now_in_timezone(self.config)
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                target += timedelta(days=(6 - target.weekday()) % 7)
                if target <= now:
                    if self._weekly_catch_up_allowed():
                        logger.info("周报定时点已过且本周尚未发送，立即补发一次")
                        target = now
                    else:
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
                # A change in the last minute must not fire at the old time;
                # the daily loop rechecks the same way.
                if (
                    self._weekly_time_text() != time_text
                    or str(
                        cfg_get(self.config, "display.timezone", "Asia/Shanghai")
                        or "Asia/Shanghai"
                    )
                    != tz_name
                ):
                    continue
                self._weekly_image_attempts = 0
                self._weekly_image_pending = None
                await self._send_weekly_scheduled()
                last_logged = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("周报定时任务失败")
                await asyncio.sleep(60)

    def _default_report_day(self) -> str:
        return (now_in_timezone(self.config) - timedelta(days=1)).date().isoformat()

    def _schedule_weekly_retry(self) -> bool:
        self._weekly_retry_count += 1
        if self._weekly_retry_count > 3:
            self._weekly_pending = None
            self._weekly_pending_report_day = None
            self._weekly_retry_count = 0
            # Remember the week that was abandoned: without it the catch-up
            # check instantly queued another attempt after the give-up notice.
            self._weekly_given_up_week = self._week_key(self._today())
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

    async def _send_weekly_scheduled(self, day: str | None = None) -> None:
        # Mark this week as attempted before any slow work, mirroring the
        # daily report's last_attempt_day (v2.20.0).
        self._weekly_attempt_week = self._week_key(self._today())
        self._save_delivery_state()
        weekly_sections = self._weekly_sections()
        if weekly_sections is not None and not weekly_sections:
            logger.warning("周报内容为空（daily_report.weekly_sections=[]），跳过发送")
            week_key = self._week_key(self._today())
            if self._weekly_empty_recorded_week != week_key:
                # Record the misconfiguration once per week, not once per
                # second (the loop retried immediately after the early return).
                self._weekly_empty_recorded_week = week_key
                self._record(self._default_report_day(), False, "weekly sections empty")
            self._weekly_pause_until = time.time() + 600
            self._clear_retry()
            return
        if not self.store.available and not self.store.ensure_ready():
            logger.error("日报数据库不可用，跳过本次周报发送")
            await self._notify_store_unavailable(self._targets("weekly"))
            # A missing/unreadable database is not going to heal within the
            # second the loop otherwise waits before re-entering this function:
            # without a pause the loop spun at 1Hz from 21:30 to midnight,
            # fsync-ing state.json and logging ~18k lines each Sunday. Back off
            # like the empty-target/sections branches (a fresh attempt still
            # runs after the pause).
            self._weekly_pause_until = time.time() + 600
            return
        try:
            view = await asyncio.to_thread(
                self._build_view, day, 7, self._weekly_sections()
            )
        except Exception:
            logger.exception("周报数据聚合失败")
            if not self._schedule_weekly_retry():
                await self._notify_give_up(self._targets("weekly"), "--", weekly=True)
            return
        report_day = str(view.get("day") or "")
        self._weekly_pending_report_day = report_day
        try:
            iso = date.fromisoformat(report_day).isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"
        except ValueError:
            iso = now_in_timezone(self.config).date().isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"
        if self._weekly_last_sent_week == week_key:
            logger.info("周报 %s 已发送过，跳过重复发送", week_key)
            return
        targets = self._targets("weekly")
        if not targets:
            # Same as the daily report: an empty target list cannot heal, so
            # the three-retry cycle only burned report_runs rows.
            logger.warning("周报推送目标为空，跳过发送（请先在配置里填写推送目标）")
            self._weekly_retry_count = 0
            self._weekly_pause_until = time.time() + 600
            self._save_delivery_state()
            return
        day = report_day
        if self._with_image():
            image_url = None
            try:
                image_url = await self.plugin.render_report_image(view)
            except Exception:
                logger.exception("周报渲染失败")
            if image_url is None and self._weekly_image_attempts < 2:
                # Same short-outage policy as the daily report: retry the
                # render twice before degrading to text.
                self._weekly_image_attempts += 1
                self._weekly_image_pending = time.time() + 600
                self._weekly_image_report_day = day
                self._save_delivery_state()
                logger.warning(
                    "周报图片渲染失败，10 分钟后重试第 %d/2 次；仍失败将发送文字版",
                    self._weekly_image_attempts,
                )
                return
            if image_url:
                result = await self.plugin.send_image(
                    targets, image_url, source="weekly_report"
                )
                if _delivered(result):
                    self._weekly_last_sent_week = week_key
                    self._weekly_given_up_week = None
                    self._record(
                        day,
                        True,
                        "weekly-image"
                        if not (isinstance(result, DeliveryResult) and result.partial)
                        else "weekly-image (partial)",
                    )
                    self._weekly_pending = None
                    self._weekly_pending_report_day = None
                    self._weekly_retry_count = 0
                    self._weekly_image_attempts = 0
                    self._weekly_image_pending = None
                    self._weekly_image_report_day = None
                    self._save_delivery_state()
                    logger.info("周报已发送（图片）：%s", targets)
                    return
                if _uncertain(result):
                    logger.warning(
                        "周报图片发送未确认（无法确认是否送达），已按不重发处理以免重复"
                    )
                    self._weekly_last_sent_week = week_key
                    self._weekly_given_up_week = None
                    self._record(day, True, "weekly-image unconfirmed")
                    self._weekly_pending = None
                    self._weekly_pending_report_day = None
                    self._weekly_retry_count = 0
                    self._weekly_image_attempts = 0
                    self._weekly_image_pending = None
                    self._weekly_image_report_day = None
                    self._save_delivery_state()
                    return
        result = await self.plugin.send_text(
            targets, build_report_text(view), source="weekly_report"
        )
        if _delivered(result):
            self._weekly_last_sent_week = week_key
            self._record(
                day,
                True,
                "weekly-text"
                if not (isinstance(result, DeliveryResult) and result.partial)
                else "weekly-text (partial)",
            )
            self._weekly_pending = None
            self._weekly_pending_report_day = None
            self._weekly_retry_count = 0
            self._weekly_image_attempts = 0
            self._weekly_image_pending = None
            self._weekly_image_report_day = None
            self._save_delivery_state()
            logger.info("周报已发送（文字）：%s", targets)
        elif _uncertain(result):
            logger.warning("周报文字发送未确认（无法确认是否送达），已按不重发处理")
            self._weekly_last_sent_week = week_key
            self._record(day, True, "weekly-text unconfirmed")
            self._weekly_pending = None
            self._weekly_pending_report_day = None
            self._weekly_retry_count = 0
            self._weekly_image_attempts = 0
            self._weekly_image_pending = None
            self._weekly_image_report_day = None
            self._save_delivery_state()
        else:
            self._record(day, False, "weekly send failed")
            logger.warning("周报发送失败：%s", targets)
            if not self._schedule_weekly_retry():
                await self._notify_give_up(targets, day, weekly=True)

    async def _notify_store_unavailable(self, targets: list[str]) -> None:
        """Tell the operator at most once per day that report.db is unusable."""

        if not targets:
            return
        today = self._today()
        state = getattr(self.plugin, "state", None)
        if state is not None:
            try:
                sent = state.get("report_store_notice", {}) or {}
                if isinstance(sent, dict) and sent.get("day") == today:
                    return
            except Exception:
                pass
        try:
            await self.plugin.send_text(
                targets,
                tr(self.config, "report_db_unavailable", day=today),
                source="report_db_unavailable",
            )
        except Exception:
            logger.debug("发送日报数据库不可用提示失败", exc_info=True)
        if state is not None:
            try:
                state.set("report_store_notice", {"day": today, "ts": time.time()})
                state.save()
            except Exception:
                logger.debug("记录日报数据库不可用提示失败", exc_info=True)

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

    def _schedule_retry(self, day: str | None) -> bool:
        """Remember a failed delivery and queue the next attempt.

        ``day`` is ``None`` for the scheduled run, which resolves "yesterday"
        itself. Callers used to pass ``day or ""``, so a build failure queued
        an empty string and every retry then died in ``date.fromisoformat("")``
        -- the retry budget was spent without ever producing a report.

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

    def _clear_retry(self, clear_weekly: bool = False) -> None:
        changed = (
            self._pending_retry is not None
            or bool(self._pending_retry_count)
            or self._pending_retry_day is not None
            or self._pending_image_retry_report_day is not None
            or self._pending_image_retry_send_day is not None
        )
        self._pending_retry = None
        self._pending_retry_count = 0
        self._pending_retry_day = None
        self._pending_image_retry_report_day = None
        self._pending_image_retry_send_day = None
        if clear_weekly:
            # Only the disabled branch passes this: a weekly retry queued
            # while the daily report got disabled used to survive the
            # cleanup (the daily fields were clean, so nothing was saved)
            # and resurrect itself after a reload (v2.20.0). A plain
            # daily-success clear must NOT touch the weekly schedule.
            changed = changed or (
                self._weekly_pending is not None
                or self._weekly_pending_report_day is not None
                or self._weekly_image_pending is not None
                or self._weekly_image_report_day is not None
            )
            self._weekly_pending = None
            self._weekly_pending_report_day = None
            self._weekly_image_pending = None
            self._weekly_image_report_day = None
        if changed:
            self._save_delivery_state()

    def _today(self) -> str:
        return now_in_timezone(self.config).strftime("%Y-%m-%d")

    def _daily_catch_up_allowed(self, today: str) -> bool:
        """Whether a missed daily-report window may be sent on start-up.

        A missing marker means this is a first install or an upgrade from a
        version without the marker; sending then would duplicate a report the
        previous process may already have delivered. Once a marker exists,
        only ``last_attempt_day`` protects against a reload loop.
        """

        if not self._last_attempt_day:
            return False
        return not (self._last_sent_day == today or self._last_attempt_day == today)

    def _week_key(self, day: str) -> str:
        try:
            iso = date.fromisoformat(day).isocalendar()
        except ValueError:
            # Align with the rest of the scheduler: use the display
            # timezone, not the server-local date (v2.20.0).
            iso = now_in_timezone(self.config).date().isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def _weekly_catch_up_allowed(self) -> bool:
        """Whether a missed weekly window may be sent right now.

        The daily report already catches up; the weekly one did not, so a
        reload at 21:29 silently skipped the whole week. Two guards keep it
        safe: a missing marker means a first install or an upgrade, and the
        catch-up only runs on the trigger day itself -- a Monday restart must
        not deliver last week's report in the middle of a new week.
        """

        if time.time() < float(getattr(self, "_weekly_pause_until", 0.0) or 0.0):
            # A config problem (empty target/sections) paused the loop for a
            # few minutes instead of re-entering the send path every second.
            return False
        if not self._weekly_last_sent_week and not self._weekly_attempt_week:
            # Neither marker means a genuine first install/upgrade: sending
            # then could duplicate a report the previous process already
            # delivered. An attempt marker alone (crash mid-send) is enough
            # to allow the catch-up on the trigger day (v2.20.0).
            return False
        now = now_in_timezone(self.config)
        if now.weekday() != 6:
            return False
        report_day = (now - timedelta(days=1)).date().isoformat()
        week_key = self._week_key(report_day)
        if getattr(self, "_weekly_given_up_week", None) == week_key:
            # Already gave up after three failed attempts this week; retrying
            # again would produce the same notice every ~40 minutes.
            return False
        return self._weekly_last_sent_week != week_key

    async def _send_scheduled(self, day: str | None = None) -> None:
        # Scope the "already attempted today" guard to the report day, not the
        # wall-clock day. A stale retry that crosses midnight used to stamp
        # ``_last_attempt_day`` with *today* here, so the catch-up check then
        # concluded "today already attempted" and the fresh report was skipped
        # for the whole day while the operator received an outdated one.
        self._last_attempt_day = day or self._today()
        self._save_delivery_state()
        targets = self._targets()
        if self._sections() is not None and not self._sections():
            logger.warning("日报内容为空（daily_report.sections=[]），跳过发送")
            self._record(self._default_report_day(), False, "sections empty")
            self._clear_retry()
            return
        if not self.store.available and not self.store.ensure_ready():
            # A damaged or read-only database would otherwise produce a page
            # full of "--" and send it as if it were a real report.
            logger.error("日报数据库不可用，跳过本次日报发送")
            self._record(day or self._default_report_day(), False, "db unavailable")
            await self._notify_store_unavailable(targets)
            return
        try:
            view = await asyncio.to_thread(self._build_view, day)
        except Exception:
            logger.exception("日报数据聚合失败")
            self._record(day or self._default_report_day(), False, "build failed")
            # Resolve "yesterday" here: a build failure at the scheduled run
            # passes ``day=None`` and the retry then rebuilt *today's* (mostly
            # empty) report after a midnight crossing, discarding the report
            # that actually failed.
            if not self._schedule_retry(day or self._default_report_day()):
                await self._notify_give_up(targets, day or "", weekly=False)
            return
        day = str(view.get("day") or "")
        if not targets:
            # An empty target list is a configuration problem that will not
            # heal on its own; retrying three times only wrote three
            # "targets empty" rows and the give-up notice went nowhere
            # anyway (there is no target to notify). The config audit flags
            # the empty list at startup; here one record is enough.
            logger.warning("日报推送目标为空，跳过发送（请先在配置里填写推送目标）")
            self._record(day, False, "targets empty")
            self._clear_retry()
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
                logger.exception("日报渲染失败")
            if image_url is None and self._image_retry_attempts < 2:
                # The T2I endpoints fail in short bursts (seen twice right on
                # 21:00). Silently retry the render twice, ten minutes apart,
                # before falling back to text: a short outage should still
                # deliver the image rather than a text-only report.
                self._image_retry_attempts += 1
                self._pending_image_retry = time.time() + 600
                self._pending_image_retry_report_day = str(view.get("day") or "")
                self._pending_image_retry_send_day = self._today()
                self._save_delivery_state()
                logger.warning(
                    "日报图片渲染失败，10 分钟后重试第 %d/2 次；仍失败将发送文字版",
                    self._image_retry_attempts,
                )
                return
            if image_url:
                result = await self.plugin.send_image(
                    targets, image_url, source="report"
                )
                if _delivered(result):
                    self._last_sent_day = day
                    self._record(
                        day,
                        True,
                        "image"
                        if not (isinstance(result, DeliveryResult) and result.partial)
                        else "image (partial)",
                    )
                    self._image_retry_attempts = 0
                    self._pending_image_retry = None
                    self._clear_retry()
                    self._save_delivery_state()
                    logger.info("日报已发送（图片）：%s", targets)
                    return
                if _uncertain(result):
                    logger.warning(
                        "日报图片发送未确认（无法确认是否送达），已按不重发处理以免重复"
                    )
                    self._last_sent_day = day
                    # Not a confirmed delivery: record it as failed so the
                    # dashboard's send history does not show a clean success
                    # (the detail still says "unconfirmed").
                    self._record(day, False, "image unconfirmed")
                    self._image_retry_attempts = 0
                    self._pending_image_retry = None
                    self._clear_retry()
                    self._save_delivery_state()
                    return
        result = await self.plugin.send_text(
            targets, build_report_text(view), source="report"
        )
        if _delivered(result):
            self._last_sent_day = day
            self._record(
                day,
                True,
                "text"
                if not (isinstance(result, DeliveryResult) and result.partial)
                else "text (partial)",
            )
            self._image_retry_attempts = 0
            self._pending_image_retry = None
            self._clear_retry()
            self._save_delivery_state()
            logger.info("日报已发送（文字）：%s", targets)
        elif _uncertain(result):
            logger.warning("日报文字发送未确认（无法确认是否送达），已按不重发处理")
            self._last_sent_day = day
            self._record(day, False, "text unconfirmed")
            self._image_retry_attempts = 0
            self._pending_image_retry = None
            self._clear_retry()
            self._save_delivery_state()
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
        if not self.store.available and not self.store.ensure_ready():
            # Preview used to build a page of "--" and send it as if it were
            # a real report; the scheduled path already refuses to.
            return False, tr(self.config, "report_db_unavailable_preview")
        span = max(1, min(30, int(span_days or 1)))
        prefix = "report" if span == 1 else "weekly"
        source = "report_preview" if span == 1 else "weekly_preview"
        try:
            preview_sections = (
                self._sections() if span == 1 else self._weekly_sections()
            )
            view = await asyncio.to_thread(
                self._build_view, span_days=span, sections=preview_sections
            )
        except Exception:
            logger.exception("日报预览数据聚合失败")
            return False, tr(self.config, f"{prefix}_preview_failed")
        if self._with_image():
            image_url = None
            try:
                image_url = await self.plugin.render_report_image(view)
            except Exception:
                logger.exception("日报预览渲染失败，回退文字摘要")
            if image_url:
                result = await self.plugin.send_image(
                    [target_umo], image_url, source=source
                )
                if _delivered(result):
                    return True, tr(self.config, f"{prefix}_preview_sent")
                if _uncertain(result):
                    return True, tr(self.config, f"{prefix}_preview_unconfirmed")
        result = await self.plugin.send_text(
            [target_umo], build_report_text(view), source=source
        )
        if _delivered(result):
            return True, tr(self.config, f"{prefix}_preview_sent_text")
        if _uncertain(result):
            return True, tr(self.config, f"{prefix}_preview_unconfirmed")
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
            "weekly_sections": self._weekly_sections(),
            "with_image": self._with_image(),
            "history_days": self._history_days(),
            "next_run": self._next_run_text,
            "last_run": last_run,
            "weekly_enabled": self._weekly_enabled(),
            "weekly_time": self._weekly_time_text(),
            "weekly_next_run": self._weekly_next_run_text,
        }
