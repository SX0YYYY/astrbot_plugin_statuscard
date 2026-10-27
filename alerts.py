from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psutil
from astrbot.api import logger

from .collectors import (
    _call_action,
    _get_aiocqhttp_client,
    _normalize_object,
    cached_cpu_percent,
    cfg_bool,
    cfg_float,
    cfg_get,
    cfg_int,
    format_bytes,
    format_duration,
    memory_stat,
    now_in_timezone,
    swap_stat,
)
from .delivery import DeliveryResult
from .redactor import HIDDEN, read_rules
from .state import StateStore
from .utils import clamp_config, normalize_targets, retry_delay, truncate_text


def _now_text(config: Any = None) -> str:
    if config is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return now_in_timezone(config).strftime("%Y-%m-%d %H:%M:%S")


class AlertEngine:
    """Threshold, offline, daily-summary and Komari webhook alerts.

    All alerts are opt-in. The plugin ships with ``alerts.enabled = false``.
    """

    def __init__(
        self,
        plugin: Any,
        state: StateStore,
        config: Any,
        report_store: Any = None,
    ):
        self.plugin = plugin
        self.state = state
        self.config = config
        self.report_store = report_store
        self.interval = clamp_config(
            cfg_int(config, "alerts.interval_seconds", 60),
            15,
            600,
            "alerts.interval_seconds",
        )
        self.sustain = clamp_config(
            cfg_int(config, "alerts.sustain_samples", 3),
            1,
            10,
            "alerts.sustain_samples",
        )
        self.cooldown = max(
            0, min(86400, cfg_int(config, "alerts.cooldown_seconds", 1800))
        )
        self.recover_notify = cfg_bool(config, "alerts.recover_notify", True)
        self.offline_notify = cfg_bool(config, "alerts.offline_notify", True)
        self.cpu_threshold = clamp_config(
            cfg_float(config, "alerts.cpu_threshold", 85.0),
            0.0,
            100.0,
            "alerts.cpu_threshold",
        )
        self.mem_threshold = clamp_config(
            cfg_float(config, "alerts.mem_threshold", 90.0),
            0.0,
            100.0,
            "alerts.mem_threshold",
        )
        self.swap_threshold = clamp_config(
            cfg_float(config, "alerts.swap_threshold", 90.0),
            0.0,
            100.0,
            "alerts.swap_threshold",
        )
        self.disk_threshold = clamp_config(
            cfg_float(config, "alerts.disk_threshold", 85.0),
            0.0,
            100.0,
            "alerts.disk_threshold",
        )
        self.load_threshold = clamp_config(
            cfg_float(config, "alerts.load_per_core_threshold", 2.0),
            0.0,
            16.0,
            "alerts.load_per_core_threshold",
        )
        self.platform_id = str(
            cfg_get(config, "alerts.platform_id", "default") or "default"
        )
        self.targets = normalize_targets(
            cfg_get(config, "alerts.targets", []), self.platform_id
        )
        self._monitor_task: asyncio.Task | None = None
        self._daily_task: asyncio.Task | None = None
        self._webhook_task: asyncio.Task | None = None
        self._running = False
        self._webhook_runner: Any = None
        self._webhook_site: Any = None
        self._primed = False
        self._daily_last_sent_day: str | None = None
        self._daily_last_attempt_day: str | None = None
        self._next_daily_text: str | None = None
        try:
            alerts_state = self.state.get("alerts", {}) or {}
            summary_state = (
                alerts_state.get("daily_summary")
                if isinstance(alerts_state, dict)
                else None
            )
            if isinstance(summary_state, dict):
                sent = summary_state.get("last_sent_day")
                attempted = summary_state.get("last_attempt_day")
                self._daily_last_sent_day = sent if isinstance(sent, str) else None
                self._daily_last_attempt_day = (
                    attempted if isinstance(attempted, str) else None
                )
        except Exception:
            logger.debug("读取每日定时状态图发送标记失败", exc_info=True)

    def _mark_daily_sent(self, day: str) -> None:
        self._daily_last_sent_day = day
        self._daily_last_attempt_day = day
        self._save_daily_markers(day, day)

    def _daily_catch_up_allowed(self, today_text: str) -> bool:
        """Whether a missed window today should be sent right after startup.

        No marker at all means a fresh install or an upgrade from a version
        that kept the marker in memory only; that day's image was most likely
        already sent, so it is not re-sent.
        """

        return (
            self._daily_last_attempt_day is not None
            and self._daily_last_attempt_day != today_text
        )

    def _mark_daily_attempt(self, day: str) -> None:
        self._daily_last_attempt_day = day
        self._save_daily_markers(self._daily_last_sent_day, day)

    def _save_daily_markers(self, sent: str | None, attempted: str) -> None:
        try:
            with self.state.section("alerts") as alerts:
                summary = alerts.get("daily_summary")
                if not isinstance(summary, dict):
                    summary = {}
                    alerts["daily_summary"] = summary
                if sent:
                    summary["last_sent_day"] = sent
                summary["last_attempt_day"] = attempted
            self.state.save()
        except Exception:
            logger.debug("保存每日定时状态图发送标记失败", exc_info=True)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _compute_daily_target(self) -> datetime | None:
        """Return the next daily-summary time, or None if config is invalid."""

        try:
            time_text = str(
                cfg_get(self.config, "alerts.daily_summary.time", "09:00") or "09:00"
            ).strip()
            hour, minute = [int(x) for x in time_text.split(":", 1)]
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return None
            now = now_in_timezone(self.config)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return target
        except Exception:
            return None

    def _log_next_daily(self) -> None:
        target = self._compute_daily_target()
        if target is None:
            return
        self._next_daily_text = target.strftime("%Y-%m-%d %H:%M")
        logger.info("每日定时状态图将在 %s 发送", self._next_daily_text)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("状态卡后台任务异常退出: %r", exc, exc_info=exc)

    def start(self) -> None:
        alerts_on = cfg_bool(self.config, "alerts.enabled", False)
        daily_on = cfg_bool(self.config, "alerts.daily_summary.enabled", False)
        self._running = alerts_on or daily_on
        if alerts_on:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            self._monitor_task.add_done_callback(self._on_task_done)
        if daily_on:
            self._daily_task = asyncio.create_task(self._daily_loop())
            self._daily_task.add_done_callback(self._on_task_done)
            self._log_next_daily()
        self._start_webhook_task()
        if not self._running:
            logger.info("状态卡主动告警与每日定时状态图均未启用")
            return
        logger.info(
            "状态卡任务已启动：阈值告警=%s，每日定时状态图=%s，目标=%s，间隔=%ss，"
            "阈值 CPU %.0f%% / RAM %.0f%% / SWAP %.0f%% / DISK %.0f%%",
            alerts_on,
            daily_on,
            self.targets,
            self.interval,
            self.cpu_threshold,
            self.mem_threshold,
            self.swap_threshold,
            self.disk_threshold,
        )

    def _start_webhook_task(self) -> None:
        if not cfg_bool(self.config, "alerts.komari_webhook.enabled", False):
            return
        self._webhook_task = asyncio.create_task(self.start_webhook())
        self._webhook_task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        self._running = False
        tasks = (
            self._monitor_task,
            self._daily_task,
            self._webhook_task,
        )
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._monitor_task = None
        self._daily_task = None
        self._webhook_task = None
        await self.stop_webhook()

    # ------------------------------------------------------------------
    # targets / sending
    # ------------------------------------------------------------------

    async def _send(self, text: str, source: str = "alert") -> DeliveryResult:
        if not self.targets:
            logger.warning("告警目标为空，跳过发送")
            return DeliveryResult(ok=False)
        value = await self.plugin.send_text(self.targets, text, source=source)
        result = (
            value
            if isinstance(value, DeliveryResult)
            else DeliveryResult(ok=bool(value))
        )
        if not result.ok:
            logger.warning("告警发送失败：%s", text.splitlines()[0] if text else "")
        return result

    @staticmethod
    def _delivered(result: Any) -> bool:
        """A message that may have arrived counts as delivered.

        Retrying a timeout or a lost acknowledgement would show the alert
        twice in the chat, so only a provably unsent message is retried.
        Plain booleans keep test doubles and third-party callers working.
        """

        if isinstance(result, DeliveryResult):
            return result.ok or bool(result.uncertain_targets)
        return bool(result)

    def _record_event(
        self,
        kind: str,
        name: str,
        detail: str,
        status: str,
        target: str = "",
    ) -> None:
        if self.report_store is None:
            return
        try:
            now = time.time()
            day = now_in_timezone(self.config).strftime("%Y-%m-%d")
            self.report_store.add_event(
                int(now), day, kind, name, detail, status, target
            )
            if status == "recovery":
                # Close the open alert of the same rule so the report can
                # show how long the interruption lasted, even across a
                # plugin restart.
                self.report_store.resolve_event(int(now), name, int(now), kind=kind)
        except Exception:
            logger.exception("记录告警事件失败")

    async def send_test(self, target_umo: str | None = None) -> bool:
        targets = [target_umo] if target_umo else self.targets
        if not targets:
            return False
        text = (
            "🔔 测试通知\n"
            "──────────────\n"
            f"主机: {self._hostname()}\n"
            f"时间: {_now_text(self.config)}\n"
            "如果你收到这条消息，说明主动推送链路正常。"
        )
        result = await self.plugin.send_text(targets, text, source="test")
        return self._delivered(result)

    # ------------------------------------------------------------------
    # monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        # Sampler owns psutil.cpu_percent(); give it a moment to seed.
        await asyncio.sleep(1)
        while self._running:
            try:
                await self._sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状态卡告警采样失败")
            await asyncio.sleep(self.interval)

    async def _sample_once(self) -> None:
        now = time.time()
        cpu = cached_cpu_percent()
        mem = memory_stat()
        swap = swap_stat()
        await self._evaluate(
            "cpu",
            "CPU 使用率",
            cpu,
            self.cpu_threshold,
            "%",
            f"{cpu:.1f}%",
        )
        await self._evaluate(
            "ram",
            "内存使用率",
            mem.percent,
            self.mem_threshold,
            "%",
            f"{mem.percent:.1f}% ({format_bytes(mem.used)} / {format_bytes(mem.total)})",
        )
        await self._evaluate(
            "swap",
            "SWAP 使用率",
            swap.percent,
            self.swap_threshold,
            "%",
            f"{swap.percent:.1f}% ({format_bytes(swap.used)} / {format_bytes(swap.total)})",
        )

        disk_path = "/"
        try:
            disk_paths = cfg_get(self.config, "display.disk_paths", None)
            if isinstance(disk_paths, list) and disk_paths:
                disk_path = str(disk_paths[0])
        except Exception:
            pass
        try:
            disk = psutil.disk_usage(disk_path)
            await self._evaluate(
                "disk",
                f"磁盘使用率 ({disk_path})",
                disk.percent,
                self.disk_threshold,
                "%",
                f"{disk.percent:.1f}% ({format_bytes(disk.used)} / {format_bytes(disk.total)})",
            )
        except Exception:
            pass

        if hasattr(os, "getloadavg"):
            try:
                load = os.getloadavg()[0]
                cores = psutil.cpu_count() or 1
                per_core = load / max(1, cores)
                await self._evaluate(
                    "load",
                    "系统负载",
                    per_core,
                    self.load_threshold,
                    " /核",
                    f"{load:.2f}（{cores} 核，{per_core:.2f}/核）",
                )
            except Exception:
                pass

        if self.offline_notify:
            await self._check_offline()
        self.state.set("alerts_last_sample", now)
        self.state.save()

    async def _evaluate(
        self,
        key: str,
        name: str,
        value: float,
        threshold: float,
        unit: str,
        value_text: str,
    ) -> None:
        alerts = self.state.setdefault("alerts", {})
        rules = alerts.setdefault("rules", {})
        st = rules.setdefault(key, {})
        now = time.time()
        count = int(st.get("count") or 0)
        alerting = bool(st.get("alerting"))
        last_sent = float(st.get("last_sent") or 0)

        if threshold <= 0:
            # A threshold of 0 (slider misjudged as "rule off") would make
            # "value >= threshold" true for every sample and stick the rule
            # in alerting forever; treat it as disabled instead.
            st["count"] = 0
            st["alerting"] = False
            return

        if value >= threshold:
            # Keep any unsent recovery notice: dropping it here would silently
            # lose the "recovered" message when the value crosses the threshold
            # again before the retry succeeds.
            count += 1
            st["count"] = count
            if not alerting and count >= self.sustain:
                next_attempt = float(st.get("next_attempt") or 0)
                if now >= next_attempt and (
                    not last_sent or now - last_sent >= self.cooldown
                ):
                    text = (
                        f"⚠️ 系统告警 · {name}\n"
                        "──────────────\n"
                        f"当前: {value_text}（阈值 {threshold:.1f}{unit}）\n"
                        f"主机: {self._hostname()}\n"
                        f"时间: {_now_text(self.config)}\n"
                        f"已连续 {self.sustain} 个采样周期超过阈值。"
                    )
                    result = await self._send(text)
                    delivered = self._delivered(result)
                    st["last_attempt"] = now
                    st["last_error"] = "" if delivered else "send_failed"
                    if delivered:
                        st["alerting"] = True
                        st["alert_since"] = now
                        st["count"] = 0
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        self._record_event(
                            "resource",
                            name,
                            f"{value_text}（阈值 {threshold:.1f}{unit}）",
                            "alert",
                        )
                    else:
                        retry_count = int(st.get("retry_count") or 0) + 1
                        st["retry_count"] = retry_count
                        if retry_count >= 3:
                            # Stop retrying: an unreachable target used to be
                            # retried every ~10 minutes forever, writing one
                            # "failed" event per attempt. Mark the rule as
                            # alerting so only the recovery notice tries again.
                            logger.warning(
                                "告警 %s 连续发送失败 %d 次，停止重试（恢复时仍会通知）",
                                name,
                                retry_count,
                            )
                            st["alerting"] = True
                            st["alert_since"] = now
                            st["count"] = 0
                            st["retry_count"] = 0
                            st["next_attempt"] = 0
                        else:
                            st["next_attempt"] = now + retry_delay(retry_count)
                            st["count"] = self.sustain
                            if retry_count == 1:
                                # One failed event per interruption, not one
                                # per retry.
                                self._record_event(
                                    "resource",
                                    name,
                                    f"{value_text}（阈值 {threshold:.1f}{unit}）",
                                    "failed",
                                )
        else:
            st["count"] = 0
            # A new episode starts with a clean retry budget. Leaving the
            # counter behind meant a later failure could give up on its first
            # attempt and never deliver any alert at all.
            st["retry_count"] = 0
            st["next_attempt"] = 0
            pending_text = st.get("pending_recovery")
            if pending_text:
                next_recovery = float(st.get("recovery_next_attempt") or 0)
                if now >= next_recovery:
                    result = await self._send(str(pending_text))
                    if self._delivered(result):
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                    else:
                        retry_count = int(st.get("recovery_retry_count") or 0) + 1
                        st["recovery_retry_count"] = retry_count
                        st["recovery_next_attempt"] = now + retry_delay(retry_count)
            if alerting:
                st["alerting"] = False
                st["retry_count"] = 0
                st["next_attempt"] = 0
                st["last_error"] = ""
                duration = now - float(st.get("alert_since") or now)
                st["alert_since"] = 0
                # The event log is the source of truth for interruption
                # durations and the daily report's unresolved list, so it must
                # not depend on whether the recovery notice is enabled.
                self._record_event(
                    "resource",
                    name,
                    f"持续 {format_duration(duration)}",
                    "recovery",
                )
                if self.recover_notify:
                    text = (
                        f"✅ 已恢复 · {name}\n"
                        "──────────────\n"
                        f"当前: {value_text}\n"
                        f"告警持续: {format_duration(duration)}\n"
                        f"时间: {_now_text(self.config)}"
                    )
                    result = await self._send(text)
                    if not self._delivered(result):
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + retry_delay(1)

    async def _check_offline(self) -> None:
        # Without a self_id filter a multi-bot instance could probe another
        # bot's connection and report the wrong one as offline.
        self_id = str(cfg_get(self.config, "alerts.self_id", "") or "").strip()
        client = _get_aiocqhttp_client(
            self.plugin.context, self.platform_id, self_id or None
        )
        if client is None:
            return

        online: bool | None = None
        try:
            status = _normalize_object(
                await _call_action(client, "get_status", timeout=5)
            )
            if "online" in status:
                online = bool(status.get("online"))
            else:
                online = True
        except Exception as exc:
            # Distinguish "nothing is connected" from "we cannot tell".
            # aiocqhttp raises ApiNotAvailable when no reverse-WS client is
            # registered: that is a confirmed offline bot, not an unknown
            # state. A timeout or a closed socket stays unknown so a
            # restarting NapCat does not raise a false alarm.
            name = exc.__class__.__name__
            if name in ("ApiNotAvailable", "InvalidURL"):
                online = False
            elif name == "ActionFailed":
                online = True
            else:
                online = None
            logger.debug("OneBot get_status 调用失败（%s）: %s", name, exc)

        alerts = self.state.setdefault("alerts", {})
        st = alerts.setdefault("offline", {})
        now = time.time()
        count = int(st.get("count") or 0)
        unreachable = int(st.get("unreachable") or 0)

        if online:
            st["count"] = 0
            st["unreachable"] = 0
            st["unreachable_warned"] = False
            pending_text = st.get("pending_recovery")
            if pending_text:
                next_recovery = float(st.get("recovery_next_attempt") or 0)
                if now >= next_recovery:
                    result = await self._send(str(pending_text))
                    if self._delivered(result):
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                        logger.info("Bot 连接恢复通知补发成功")
                    else:
                        retry_count = int(st.get("recovery_retry_count") or 0) + 1
                        st["recovery_retry_count"] = retry_count
                        st["recovery_next_attempt"] = now + retry_delay(retry_count)
                        logger.warning("Bot 连接恢复通知补发失败，等待下次重试")
            if st.get("alerting") or st.get("pending_alert_text"):
                duration = now - float(st.get("since") or now)
                queued = st.pop("pending_alert_text", None)
                st["alerting"] = False
                st["since"] = 0
                parts: list[str] = []
                if queued:
                    parts.append(str(queued))
                if self.recover_notify:
                    text = (
                        "✅ Bot 连接已恢复\n"
                        "──────────────\n"
                        f"离线时长: {format_duration(duration)}\n"
                        f"时间: {_now_text(self.config)}"
                    )
                    if st.get("pending_alert") and not queued:
                        text += "\n（离线期间有一条告警未能送达，现已恢复）"
                    parts.append(text)
                st["pending_alert"] = False
                # The event log does not depend on the notification pref.
                self._record_event(
                    "offline",
                    "Bot 连接",
                    f"离线时长 {format_duration(duration)}",
                    "recovery",
                )
                if parts:
                    text = "\n\n".join(parts)
                    result = await self._send(text)
                    if self._delivered(result):
                        logger.info(
                            "Bot 连接恢复通知已发送%s",
                            "（含离线补发）" if queued else "",
                        )
                    else:
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + retry_delay(1)
                        logger.warning("Bot 连接恢复通知发送失败，已排队重试")
            return

        # Keep pending_recovery for the next online transition instead of
        # dropping a recovery notice that has not been delivered yet.
        if online is None:
            # The status call itself failed, so the state of the connection is
            # unknown. Count it separately and stay quiet: a restarting
            # NapCat container must not be reported as a confirmed offline
            # bot, which would raise a false alarm.
            st["unreachable"] = unreachable + 1
            st["count"] = 0
            if unreachable + 1 >= self.sustain * 3 and not st.get("unreachable_warned"):
                st["unreachable_warned"] = True
                logger.warning(
                    "OneBot 状态查询连续 %d 次失败，暂时无法确认 Bot 在线状态",
                    unreachable + 1,
                )
            return

        st["unreachable"] = 0
        st["unreachable_warned"] = False
        count += 1
        st["count"] = count
        if not st.get("alerting") and count >= self.sustain:
            st["alerting"] = True
            st["since"] = now
            st["count"] = 0
            text = (
                "🔴 Bot 连接离线\n"
                "──────────────\n"
                f"主机: {self._hostname()}\n"
                f"时间: {_now_text(self.config)}\n"
                "离线告警会先排队，连接恢复后补发。"
            )
            result = await self._send(text)
            delivered = self._delivered(result)
            st["pending_alert"] = not delivered
            if delivered:
                st.pop("pending_alert_text", None)
                logger.info("Bot 连接离线告警已发送")
                self._record_event("offline", "Bot 连接", "连接离线", "alert")
            else:
                st["pending_alert_text"] = text
                logger.warning("Bot 连接离线告警发送失败，已排队，恢复后补发")
                self._record_event(
                    "offline", "Bot 连接", "连接离线（推送失败）", "failed"
                )

    def _hostname(self) -> str:
        # The push text must obey the same hostname rule as the card, the
        # report and the dashboard; all four read the same rule definition.
        try:
            if "hostname" in read_rules(self.config, "display.redact"):
                return HIDDEN
        except Exception:
            pass
        try:
            return os.uname().nodename
        except Exception:
            return os.environ.get("COMPUTERNAME", "unknown")

    # ------------------------------------------------------------------
    # daily status image schedule
    # ------------------------------------------------------------------

    async def _daily_loop(self) -> None:
        last_logged = None
        warned: set[str] = set()
        while self._running:
            try:
                if not cfg_bool(self.config, "alerts.daily_summary.enabled", False):
                    await asyncio.sleep(60)
                    continue
                tz_name = str(
                    cfg_get(self.config, "display.timezone", "Asia/Shanghai")
                    or "Asia/Shanghai"
                )
                try:
                    tz = ZoneInfo(tz_name)
                    warned.discard("tz")
                except Exception:
                    if "tz" not in warned:
                        logger.warning(
                            "每日定时状态图时区非法：%s，回退到系统时区", tz_name
                        )
                        warned.add("tz")
                    tz = datetime.now().astimezone().tzinfo
                time_text = str(
                    cfg_get(self.config, "alerts.daily_summary.time", "09:00")
                    or "09:00"
                )
                try:
                    hour, minute = [int(x) for x in time_text.split(":", 1)]
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        raise ValueError("out of range")
                    warned.discard("time")
                except Exception:
                    if "time" not in warned:
                        logger.warning(
                            "每日定时状态图时间格式非法：%s（应为 HH:MM）", time_text
                        )
                        warned.add("time")
                    await asyncio.sleep(60)
                    continue
                now = datetime.now(tz)
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    today_text = now.strftime("%Y-%m-%d")
                    if self._daily_catch_up_allowed(today_text):
                        # The window passed while the plugin was down or
                        # reloading; send now instead of silently skipping
                        # the whole day.
                        target = now
                    else:
                        target += timedelta(days=1)
                if last_logged != target:
                    self._next_daily_text = target.strftime("%Y-%m-%d %H:%M")
                    logger.info(
                        "每日定时状态图将在 %s 发送（%.0f 秒后）",
                        self._next_daily_text,
                        max(1.0, (target - now).total_seconds()),
                    )
                    last_logged = target
                wait = max(1.0, (target - now).total_seconds())
                if wait > 60:
                    # Re-read configuration every minute so time/timezone
                    # changes take effect without a plugin reload.
                    await asyncio.sleep(60)
                    continue
                await asyncio.sleep(wait)
                if not cfg_bool(self.config, "alerts.daily_summary.enabled", False):
                    continue
                current_time_text = str(
                    cfg_get(self.config, "alerts.daily_summary.time", "09:00")
                    or "09:00"
                )
                current_tz_name = str(
                    cfg_get(self.config, "display.timezone", "Asia/Shanghai")
                    or "Asia/Shanghai"
                )
                if current_time_text != time_text or current_tz_name != tz_name:
                    # 最后一分钟内改了时间/时区，重新计算，不按旧目标发送。
                    continue
                if not await self._send_daily_summary():
                    for attempt in range(3):
                        await asyncio.sleep(600)
                        if not self._running:
                            break
                        if await self._send_daily_summary():
                            break
                        logger.warning(
                            "每日定时状态图重试失败（第 %d 次）",
                            attempt + 1,
                        )
                self._mark_daily_attempt(
                    now_in_timezone(self.config).strftime("%Y-%m-%d")
                )
                last_logged = None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("每日定时状态图任务失败")
                await asyncio.sleep(60)

    async def _send_daily_summary(self) -> bool:
        today = now_in_timezone(self.config).strftime("%Y-%m-%d")
        if self._daily_last_sent_day == today:
            logger.info("每日定时状态图 %s 已发送，跳过重复发送", today)
            return True
        if not self.targets:
            logger.warning("每日定时状态图已启用，但告警目标为空，跳过发送")
            return True
        with_image = cfg_bool(self.config, "alerts.daily_summary.with_image", True)
        if with_image:
            try:
                image_url = await self.plugin.build_status_image()
            except Exception:
                logger.exception("每日定时状态图渲染失败")
                image_url = None
            if image_url:
                result = await self.plugin.send_image(
                    self.targets, image_url, source="daily_summary"
                )
                if self._delivered(result):
                    self._mark_daily_sent(today)
                    logger.info("每日定时状态图已发送（图片）：%s", self.targets)
                    return True
        summary = await self.plugin.build_summary_text()
        result = await self._send(
            "📅 每日定时状态图\n──────────────\n" + summary,
            source="daily_summary",
        )
        if self._delivered(result):
            self._mark_daily_sent(today)
            logger.info("每日定时状态图已发送（文字）：%s", self.targets)
            return True
        logger.warning("每日定时状态图发送失败：%s", self.targets)
        return False

    # ------------------------------------------------------------------
    # Komari webhook
    # ------------------------------------------------------------------

    async def start_webhook(self) -> None:
        if not cfg_bool(self.config, "alerts.komari_webhook.enabled", False):
            return
        try:
            from aiohttp import web
        except Exception as exc:
            logger.error("无法导入 aiohttp，Komari Webhook 未启动: %s", exc)
            return

        host = str(
            cfg_get(self.config, "alerts.komari_webhook.host", "127.0.0.1")
            or "127.0.0.1"
        )
        port = max(
            1,
            min(65535, cfg_int(self.config, "alerts.komari_webhook.port", 9968)),
        )
        path = str(
            cfg_get(self.config, "alerts.komari_webhook.path", "/webhook") or "/webhook"
        )
        if not path.startswith("/"):
            path = "/" + path
        token = str(cfg_get(self.config, "alerts.komari_webhook.token", "") or "")
        require_token = cfg_bool(
            self.config, "alerts.komari_webhook.require_token", True
        )
        if require_token and not token:
            logger.error(
                "Komari Webhook 已开启“强制要求 Token”，但未配置 Token，已拒绝启动"
            )
            return

        if host == "0.0.0.0" and not token:
            # Exposing the webhook on every interface without a token would
            # let anyone post to it and push arbitrary messages into the
            # operator's chat, so the listener refuses to start.
            logger.error(
                "Komari Webhook 绑定 0.0.0.0 但未配置 Token，已拒绝启动；"
                "请改用 127.0.0.1，或填写 Token 后再启用"
            )
            return

        app = web.Application()
        max_body_bytes = 64 * 1024

        async def handle(request: web.Request) -> web.Response:
            if token:
                auth = request.headers.get("Authorization", "")
                if not hmac.compare_digest(
                    auth.encode("utf-8"), f"Bearer {token}".encode()
                ):
                    return web.json_response(
                        {"status": "error", "message": "unauthorized"},
                        status=403,
                    )
            length = request.content_length
            if length is not None and length > max_body_bytes:
                return web.json_response(
                    {"status": "error", "message": "payload too large"},
                    status=413,
                )
            raw = await request.content.read(max_body_bytes + 1)
            if len(raw) > max_body_bytes:
                return web.json_response(
                    {"status": "error", "message": "payload too large"},
                    status=413,
                )
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {"message": raw.decode("utf-8", errors="replace")[:4000]}
            text = self._format_komari(data)
            result = await self._send(text, source="webhook")
            if not self._delivered(result):
                return web.json_response(
                    {"status": "error", "message": "delivery failed"},
                    status=503,
                )
            return web.json_response({"status": "ok"})

        app.router.add_post(path, handle)
        runner = web.AppRunner(app)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
        except Exception as exc:
            logger.error("Komari Webhook 启动失败 %s:%s: %s", host, port, exc)
            try:
                await runner.cleanup()
            except Exception:
                pass
            return
        self._webhook_runner = runner
        self._webhook_site = site
        logger.info("Komari Webhook 已监听 http://%s:%s%s", host, port, path)

    async def stop_webhook(self) -> None:
        runner = self._webhook_runner
        self._webhook_runner = None
        self._webhook_site = None
        if runner is not None:
            try:
                await runner.cleanup()
                logger.info("Komari Webhook 已停止")
            except Exception:
                pass

    @staticmethod
    def _format_komari(data: Any) -> str:
        if not isinstance(data, dict):
            return truncate_text(
                "🔔 Komari 监控通知\n──────────────\n"
                + json.dumps(data, ensure_ascii=False, indent=2)[:4000],
                2000,
            )

        title = str(data.get("title") or data.get("event") or "").strip()
        message = str(data.get("message") or data.get("text") or "").strip()
        title_lower = title.lower()
        icon_map = {
            "offline": "🔴",
            "online": "🟢",
            "alert": "⚠️",
            "expire": "⏰",
            "renew": "♻️",
            "traffic": "📊",
        }
        icon = icon_map.get(title_lower, "🔔")

        lines = [f"{icon} Komari 监控通知", "──────────────"]
        if title:
            lines.append(f"事件: {title}")
        if message:
            cleaned: list[str] = []
            for line in message.splitlines():
                text = line.strip()
                if not text:
                    continue
                if text.startswith(("节点：", "节点:")):
                    continue
                if text.startswith(("消息：", "消息:")):
                    text = text[3:].strip()
                cleaned.append(text)
            lines.extend(cleaned or [message])
        else:
            # Dumping the whole payload would publish whatever Komari sends
            # (node IPs, UUIDs) into the group chat; keep it to known fields.
            known = [
                f"{label}: {data[key]}"
                for label, key in (
                    ("事件", "event"),
                    ("级别", "level"),
                    ("节点", "node_name"),
                    ("时间", "time"),
                )
                if data.get(key)
            ]
            lines.extend(known or ["（Komari 未附带可读内容，详情见 Komari 面板）"])
        return truncate_text("\n".join(lines), 2000)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        alerts = self.state.get("alerts", {}) or {}
        return {
            "enabled": cfg_bool(self.config, "alerts.enabled", False),
            "running": self._running,
            "targets": list(self.targets),
            "interval": self.interval,
            "sustain": self.sustain,
            "cooldown": self.cooldown,
            "thresholds": {
                "cpu": self.cpu_threshold,
                "ram": self.mem_threshold,
                "swap": self.swap_threshold,
                "disk": self.disk_threshold,
                "load_per_core": self.load_threshold,
            },
            "rules": alerts.get("rules", {}),
            "offline": alerts.get("offline", {}),
            "daily_summary": bool(
                cfg_bool(self.config, "alerts.daily_summary.enabled", False)
            ),
            "daily_summary_running": bool(
                self._daily_task is not None and not self._daily_task.done()
            ),
            "daily_summary_next": self._next_daily_text,
            "daily_summary_last_sent_day": self._daily_last_sent_day,
            "komari_webhook": bool(
                cfg_bool(self.config, "alerts.komari_webhook.enabled", False)
            ),
        }
