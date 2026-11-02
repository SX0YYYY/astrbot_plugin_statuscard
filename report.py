from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import date, datetime, timedelta
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
    get_timezone,
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
        if self._task is not None and not self._task.done():
            # A second initialize() (config reload) used to create a second
            # sampling loop; terminate() only cancels the last one, so the
            # first kept writing and saving state in the background
            # (v2.20.17).
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
            try:
                local_start = (
                    now_in_timezone(self.config)
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .timestamp()
                )
            except Exception:
                local_start = None
            await asyncio.to_thread(
                self.store.prune,
                self.retention_days,
                self.event_retention_days,
                int(now),
                local_start,
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
        # The snapshot is meant to hold the counter at the *start* of the day.
        # If the bot was disconnected at 00:05 and reconnected at 08:00,
        # reading the counter now stores the 08:00 value and the day's receive
        # delta silently loses the first eight hours (measured as low as 9% of
        # the real number). The sampler records the day's first reading in
        # ``day_open_*``; prefer it when it belongs to this day.
        if isinstance(recv_state, dict):
            open_day = str(recv_state.get("day_open_day") or "")
            open_total = recv_state.get("day_open_total")
            if (
                open_day == day
                and isinstance(open_total, int)
                and (recv_total is None or open_total <= recv_total)
            ):
                recv_total = open_total
        bot = await collect_bot_info(self.plugin.context, None, self.config, recv_state)
        friends = bot.get("friends")
        groups = bot.get("groups")
        members = bot.get("members")
        if friends == 0 and groups == 0 and members == 0:
            # get_friend_list/get_group_list answering with an empty payload
            # yields 0 rather than None. Storing that pair locked the day's
            # snapshot at 0/0/0 (``complete=1`` stopped the retries and the
            # UPSERT kept the zeros), so the report showed no friends/groups
            # for a day that actually had them (v2.20.8). Treat all-zero as
            # "not collected yet" and retry; a bot with genuinely zero of
            # all three is not a real deployment.
            friends = groups = members = None
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
            # Once per 30 minutes, not every 5: a four-hour outage produced
            # ~48 identical warnings that buried everything else (v2.20.5).
            last = float(getattr(self, "_snapshot_warn_ts", 0.0) or 0.0)
            if time.time() - last >= 1800:
                self._snapshot_warn_ts = time.time()
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
        # The calendar day of the last attempt. ``_last_attempt_day`` names
        # the *report* day, which is yesterday for a scheduled run and can be
        # an older date for a retry; the catch-up guard needs to know whether
        # *today* was already attempted.
        self._last_attempt_wall_day: str | None = None
        # A day whose report was given up must not restart the retry cycle.
        # Without this marker the daily loop re-armed the catch-up after
        # every abandon and sent the report all night long (11 attempts and
        # two "stopped retrying" notices per evening).
        self._daily_given_up_day: str | None = None
        # The latest report day that failed because the transport itself was
        # down (ApiNotAvailable). Only the newest entry is kept: after a
        # multi-day outage the plugin retries the most recent report once and
        # lets the older ones go, so reconnecting never flushes a burst of
        # reports into the chat (v2.20.5).
        self._pending_recovery_day: str | None = None
        self._pending_recovery_since = 0.0
        # A configuration problem ("targets empty" / "sections empty") cannot
        # heal within the second the loop otherwise waits before re-entering
        # the send path: without a pause the daily loop spun at 1 Hz from the
        # scheduled time to midnight, rebuilding the report and fsyncing
        # state.json every second (the weekly loop already had this pause).
        self._daily_pause_until = 0.0
        self._weekly_pending_recovery_week: str | None = None
        self._weekly_pending_recovery_since = 0.0
        self._weekly_last_sent_week: str | None = None
        # The slot (YYYY-MM-DD HH:MM) of the weekly report that was last
        # delivered -- a SECOND line of defence, not the fix.
        #
        # The double send on 2026-11-01 happened because the loop computed
        # the report day from a clock snapshot taken before it slept, so
        # the slot send published 2026-10-24 (ISO week W43) and the next
        # iteration published 2026-10-31 (W44). Those are two DIFFERENT
        # slots (10-25 and 11-01), so no de-duplication key derived from
        # the delivery could have matched: the two reports belonged to
        # different weeks.
        #
        # The actual fix is to take the slot from ``target`` -- the moment
        # this iteration already decided to fire at (see ``_weekly_loop``),
        # which makes both attempts name the same report day and lets the
        # week-key guard do its job. This slot key is kept as an extra
        # guard: an exhaustive check over self-consistent states showed it
        # never blocks something the week key would not already block, so
        # it is redundant rather than load-bearing.
        self._weekly_last_sent_slot: str | None = None
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

        migrated = _safe_int(saved.get("schema")) < 2

        self._last_sent_day = saved.get("last_sent_day") or None
        self._last_attempt_day = saved.get("last_attempt_day") or None
        self._last_attempt_wall_day = saved.get("last_attempt_wall_day") or None
        self._daily_given_up_day = saved.get("daily_given_up_day") or None
        self._pending_recovery_day = saved.get("pending_recovery_day") or None
        self._weekly_pending_recovery_week = (
            saved.get("weekly_pending_recovery_week") or None
        )
        if migrated:
            # v2.20.5 migration: the day-scoped markers switched from "the
            # calendar day of the attempt" to "the report day". An old marker
            # can equal the report day that is due today, and the catch-up then
            # skipped the whole evening's report. The old code always wrote
            # ``daily_given_up_day`` with the calendar day, so shift it back
            # one; ``last_attempt_day`` may already be a report day (retries
            # wrote it that way), so only "today" is converted.
            if self._daily_given_up_day:
                try:
                    self._daily_given_up_day = (
                        date.fromisoformat(self._daily_given_up_day) - timedelta(days=1)
                    ).isoformat()
                except ValueError:
                    self._daily_given_up_day = None
            today_text = self._today()
            if self._last_attempt_day == today_text:
                try:
                    self._last_attempt_day = (
                        date.fromisoformat(today_text) - timedelta(days=1)
                    ).isoformat()
                except ValueError:
                    pass
            # The retry counter key was a calendar day; a fresh budget is the
            # safe interpretation (handled where the field is assigned below).
        for attribute, key in (
            ("_pending_recovery_since", "pending_recovery_since"),
            ("_weekly_pending_recovery_since", "weekly_pending_recovery_since"),
        ):
            raw = saved.get(key)
            try:
                setattr(self, attribute, float(raw) if raw else 0.0)
            except (TypeError, ValueError):
                setattr(self, attribute, 0.0)
        self._weekly_last_sent_week = saved.get("weekly_last_sent_week") or None
        self._weekly_last_sent_slot = saved.get("weekly_last_sent_slot") or None
        self._weekly_attempt_week = saved.get("weekly_attempt_week") or None
        self._weekly_given_up_week = saved.get("weekly_given_up_week") or None
        self._pending_retry_count = _safe_int(saved.get("pending_retry_count"))
        self._pending_retry_day = (
            None
            if _safe_int(saved.get("schema")) < 2
            else (saved.get("pending_retry_day") or None)
        )
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
        if self._pending_retry is not None:
            # A retry queued before a long shutdown must not fire days later:
            # the state file survived but the report is stale. Mirror the
            # weekly 6-hour rule and keep at most a two-day-old report day.
            pending_day, pending_due = self._pending_retry
            stale = False
            if pending_day:
                try:
                    stale = date.fromisoformat(pending_day) < (
                        now_in_timezone(self.config).date() - timedelta(days=2)
                    )
                except ValueError:
                    stale = True
            elif pending_due and pending_due < time.time() - 6 * 3600:
                stale = True
            if stale:
                logger.warning(
                    "丢弃过期的日报补发排程（报告日 %s）", pending_day or "--"
                )
                self._pending_retry = None
        weekly_pending = saved.get("weekly_pending")
        try:
            self._weekly_pending = float(weekly_pending) if weekly_pending else None
        except (TypeError, ValueError):
            self._weekly_pending = None
        if (
            self._weekly_pending is not None
            and self._weekly_pending < time.time() - 6 * 3600
        ):
            # A reload/kill right after a failed weekly send left the retry
            # timestamp in state.json. Restoring it unconditionally delivered
            # a week-old report at an arbitrary moment (e.g. Tuesday
            # afternoon). Only a retry from the trigger window is honoured
            # (v2.20.5).
            logger.warning(
                "丢弃过期的周报补发排程（报告日 %s）",
                self._weekly_pending_report_day or "--",
            )
            self._weekly_pending = None
            self._weekly_pending_report_day = None
        self._image_retry_attempts = _safe_int(saved.get("image_retry_attempts"))
        self._weekly_image_attempts = _safe_int(saved.get("weekly_image_attempts"))
        now_ts = time.time()
        for attribute, key in (
            ("_pending_image_retry", "pending_image_retry"),
            ("_weekly_image_pending", "weekly_image_pending"),
        ):
            raw = saved.get(key)
            try:
                value = float(raw) if raw else None
            except (TypeError, ValueError):
                value = None
            if value is not None and value < now_ts - 6 * 3600:
                # A process killed inside the 10-minute image-retry window
                # left its due timestamp behind; restoring it unconditionally
                # sent a day-old report at an arbitrary hour the next time the
                # plugin started. The text retry already had this 6-hour rule.
                logger.warning("丢弃过期的图片重试排程（%s）", key)
                value = None
                if attribute == "_pending_image_retry":
                    self._pending_image_retry_report_day = None
                    self._pending_image_retry_send_day = None
                else:
                    self._weekly_image_report_day = None
            setattr(self, attribute, value)
        if migrated:
            # Persist the converted markers so the migration runs exactly once
            # and the on-disk state reflects what the process is using
            # (v2.20.6).
            self._save_delivery_state()

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
                    "last_attempt_wall_day": self._last_attempt_wall_day,
                    "schema": 2,
                    "daily_given_up_day": self._daily_given_up_day,
                    "pending_recovery_day": getattr(
                        self, "_pending_recovery_day", None
                    ),
                    "pending_recovery_since": getattr(
                        self, "_pending_recovery_since", 0.0
                    ),
                    "weekly_pending_recovery_week": getattr(
                        self, "_weekly_pending_recovery_week", None
                    ),
                    "weekly_pending_recovery_since": getattr(
                        self, "_weekly_pending_recovery_since", 0.0
                    ),
                    "pending_retry": pending,
                    "pending_retry_count": self._pending_retry_count,
                    "pending_retry_day": self._pending_retry_day,
                    "weekly_last_sent_week": self._weekly_last_sent_week,
                    "weekly_last_sent_slot": self._weekly_last_sent_slot,
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
        if self._running or (self._task is not None and not self._task.done()):
            logger.debug("日报定时任务已在运行，忽略重复启动")
            return
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
                    # ``_pending_retry_day`` is the report day now, so compare
                    # against "yesterday" rather than "today": a retry for an
                    # older report is exactly the case that must keep running.
                    if _day and _day != self._default_report_day():
                        # Keep the pinned report day instead of dropping the
                        # retry: the same cross-midnight rule was already
                        # fixed for images, and a text report is still the
                        # same day's report.
                        logger.warning(
                            "日报 %s 的重试已跨天，继续用原报告日发送",
                            _day or "--",
                        )
                    if self._pending_recovery_day:
                        # An outage spanning a whole day leaves the retry
                        # pinned to the first report day it failed on. The
                        # loop only ever re-sent that (older) report, never
                        # reached the scheduled branch, and dropped the newest
                        # missed day for good. Only the newest one should be
                        # sent after a reconnect, so move the pin forward.
                        #
                        # The pin may only move to a report whose window has
                        # actually closed. ``_default_report_day()`` is the
                        # calendar yesterday, but the report for that day is
                        # not due until the NEXT day's send time: a retry
                        # that fired at 00:01 used to move the pin to the
                        # report of the new day, sent a report whose window
                        # had just opened (all "--"), wrote ``last_sent_day``
                        # for it and therefore skipped the real, complete
                        # report of that evening -- losing both (v2.20.12).
                        newest = self._latest_due_report_day()
                        if _day and newest > _day:
                            logger.info(
                                "日报 %s 的掉线补发已更新为 %s（只补最近一份）",
                                _day,
                                newest,
                            )
                            _day = newest
                            self._pending_recovery_day = newest
                            self._pending_recovery_since = time.time()
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
                pause_until = float(getattr(self, "_daily_pause_until", 0.0) or 0.0)
                if time.time() < pause_until:
                    # A config problem paused the send path; do not re-enter it
                    # every second (the catch-up guard alone kept re-arming).
                    await asyncio.sleep(min(60.0, max(1.0, pause_until - time.time())))
                    continue
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                catch_up_day = None
                if target <= now:
                    # An empty target list cannot heal on its own: without
                    # this gate the catch-up branch re-armed after every
                    # 10-minute pause and rebuilt the whole report 18 times
                    # before midnight (v2.20.17). Filling the target re-arms
                    # it immediately because this reads the live config.
                    if self._targets() and self._daily_catch_up_allowed(self._today()):
                        logger.info(
                            "日报定时点已过且今天尚未尝试，立即补发一次（%s）",
                            self._today(),
                        )
                        target = now
                    else:
                        target += timedelta(days=1)
                else:
                    # The process can be down through the whole evening and
                    # come back after midnight. The branch above only covers
                    # "tonight's slot already passed", so a 00:30 restart used
                    # to wait for the NEXT evening: the report that was due at
                    # 21:00 vanished without a trace (v2.20.17). Backfill it
                    # while its slot is still inside the same 8.5-hour window
                    # the weekly report uses.
                    catch_up_day = self._daily_missed_window_day()
                    if catch_up_day is not None:
                        # Remember that this process already tried it, even if
                        # the send fails: the retry/recovery queues own the
                        # report from here (see _daily_missed_window_day).
                        self._daily_backfill_day = catch_up_day
                        self._daily_backfill_inflight = True
                        logger.info(
                            "发现 %s 的日报未发送（停机跨过发送时间），立即补发",
                            catch_up_day,
                        )
                        target = now
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
                try:
                    await self._send_scheduled(catch_up_day)
                finally:
                    self._daily_backfill_inflight = False
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
                        await self._send_weekly_scheduled(
                            report_day, self._weekly_slot_for_report_day(report_day)
                        )
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
                        await self._send_weekly_scheduled(
                            report_day, self._weekly_slot_for_report_day(report_day)
                        )
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
                # A temporary pause (database/config problem) must delay the
                # attempt, not push the target to next week: the old code
                # advanced it by seven days and the whole week was lost even
                # after the database healed (v2.20.8).
                pause_until = float(getattr(self, "_weekly_pause_until", 0.0) or 0.0)
                if time.time() < pause_until:
                    await asyncio.sleep(min(60.0, max(1.0, pause_until - time.time())))
                    continue
                caught_up = False
                if self._targets("weekly") and self._weekly_catch_up_allowed():
                    # Covers Sunday nights *and* a Monday-morning restart: the
                    # slot is in the past but still inside the retry window,
                    # so send now instead of waiting for next Sunday.
                    logger.info("周报定时点已过且本期尚未发送，立即补发一次")
                    target = now
                    caught_up = True
                elif target <= now:
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
                # The report day comes from the slot, not from the calendar:
                # the weekly report covers the seven days that END on the
                # Saturday before the slot. Deriving it from "yesterday" gave
                # the catch-up run (Monday 00:30) a window one day later than
                # the Sunday 21:30 send -- the same week key, a different
                # window (v2.20.14).
                #
                # The slot is ``target`` -- the moment this iteration
                # already decided to fire at -- and NOT a fresh wall-clock
                # read. Both earlier attempts to derive it from the clock
                # failed, in two different ways:
                #
                # 1. From the snapshot taken at the top of the iteration:
                #    the loop sleeps up to 60 seconds to reach the slot, so
                #    at 21:29:59 the slot is still in the future and
                #    ``_weekly_slot_before`` rolls back seven days, naming
                #    LAST week's report day (2026-10-24, W43). The send
                #    published that older window and recorded W43 as sent;
                #    the next iteration recomputed the correct day
                #    (2026-10-31, W44), a different week key, so the
                #    "already sent" guard could not stop it and a SECOND
                #    weekly report went out (2026-11-01 21:30).
                #
                # 2. From a fresh wall-clock read at the sending moment:
                #    that only narrowed the window to the microsecond. A
                #    wall clock stepped backwards during the sleep (NTP
                #    step, manual clock change) puts the read before the
                #    slot again and the same double send returns.
                #
                # ``target`` is immune to both: it was computed from the
                # configured time and pushed to the coming Sunday, so it
                # names the slot this iteration is firing for no matter
                # what the clock does during the sleep.
                #
                # The catch-up branch is the exception: it sets
                # ``target = now`` (send immediately) because the slot is
                # in the past but still inside the retry window. There the
                # slot has to be derived from the clock again.
                catchup_day = None
                if caught_up:
                    slot = self._weekly_slot_before(now_in_timezone(self.config))
                else:
                    slot = target
                if slot is not None:
                    catchup_day = (slot - timedelta(days=1)).date().isoformat()
                await self._send_weekly_scheduled(
                    catchup_day, self._weekly_slot_key(slot)
                )
                last_logged = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("周报定时任务失败")
                await asyncio.sleep(60)

    def _default_report_day(self) -> str:
        return (now_in_timezone(self.config) - timedelta(days=1)).date().isoformat()

    def _latest_due_report_day(self) -> str:
        """The newest report day whose window has already closed.

        The report for report day D is due at D+1's send time: a report
        scheduled for 21:00 covers 00:00-24:00 of the previous day and is
        therefore only complete once the next evening has arrived. The
        calendar yesterday (``_default_report_day``) is NOT necessarily due
        yet -- at 00:01 on 11-01 it names 10-31, whose report is not sent
        until 11-01 21:00.

        The offline-recovery path moves its pinned report day forward so a
        multi-day outage delivers the newest report instead of the oldest
        one. Moving it to a day that is not due yet sends a report whose
        window has just opened (all "--"), marks that day as sent and
        therefore skips the real, complete report of that evening (v2.20.12).

        The result is the latest report day D with
        ``now >= (D + 1 day) at the configured send time``; when the send
        time is invalid the calendar yesterday is used, because the daily
        loop cannot know when the report is due then.
        """

        now = now_in_timezone(self.config)
        fallback = self._default_report_day()
        try:
            time_text = self._time_text()
            hour, minute = [int(part) for part in time_text.split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("out of range")
        except (TypeError, ValueError):
            # The send time cannot be parsed, so the loop cannot know when a
            # report is due. The calendar yesterday is NOT a safe fallback:
            # it is the very value this function exists to avoid (a report
            # whose window has just opened). Returning the day before it
            # keeps the pin where it is -- a report that is one day late is
            # much better than one that is empty and locks the evening
            # (v2.20.12).
            try:
                return (date.fromisoformat(fallback) - timedelta(days=1)).isoformat()
            except ValueError:
                return fallback
        # The candidate is the calendar yesterday; walk back until its window
        # has closed. At most a few iterations are needed (the send time can
        # be later in the day than the current time).
        try:
            candidate = date.fromisoformat(fallback)
        except ValueError:
            return fallback
        tz = now.tzinfo
        for _ in range(4):
            due = datetime(
                candidate.year,
                candidate.month,
                candidate.day,
                hour,
                minute,
                tzinfo=tz,
            ) + timedelta(days=1)
            if now >= due:
                return candidate.isoformat()
            candidate -= timedelta(days=1)
        return fallback

    def _schedule_weekly_retry(
        self,
        transport_offline: bool = False,
        store_unavailable: bool = False,
    ) -> bool:
        # Pin the report day to the slot BEFORE computing the week key.
        #
        # The content-retry branch used to leave ``_weekly_pending_report_day``
        # unset and derive the day from the clock at retry time, so a build
        # failure at Sunday 21:30 followed by a reload at Monday 00:20 built
        # "yesterday's" week: the window moved one day forward while the ISO
        # week key stayed the same, so de-duplication could not save it
        # (v2.20.17).
        slot = self._weekly_slot_before(now_in_timezone(self.config))
        report_day = getattr(self, "_weekly_pending_report_day", None)
        if slot is not None:
            report_day = (slot - timedelta(days=1)).date().isoformat()
        if not report_day:
            report_day = self._default_report_day()
        self._weekly_pending_report_day = report_day
        week_key = self._week_key(report_day)
        if transport_offline:
            if (
                getattr(self, "_weekly_pending_recovery_week", None)
                and self._weekly_pending_recovery_week != week_key
            ):
                logger.warning(
                    "周报 %s 的补发已被更新的 %s 取代（恢复后只补最近一期）",
                    self._weekly_pending_recovery_week,
                    week_key,
                )
            self._weekly_pending_recovery_week = week_key
            if not self._weekly_pending_recovery_since:
                self._weekly_pending_recovery_since = time.time()
            # The report day was pinned to the slot at the top of this
            # function, so the queued report keeps the window the slot
            # announced even when the retry crosses midnight.
            self._weekly_pending = time.time() + 900
            self._save_delivery_state()
            logger.warning(
                "周报发送失败（Bot 连接不可用），已排队，恢复后补发最近这一期"
            )
            return True
        if store_unavailable:
            # The database heals on its own, so the content retry budget
            # stays untouched. The weekly side used to queue nothing at all
            # here: the only recovery path was the catch-up guard, whose
            # window is 8.5 hours after the slot, so a longer database
            # outage made the whole weekly report disappear silently
            # (v2.20.12). The report day was pinned at the top of this
            # function (v2.20.17).
            self._weekly_pending = time.time() + 900
            self._save_delivery_state()
            logger.warning("周报发送失败（日报数据库不可用），已排队，数据库恢复后补发")
            return True
        self._weekly_retry_count += 1
        if self._weekly_retry_count > 3:
            self._weekly_pending = None
            self._weekly_pending_report_day = None
            self._weekly_retry_count = 0
            # Remember the week that was abandoned: without it the catch-up
            # check instantly queued another attempt after the give-up notice.
            self._weekly_given_up_week = week_key
            self._save_delivery_state()
            logger.warning("周报重试 3 次后仍失败，停止重试")
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

    async def _send_weekly_scheduled(
        self, day: str | None = None, slot: str | None = None
    ) -> None:
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
                self._record(
                    day or self._default_report_day(), False, "weekly sections empty"
                )
            self._weekly_pause_until = time.time() + 600
            self._clear_retry()
            return
        targets = self._targets("weekly")
        if not targets:
            # Same as the daily report: an empty target list cannot heal, and
            # building a seven-day window first only made the loop rebuild it
            # every ten minutes all night (v2.20.17).
            logger.warning("周报推送目标为空，跳过发送（请先在配置里填写推送目标）")
            self._weekly_retry_count = 0
            self._weekly_pause_until = time.time() + 600
            self._save_delivery_state()
            return
        if not self.store.probe() or not self.store.ensure_ready():
            # ``probe()`` MUST run before ``ensure_ready()`` (v2.20.14):
            # probe() touches a real table and fails on a database whose file
            # was deleted -- the empty file sqlite silently recreates has no
            # tables yet. ensure_ready() rebuilds the schema in that empty
            # file and flips ``available`` back to True, so a later attempt
            # saw a "healthy" but empty database, built an all-"--" report,
            # sent it and recorded the day as delivered: the real data of
            # that day could never be sent afterwards.
            logger.error("日报数据库不可用，跳过本次周报发送")
            await self._notify_store_unavailable(self._targets("weekly"), weekly=True)
            # A missing/unreadable database is not going to heal within the
            # second the loop otherwise waits before re-entering this function:
            # without a pause the loop spun at 1Hz from 21:30 to midnight,
            # fsync-ing state.json and logging ~18k lines each Sunday. Back off
            # like the empty-target/sections branches (a fresh attempt still
            # runs after the pause).
            self._weekly_pause_until = time.time() + 600
            # Queue the report instead of dropping it. The weekly side used
            # to queue nothing here, so the only recovery path was the
            # catch-up guard and its 8.5 hour window: a longer database
            # outage made the whole weekly report disappear without a
            # trace (v2.20.12).
            self._schedule_weekly_retry(store_unavailable=True)
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
        if slot and self._weekly_last_sent_slot == slot:
            # A redundant second guard: the same slot must not be delivered
            # twice. The week-key check above is the one that stops the
            # 2026-11-01 double send, and it works because the loop now
            # computes the report day at the moment of sending -- both
            # attempts then name the same report day and therefore the
            # same week key.
            logger.info("周报时段 %s 已发送过，跳过重复发送", slot)
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
                    self._weekly_last_sent_slot = slot
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
                    self._weekly_pending_recovery_week = None
                    self._weekly_pending_recovery_since = 0.0
                    self._save_delivery_state()
                    logger.info("周报已发送（图片）：%s", targets)
                    return
                if _uncertain(result):
                    logger.warning(
                        "周报图片发送未确认（无法确认是否送达），已按不重发处理以免重复"
                    )
                    self._weekly_last_sent_week = week_key
                    self._weekly_last_sent_slot = slot
                    self._weekly_given_up_week = None
                    self._record(day, True, "weekly-image unconfirmed")
                    self._weekly_pending = None
                    self._weekly_pending_report_day = None
                    self._weekly_retry_count = 0
                    self._weekly_image_attempts = 0
                    self._weekly_image_pending = None
                    self._weekly_image_report_day = None
                    self._weekly_pending_recovery_week = None
                    self._weekly_pending_recovery_since = 0.0
                    self._save_delivery_state()
                    return
        result = await self.plugin.send_text(
            targets, build_report_text(view), source="weekly_report"
        )
        if _delivered(result):
            self._weekly_last_sent_week = week_key
            self._weekly_last_sent_slot = slot
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
            self._weekly_pending_recovery_week = None
            self._weekly_pending_recovery_since = 0.0
            self._save_delivery_state()
            logger.info("周报已发送（文字）：%s", targets)
        elif _uncertain(result):
            logger.warning("周报文字发送未确认（无法确认是否送达），已按不重发处理")
            self._weekly_last_sent_week = week_key
            self._weekly_last_sent_slot = slot
            self._record(day, True, "weekly-text unconfirmed")
            self._weekly_pending = None
            self._weekly_pending_report_day = None
            self._weekly_retry_count = 0
            self._weekly_image_attempts = 0
            self._weekly_image_pending = None
            self._weekly_image_report_day = None
            self._weekly_pending_recovery_week = None
            self._weekly_pending_recovery_since = 0.0
            self._save_delivery_state()
        else:
            self._record(day, False, "weekly send failed")
            logger.warning("周报发送失败：%s", targets)
            offline = bool(
                isinstance(result, DeliveryResult) and result.transport_offline
            )
            if not self._schedule_weekly_retry(transport_offline=offline):
                await self._notify_give_up(targets, day, weekly=True)

    async def _notify_store_unavailable(
        self, targets: list[str], weekly: bool = False
    ) -> None:
        """Tell the operator at most once per day that report.db is unusable."""

        key = "weekly_db_unavailable" if weekly else "report_db_unavailable"

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
                tr(self.config, key, day=today),
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

    def _schedule_retry(
        self,
        day: str | None,
        transport_offline: bool = False,
        store_unavailable: bool = False,
        poll_offline: bool = True,
    ) -> bool:
        """Remember a failed delivery and queue the next attempt.

        ``day`` is ``None`` for the scheduled run, which resolves "yesterday"
        itself. Callers used to pass ``day or ""``, so a build failure queued
        an empty string and every retry then died in ``date.fromisoformat("")``
        -- the retry budget was spent without ever producing a report.

        ``transport_offline`` means the delivery failed because the bot was
        disconnected (ApiNotAvailable), not because of the content. Those
        failures must not consume the retry budget: a four-hour outage used to
        burn all four attempts in the first hour and silently drop the report
        for good. Instead the newest report day is kept in
        ``pending_recovery_day`` and retried once the connection is back. Only
        the newest day is kept, so reconnecting after several missed days
        sends one report rather than a burst (v2.20.5).

        ``store_unavailable`` means the report database could not be read.
        That is the same kind of "it will heal on its own" failure as a
        disconnected channel (a full disk, a read-only mount, a container
        being rebuilt), so it must not consume the content retry budget
        either: the three attempts (10/20/30 minutes) were spent within an
        hour and the report was given up for good, while the database was
        still broken (v2.20.12).

        Returns ``True`` when another attempt was scheduled and ``False``
        when the retry budget for this report day is exhausted.
        """

        budget_key = day or self._default_report_day()
        # The budget belongs to the report day, not the calendar day. A retry
        # chain that crosses midnight is the same attempt series; resetting
        # the counter at 00:00 gave a report up to seven attempts across two
        # hours while the log still said "3 次" (v2.20.5).
        if getattr(self, "_pending_retry_day", None) != budget_key:
            self._pending_retry_day = budget_key
            self._pending_retry_count = 0
        if transport_offline:
            if self._pending_recovery_day and self._pending_recovery_day != budget_key:
                logger.warning(
                    "日报 %s 的补发已被更新的 %s 取代（恢复后只补最近一天）",
                    self._pending_recovery_day,
                    budget_key,
                )
            self._pending_recovery_day = budget_key
            if not self._pending_recovery_since:
                self._pending_recovery_since = time.time()
            # A long outage has no "next retry" that can succeed; poll every
            # 15 minutes as a fallback in case the reconnect hook is missed.
            # The post-midnight backfill passes poll_offline=False: it already
            # waited once and a 15-minute poll only rebuilt the report 24
            # times before 06:00 (v2.20.17 audit).
            self._pending_retry = (
                (budget_key, time.time() + 900) if poll_offline else None
            )
            self._save_delivery_state()
            logger.warning(
                "日报 %s 发送失败（Bot 连接不可用），已排队，恢复后补发最近这一份",
                budget_key,
            )
            return True
        if store_unavailable:
            # The database is temporarily unreadable. Like a disconnected
            # channel this heals on its own, so the content retry budget
            # stays untouched and the report is polled every 15 minutes
            # instead of being given up after three attempts (v2.20.12).
            self._pending_retry = (budget_key, time.time() + 900)
            self._save_delivery_state()
            logger.warning(
                "日报 %s 发送失败（日报数据库不可用），已排队，数据库恢复后补发",
                budget_key,
            )
            return True
        self._pending_retry_count += 1
        if self._pending_retry_count > 3:
            self._pending_retry = None
            self._pending_retry_count = 0
            # Mark the report day as given up: the weekly report has the same
            # marker, and without it the daily loop re-armed the catch-up
            # after every abandon and sent the report all night long.
            self._daily_given_up_day = budget_key
            # An earlier transport failure could have left the same day in
            # the offline-recovery queue. Without clearing it, the next
            # reconnect re-queued a report that was already given up and the
            # loop sent it anyway -- bypassing the give-up guard (v2.20.17).
            if getattr(self, "_pending_recovery_day", None) == budget_key:
                self._pending_recovery_day = None
                self._pending_recovery_since = 0.0
            self._save_delivery_state()
            logger.warning("日报 %s 重试 3 次后仍失败，停止重试", budget_key)
            return False
        delay = 600 * self._pending_retry_count
        self._pending_retry = (budget_key, time.time() + delay)
        self._save_delivery_state()
        logger.warning(
            "日报 %s 发送失败，%d 分钟后重试（第 %d 次）",
            budget_key,
            delay // 60,
            self._pending_retry_count,
        )
        return True

    def _clear_retry(self, clear_weekly: bool = False) -> None:
        changed = (
            self._pending_retry is not None
            or bool(self._pending_retry_count)
            or self._pending_retry_day is not None
            or getattr(self, "_pending_recovery_day", None) is not None
            or bool(getattr(self, "_pending_recovery_since", 0.0))
            or self._pending_image_retry_report_day is not None
            or self._pending_image_retry_send_day is not None
        )
        self._pending_retry = None
        self._pending_retry_count = 0
        self._pending_retry_day = None
        self._pending_recovery_day = None
        self._pending_recovery_since = 0.0
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

    def notify_offline_recovery(self) -> bool:
        """Queue the newest missed report right after the bot reconnects.

        Called by the alert engine when the connection is back. Only the most
        recent report day/week is retried; anything older is deliberately
        abandoned so a multi-day outage never flushes a burst of reports into
        the chat (the user's explicit requirement, v2.20.5).
        """

        queued = False
        day = self._pending_recovery_day
        if day:
            age = time.time() - float(self._pending_recovery_since or 0.0)
            if age > 3 * 86400:
                logger.warning("日报 %s 的补发已超过 3 天，放弃", day)
                self._pending_recovery_day = None
                self._pending_recovery_since = 0.0
                self._pending_retry = None
            else:
                self._pending_retry = (day, time.time())
                queued = True
        if self._weekly_pending_recovery_week:
            self._weekly_pending = time.time()
            queued = True
        if queued:
            self._save_delivery_state()
            logger.info("Bot 连接恢复：已安排补发最近一份未送达的报告")
        return queued

    def _daily_catch_up_allowed(self, today: str) -> bool:
        """Whether a missed daily-report window may be sent on start-up.

        A missing marker means this is a first install or an upgrade from a
        version without the marker; sending then would duplicate a report the
        previous process may already have delivered. Once a marker exists,
        only the *report day* protects against a reload loop. The previous
        implementation compared a calendar-day marker with "today", so a
        retry that crossed midnight (for an older report) marked the new day
        as already attempted and the report due that evening was skipped for
        the whole day (v2.20.5).
        """

        if not self._last_attempt_day:
            return False
        try:
            # The caller passes today; the report due today is yesterday's.
            # Deriving it from the argument (instead of the wall clock) keeps
            # the check deterministic in tests and across timezone edges.
            report_day = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        except ValueError:
            report_day = self._default_report_day()
        if self._last_sent_day == report_day:
            return False
        if self._daily_given_up_day == report_day:
            return False
        if self._last_attempt_day == report_day:
            # The attempt marker is written before the slow work. If the
            # process was restarted mid-send (config reload at 21:00, OOM,
            # deploy), the marker survives while neither a delivery nor a
            # retry plan exists -- the day used to be skipped silently.
            # Allow exactly one catch-up attempt; a failure queues the normal
            # retry ladder and blocks this branch on the next restart.
            return bool(
                self._last_sent_day != report_day
                and getattr(self, "_pending_retry", None) is None
                and not getattr(self, "_pending_recovery_day", None)
            )
        return True

    def _daily_missed_window_day(self) -> str | None:
        """The report day whose evening slot was missed, still retryable.

        A restart after midnight computes today's slot as "later tonight" and
        waits, so a process that was down through the evening silently lost
        that day's report. This returns the newest due report day while its
        slot is still inside an 8.5-hour catch-up window (the same window the
        weekly report uses), and ``None`` in every other case.
        """

        if not self._last_attempt_day:
            # First install or an upgrade without markers: the previous
            # process may already have delivered it. Same rule as
            # ``_daily_catch_up_allowed``.
            return None
        if getattr(self, "_pending_retry", None) is not None:
            return None
        if getattr(self, "_pending_recovery_day", None):
            return None
        if not self._targets():
            return None
        sections = getattr(self, "_sections", None)
        if callable(sections):
            selected = sections()
            if selected is not None and not selected:
                # An empty section list cannot produce a report; without this
                # the 10-minute pause re-armed the backfill from 00:00 to
                # 05:30 and wrote ~30 records (v2.20.17 audit).
                return None
        report_day = self._latest_due_report_day()
        if not report_day or report_day == self._last_sent_day:
            return None
        if report_day == self._daily_given_up_day:
            return None
        if report_day == getattr(self, "_daily_backfill_day", None):
            # This process already tried this backfill once. A transport
            # outage keeps the report queued (pending_recovery/pending_retry)
            # and must not rebuild + re-render it every 15 minutes until the
            # 8.5-hour window ends (v2.20.17 audit).
            return None
        # ``report_day == self._last_attempt_day`` is NOT a blocker by itself:
        # the marker is written before the slow work, so a restart in the
        # middle of the send leaves exactly that marker with no delivery and
        # no retry plan. Exactly one catch-up is allowed then, mirroring
        # ``_daily_catch_up_allowed``. A queued retry is handled above.

        try:
            hour, minute = [int(part) for part in self._time_text().split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            slot_day = date.fromisoformat(report_day) + timedelta(days=1)
            slot = datetime(
                slot_day.year,
                slot_day.month,
                slot_day.day,
                hour,
                minute,
                tzinfo=now_in_timezone(self.config).tzinfo,
            )
        except (TypeError, ValueError):
            return None
        now = now_in_timezone(self.config)
        if now < slot or now - slot > timedelta(hours=8, minutes=30):
            return None
        return report_day

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

        The catch-up window is derived from the configured ``weekly_time``,
        not hard-coded to 21:30: a slot is "missed but still retryable" for
        8.5 hours after it starts (21:30 -> next 06:00), which covers a
        Sunday-night outage and a Monday 00:00-06:00 restart. The old code
        only evaluated the guard when ``target <= now``; a Monday restart
        computes target as *next* Sunday, so that branch was unreachable and
        the whole week was silently skipped (v2.20.8).

        The report day is derived from the slot (Sunday slot -> Saturday),
        not from "today - 1": on Monday that arithmetic landed on Sunday and
        the ISO week key pointed at the new week.

        An attempt marker alone must NOT block the catch-up: it is written
        before the slow work, so a crash/reload mid-send leaves exactly that
        marker. A genuine first install is also allowed inside the window --
        one possible duplicate beats silently skipping the first report.
        """

        if time.time() < float(getattr(self, "_weekly_pause_until", 0.0) or 0.0):
            # A config problem (empty target/sections) paused the loop for a
            # few minutes instead of re-entering the send path every second.
            return False
        now = now_in_timezone(self.config)
        slot = self._weekly_slot_before(now)
        if slot is None:
            return False
        if now - slot > timedelta(hours=8, minutes=30):
            return False
        report_day = (slot - timedelta(days=1)).date().isoformat()
        week_key = self._week_key(report_day)
        if getattr(self, "_weekly_given_up_week", None) == week_key:
            # Already gave up after three failed attempts this week; retrying
            # again would produce the same notice every ~40 minutes.
            return False
        return self._weekly_last_sent_week != week_key

    def _weekly_slot_before(self, now: datetime) -> datetime | None:
        """The most recent weekly trigger slot (this Sunday or last Sunday)."""

        text = self._weekly_time_text()
        try:
            hour, minute = [int(part) for part in text.split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
        except (TypeError, ValueError):
            return None
        slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        slot += timedelta(days=(6 - slot.weekday()) % 7)
        if slot > now:
            slot -= timedelta(days=7)
        return slot

    def _weekly_slot_key(self, slot: datetime | None) -> str | None:
        """A stable string for a weekly slot, used as the de-duplication key."""

        if slot is None:
            return None
        return slot.strftime("%Y-%m-%d %H:%M")

    def _weekly_slot_for_report_day(self, report_day: str | None) -> str | None:
        """The slot that owns a given report day.

        A weekly report day is the Saturday before its slot, so the slot is
        the next day at the configured weekly time. The retry paths already
        pin the report day, so they need the slot of THAT day -- deriving it
        from the current clock would move the key forward every time the
        retry fires and the slot guard would never match.
        """

        if not report_day:
            return None
        try:
            day = date.fromisoformat(str(report_day))
        except ValueError:
            return None
        text = self._weekly_time_text()
        try:
            hour, minute = [int(part) for part in text.split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("out of range")
        except (TypeError, ValueError):
            return None
        slot = datetime(
            day.year,
            day.month,
            day.day,
            hour,
            minute,
            tzinfo=get_timezone(self.config),
        ) + timedelta(days=1)
        return self._weekly_slot_key(slot)

    async def _send_scheduled(self, day: str | None = None) -> None:
        # Whether this send is the post-midnight backfill. A transport outage
        # during the normal 21:00 slot keeps the historical 15-minute polling
        # (pending_retry), but doing that at 00:30 rebuilt and re-rendered the
        # report 24 times in six hours (v2.20.17 audit). The backfill only
        # leaves the report in the recovery queue instead.
        backfill = bool(getattr(self, "_daily_backfill_inflight", False))
        # Two separate markers, because they answer different questions:
        # ``_last_attempt_day`` = "which report day was last attempted" and
        # ``_last_attempt_wall_day`` = "on which calendar day was the last
        # attempt made". The catch-up guard needs the *calendar* day: using
        # the report day let a cross-midnight retry mark *yesterday* as
        # attempted, which re-armed the catch-up and made the report loop
        # re-send all night (a give-up notice was followed by another report
        # one second later).
        self._last_attempt_day = day or self._default_report_day()
        self._last_attempt_wall_day = self._today()
        self._save_delivery_state()
        targets = self._targets()
        if self._sections() is not None and not self._sections():
            logger.warning("日报内容为空（daily_report.sections=[]），跳过发送")
            # Record the day that was actually attempted (a pinned retry may be
            # older than yesterday) and pause the loop: without the pause the
            # catch-up guard re-armed every second until midnight.
            self._record(day or self._default_report_day(), False, "sections empty")
            self._daily_pause_until = time.time() + 600
            self._clear_retry()
            return
        if not targets:
            # An empty target list is a configuration problem that will not
            # heal on its own; retrying only wrote "targets empty" rows and
            # the give-up notice went nowhere anyway. Check BEFORE building:
            # the old position was after the full report build, so every
            # catch-up attempt rebuilt the whole day for nothing (v2.20.17).
            logger.warning("日报推送目标为空，跳过发送（请先在配置里填写推送目标）")
            self._record(day or self._default_report_day(), False, "targets empty")
            self._daily_pause_until = time.time() + 600
            self._clear_retry()
            return
        if not self.store.probe() or not self.store.ensure_ready():
            # A damaged or read-only database would otherwise produce a page
            # full of "--" and send it as if it were a real report.
            #
            # ``probe()`` MUST run before ``ensure_ready()`` (v2.20.14):
            # probe() touches a real table and fails on a database whose file
            # was deleted -- the empty file sqlite silently recreates has no
            # tables yet. ensure_ready() rebuilds the schema in that empty
            # file and flips ``available`` back to True, so the queued retry
            # 15 minutes later saw a "healthy" but empty database, built an
            # all-"--" report, sent it to the user and recorded the day as
            # delivered: the real data of that day could never be sent
            # afterwards.
            logger.error("日报数据库不可用，跳过本次日报发送")
            self._record(day or self._default_report_day(), False, "db unavailable")
            # A database outage is usually transient (disk full, permissions,
            # a half-written WAL): it heals on its own, so it must not
            # consume the content retry budget. The three attempts (10/20/30
            # minutes) were spent inside an hour and the report was given up
            # for good while the store was still broken (v2.20.12).
            self._schedule_retry(
                day or self._default_report_day(), store_unavailable=True
            )
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
                    self._daily_given_up_day = None
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
                    self._daily_given_up_day = None
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
            self._daily_given_up_day = None
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
            self._daily_given_up_day = None
            self._record(day, False, "text unconfirmed")
            self._image_retry_attempts = 0
            self._pending_image_retry = None
            self._clear_retry()
            self._save_delivery_state()
        else:
            self._record(day, False, "send failed")
            logger.warning("日报发送失败：%s", targets)
            offline = bool(
                isinstance(result, DeliveryResult) and result.transport_offline
            )
            if not self._schedule_retry(
                day, transport_offline=offline, poll_offline=not backfill
            ):
                await self._notify_give_up(targets, day, weekly=False)

    async def preview(
        self, target_umo: str | None, span_days: int = 1, day: str | None = None
    ) -> tuple[bool, str]:
        if not target_umo:
            return False, tr(self.config, "report_no_session")
        if not self.store.probe() or not self.store.ensure_ready():
            # Preview used to build a page of "--" and send it as if it were
            # a real report; the scheduled path already refuses to.
            # The order matters (v2.20.14): probe() must detect the missing
            # schema before ensure_ready() rebuilds an empty database.
            return False, tr(self.config, "report_db_unavailable_preview")
        span = max(1, min(30, int(span_days or 1)))
        prefix = "report" if span == 1 else "weekly"
        source = "report_preview" if span == 1 else "weekly_preview"
        try:
            preview_sections = (
                self._sections() if span == 1 else self._weekly_sections()
            )
            view = await asyncio.to_thread(
                self._build_view,
                day=day,
                span_days=span,
                sections=preview_sections,
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
