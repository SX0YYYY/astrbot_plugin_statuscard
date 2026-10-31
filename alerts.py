from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psutil
from astrbot.api import logger

from .aggregator import AlertAggregator
from .collectors import (
    _call_action,
    _get_aiocqhttp_client,
    _normalize_list,
    _normalize_object,
    cached_cpu_percent,
    cfg_bool,
    cfg_float,
    cfg_get,
    cfg_int,
    disk_usage_bounded,
    format_bytes,
    format_duration,
    memory_stat,
    now_in_timezone,
    resolve_targets,
    swap_stat,
)
from .delivery import DeliveryResult
from .provider_quality import recent_streak as provider_recent_streak
from .redactor import HIDDEN, read_rules
from .state import StateStore
from .utils import (
    clamp_config,
    normalize_targets,
    retry_delay,
    truncate_text,
)
from .utils import (
    safe_float as _safe_float,
)
from .utils import (
    safe_int as _safe_int,
)


def _now_text(config: Any = None) -> str:
    if config is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return now_in_timezone(config).strftime("%Y-%m-%d %H:%M:%S")


BROADCAST_TEMPLATE_DEFAULT = (
    "✅ Bot 已恢复上线\n"
    "掉线时间: {offline_time}\n"
    "恢复时间: {recover_time}\n"
    "离线时长: {duration}"
)

# A recovery notice that cannot be delivered is retried a few times and then
# dropped. Without a ceiling the resource and offline paths retried every ten
# minutes until the process restarted, each attempt doing a full delivery.
MAX_RECOVERY_RETRIES = 5


# Disconnect alerts use their own sample threshold: ``sustain_samples`` is
# documented as affecting only the resource rules, and raising it to dampen
# memory jitter used to delay disconnect alerts by the same factor.
OFFLINE_ALERT_SAMPLES = 2
# A raw provider_stats enum ("completed") means nothing to a group member;
# the alert and recovery texts use these labels instead.
_PROVIDER_STATUS_TEXT = {
    "completed": "调用成功",
    "error": "调用失败",
    "aborted": "已中止",
}


def _webhook_bind_allowed(host: str, require_token: bool, token: str) -> bool:
    """Whether the Komari webhook may bind ``host`` with these auth settings.

    A wildcard bind without an enforced token turns the listener into an open
    relay into the operator's chat, and a token that is configured but not
    required is not checked at all, so both cases refuse to start.
    """

    normalized = host.strip().strip("[]").lower()
    if normalized not in {"0.0.0.0", "::", "0:0:0:0:0:0:0:0", ""}:
        return True
    return bool(require_token and token)


def _fmt_ts(ts: float, config: Any = None) -> str:
    """Format a Unix timestamp in the display timezone."""

    try:
        return datetime.fromtimestamp(
            float(ts), now_in_timezone(config).tzinfo
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "--"


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
        aggregator: AlertAggregator | None = None,
    ):
        self.plugin = plugin
        self.state = state
        self.config = config
        self.report_store = report_store
        self._aggregator = aggregator
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
        self.cooldown = clamp_config(
            cfg_int(config, "alerts.cooldown_seconds", 1800),
            0,
            86400,
            "alerts.cooldown_seconds",
        )
        self.recover_notify = cfg_bool(config, "alerts.recover_notify", True)
        self.offline_notify = cfg_bool(config, "alerts.offline_notify", True)
        self.provider_watch_enabled = cfg_bool(
            config, "alerts.provider_watch.enabled", False
        )
        # A hand-edited threshold of 0 means "disabled" (same reading as the
        # resource rules); clamping it to the slider minimum would instead
        # turn it into the most sensitive value.
        raw_threshold = cfg_int(config, "alerts.provider_watch.threshold", 3)
        self.provider_watch_threshold = (
            clamp_config(raw_threshold, 1, 10, "alerts.provider_watch.threshold")
            if raw_threshold > 0
            else 0
        )
        self.provider_watch_cooldown = clamp_config(
            cfg_int(config, "alerts.provider_watch.cooldown_seconds", 1800),
            300,
            86400,
            "alerts.provider_watch.cooldown_seconds",
        )
        self._provider_last_check = 0.0
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
        # Body hash of the last successfully delivered webhook request: Komari
        # retries the same payload after a timeout, which used to post the
        # event to the group twice.
        self._webhook_last: tuple[str, float] = ("", 0.0)
        self._broadcast_task: asyncio.Task | None = None
        self._client_missing_warned = False
        self._primed = False
        self._disk_warned: set[str] = set()
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

    def _mark_stale_rules(self) -> None:
        """Mark rules that were alerting while the engine was not sampling.

        A restart or ``alerts.enabled=false`` leaves the alerting flag and its
        start time in state.json. Without this mark the next recovery would
        report the entire disabled window as one interruption.
        """

        with self.state.section("alerts") as alerts:
            rules = alerts.get("rules") or {}
            offline = alerts.get("offline") or {}
            if not isinstance(rules, dict):
                return
            now = time.time()
            tracked: list[tuple[str, Any]] = [
                (str(key), st) for key, st in rules.items()
            ]
            if isinstance(offline, dict):
                tracked.append(("offline", offline))
            for key, st in tracked:
                if not isinstance(st, dict) or not st.get("alerting"):
                    continue
                last_seen = float(st.get("last_seen") or st.get("alert_since") or 0)
                if not last_seen and key == "offline":
                    last_seen = _safe_float(st.get("since"))
                if last_seen and now - last_seen > 86400:
                    st["stale"] = True
                    logger.warning(
                        "告警规则 %s 已超过 24 小时未采样（可能关闭了告警），"
                        "恢复时只记事件、不发恢复消息",
                        key,
                    )

    def start(self) -> None:
        alerts_on = cfg_bool(self.config, "alerts.enabled", False)
        daily_on = cfg_bool(self.config, "alerts.daily_summary.enabled", False)
        self._running = alerts_on or daily_on
        if alerts_on:
            # A hand-edited state.json can hold a list/scalar here; replacing
            # it under the store's lock keeps start-up from crashing.
            with self.state.section("alerts"):
                pass
            self._mark_stale_rules()
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
            self._broadcast_task,
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
        self._broadcast_task = None
        await self.stop_webhook()

    # ------------------------------------------------------------------
    # targets / sending
    # ------------------------------------------------------------------

    def _targets_for(self, kind: str) -> list[str]:
        """Push targets for one kind; None from the center means legacy."""

        picked = resolve_targets(self.config, kind)
        return self.targets if picked is None else picked

    async def _send(
        self, text: str, source: str = "alert", kind: str = "resource"
    ) -> DeliveryResult:
        targets = self._targets_for(kind)
        if not targets:
            logger.warning("告警目标为空，跳过发送")
            return DeliveryResult(ok=False)
        value = await self.plugin.send_text(targets, text, source=source)
        result = (
            value
            if isinstance(value, DeliveryResult)
            else DeliveryResult(ok=bool(value))
        )
        if not result.ok:
            headline = text.splitlines()[0] if text else ""
            if result.uncertain_targets:
                logger.warning(
                    "告警发送未确认（可能已送达，按不重发处理）：%s", headline
                )
            else:
                logger.warning("告警发送失败：%s", headline)
        elif result.partial:
            # Some targets received the message, others did not. Retrying
            # would post it twice to the successful ones, so surface the
            # partial result instead of recording a clean delivery.
            logger.warning(
                "告警部分送达：成功 %d 个、失败 %d 个目标（不重发）：%s",
                len(result.ok_targets),
                len(result.failed_targets),
                text.splitlines()[0] if text else "",
            )
        return result

    async def _push_alert(self, name: str, reason: str, text: str) -> Any | None:
        """Send the first alert of an episode, or buffer it in the window."""

        if self._aggregator is None:
            return await self._send(text)
        return await self._aggregator.push(
            label=name,
            reason=reason,
            text=text,
            send=lambda text: self._send(text, kind="resource"),
            kind="resource",
            on_abandon=self._on_summary_abandoned,
        )

    def _on_summary_abandoned(self, items: list[tuple[str, str]]) -> None:
        """Withdraw "alerted" for rules whose summary never reached the chat.

        A buffered alert is only "announced" once the summary is posted. If
        the summary is abandoned (retries exhausted, reload), keeping
        ``alerted=True`` would make the later recovery notice claim an alert
        the user never saw. ``last_sent`` is cleared for the same reason: it
        was recorded when the alert was buffered ("the summary did reach the
        chat"), and keeping it would silence a new episode inside the
        cooldown for half an hour after the summary was given up on.
        """

        labels = {label for label, _reason in items if label}
        if not labels:
            return
        changed = False
        with self.state.section("alerts") as alerts:
            rules = alerts.get("rules")
            if not isinstance(rules, dict):
                return
            for st in rules.values():
                if not isinstance(st, dict) or not st.get("alerting"):
                    continue
                label = str(st.get("label") or "")
                if label and label in labels:
                    st["alerted"] = False
                    st.pop("last_sent", None)
                    # The window died with the abandon; leaving this set
                    # would delay a future recovery for no reason.
                    st.pop("awaiting_summary", None)
                    changed = True
        # Abandon can be the last write before a reload (stop() runs after
        # the aggregator is cancelled); persist it or the new instance reads
        # the stale "alerted=True" from disk and announces a recovery for an
        # alert that never arrived.
        if changed:
            self.state.save()

    @staticmethod
    def _delivered(result: Any) -> bool:
        """Treat "may have arrived" as done for retry purposes.

        Retrying a timeout or a lost acknowledgement would show the alert
        twice in the chat, so only a provably unsent message is retried. This
        is not a delivery confirmation: ``_send`` logs the unconfirmed case
        separately. Plain booleans keep test doubles and third-party callers
        working.
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
                # plugin restart. ``target`` keeps two rules that share a name
                # (same label, different disk/mount) from closing each other.
                self.report_store.resolve_event(
                    int(now),
                    name,
                    int(now),
                    kind=kind,
                    target=target,
                    close_all=True,
                )
        except Exception:
            logger.exception("记录告警事件失败")

    async def send_test(self, target_umo: str | None = None) -> bool:
        # Exercise both the current chat and the targets real alerts would
        # use. Sending only to the invoking chat reported success while every
        # real alert went to a mistyped group id -- the most common reason
        # "alerts are not arriving".
        targets: list[str] = []
        candidates: list[str] = [str(target_umo or "").strip()]
        # Every notification kind may route elsewhere through the notification
        # center; testing only "resource" missed a broken daily/service target.
        for kind in (
            "resource",
            "service",
            "offline",
            "provider",
            "daily",
            "weekly",
            "daily_image",
        ):
            candidates.extend(
                str(item or "").strip() for item in self._targets_for(kind) or []
            )
        for text_candidate in candidates:
            if text_candidate and text_candidate not in targets:
                targets.append(text_candidate)
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
        if isinstance(result, DeliveryResult):
            # A partial or unconfirmed result must not read as "sent": the
            # whole point of this command is to diagnose a broken target.
            return bool(result.ok_targets) and not (
                result.failed_targets or result.uncertain_targets
            )
        return bool(result)

    # ------------------------------------------------------------------
    # monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        # Sampler owns psutil.cpu_percent(); give it a moment to seed.
        await asyncio.sleep(1)
        while self._running:
            try:
                # A hand-edited or half-written state.json can turn ``alerts``
                # into a list/scalar. section() replaces that under the lock,
                # so one bad write no longer makes every sample raise until a
                # restart (v2.20.1).
                with self.state.section("alerts"):
                    pass
                await self._sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状态卡告警采样失败")
            await asyncio.sleep(self.interval)

    async def _sample_once(self) -> None:
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

        disk_paths: list[str] = []
        try:
            configured = cfg_get(self.config, "display.disk_paths", None)
            if isinstance(configured, list):
                # Duplicate entries would evaluate the same mount twice per
                # sample, so a sustain_samples=3 rule fired after two cycles.
                seen: list[str] = []
                for entry in configured:
                    path = str(entry).strip()
                    if path and path not in seen:
                        seen.append(path)
                disk_paths = seen
        except Exception:
            disk_paths = []
        if not disk_paths:
            disk_paths = ["/"]

        # Every configured mount gets its own rule. Watching only the first
        # one meant a second disk could fill up silently while the card and
        # the report showed it.
        if len(disk_paths) > 1:
            # Adding a second mount used to rename the single-mount key from
            # "disk" to "disk:/" -- the old state was pruned with a fake
            # "recovered" event and the still-full disk alerted again. Carry
            # the first mount's latch over instead.
            with self.state.section("alerts") as alerts:
                rules = alerts.get("rules")
                if isinstance(rules, dict):
                    legacy_key = f"disk:{disk_paths[0]}"
                    legacy = rules.get(legacy_key)
                    if isinstance(legacy, dict) and "disk" not in rules:
                        rules["disk"] = dict(legacy)
                        # Drop the old key in the same write -- but only when
                        # we actually migrated. When "disk" already exists
                        # (the user once had this mount in first position and
                        # reordered later), popping it used to discard a
                        # possibly still-alerting episode without any
                        # recovery event; pruning handles that leftover
                        # instead.
                        rules.pop(legacy_key, None)
        valid_keys = {"cpu", "ram", "swap", "load"}
        for index, disk_path in enumerate(disk_paths):
            # The first mount keeps the historical key whether or not extra
            # mounts exist, so its cooldown and latch survive a config change.
            rule_key = "disk" if index == 0 else f"disk:{disk_path}"
            valid_keys.add(rule_key)
            try:
                # One implementation shared with the card, the report and the
                # dashboard: used/total. psutil's percent excludes the ext4
                # root-reserved blocks and would alert on a different scale
                # than the number the user reads on the card.
                # statvfs blocks on a hung network mount; keep it off the
                # event loop (the card path already did).
                disk = (await asyncio.to_thread(disk_usage_bounded, [disk_path]))[0]
                if disk.percent is None or disk.used is None or disk.total is None:
                    raise RuntimeError(disk.exception or "磁盘信息不可用")
                await self._evaluate(
                    rule_key,
                    f"磁盘使用率 ({disk_path})",
                    disk.percent,
                    self.disk_threshold,
                    "%",
                    f"{disk.percent:.1f}%"
                    f" ({format_bytes(disk.used)} / {format_bytes(disk.total)})",
                )
            except Exception as exc:
                if disk_path not in self._disk_warned:
                    self._disk_warned.add(disk_path)
                    logger.warning(
                        "磁盘挂载点 %s 无法读取（%s），磁盘告警已跳过；"
                        "请检查 display.disk_paths",
                        disk_path,
                        exc,
                    )
                # A rule left alerting by a vanished mount used to hang in
                # the daily report's "unresolved" list for 30 days with no
                # recovery event (U-disk unplugged, NFS gone). Close the
                # episode here -- the mount coming back later just starts a
                # fresh evaluation (v2.20.0).
                #
                # Everything that can block (the recovery event's SQLite
                # write and the notice itself) runs *outside* the section:
                # awaiting a send while holding the state lock let a worker
                # thread that was inside save() deadlock the event loop
                # (v2.20.1).
                close_text: str | None = None
                closed = False
                with self.state.section("alerts") as alerts:
                    rules = alerts.get("rules")
                    st = rules.get(rule_key) if isinstance(rules, dict) else None
                    if isinstance(st, dict) and st.get("alerting"):
                        notified = bool(st.pop("alerted", True))
                        st["alerting"] = False
                        st["alert_since"] = 0
                        st["count"] = 0
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        st.pop("awaiting_summary", None)
                        for stale_key in (
                            "pending_recovery",
                            "recovery_attempt",
                            "recovery_next_attempt",
                            "recovery_retry_count",
                        ):
                            st.pop(stale_key, None)
                        closed = True
                        if notified and self.recover_notify:
                            close_text = (
                                "✅ 告警已关闭 · 磁盘挂载点不可读\n"
                                "──────────────\n"
                                f"挂载点: {disk_path}\n"
                                "原因: 挂载点无法读取（已拔出或掉线），"
                                "之前的告警已自动关闭。\n"
                                f"时间: {_now_text(self.config)}"
                            )
                if closed:
                    # The event must be written even when notifications are
                    # disabled: it is what closes the episode for the report.
                    self._record_event(
                        "resource",
                        f"磁盘使用率 ({disk_path})",
                        "挂载点不可读（已拔出或掉线），告警自动关闭",
                        "recovery",
                    )
                if close_text is not None:
                    result = await self._send(
                        close_text, source="resource", kind="resource"
                    )
                    if not self._delivered(result):
                        logger.warning("磁盘挂载点 %s 的告警关闭通知未送达", disk_path)

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

        # Detection, event logging and the optional broadcast are independent
        # of the notification preference: turning notifications off must not
        # erase offline incidents from the daily report.
        await self._check_offline()
        if self.provider_watch_enabled:
            await self._check_provider_quality()
        else:
            # A latch created before the switch was turned off would stay
            # "unresolved" in the daily report forever.
            self._close_provider_watch("LLM 服务监控已关闭")
        self._prune_rules(valid_keys)
        self.state.save()

    def _prune_rules(self, valid_keys: set[str]) -> None:
        """Drop state for rules that no longer exist and close their episodes.

        A removed disk mount or a renamed rule kept ``alerting=True`` in
        state.json forever, so the daily report kept showing an "未恢复" row
        for up to 30 days and re-adding the mount produced a bogus multi-day
        duration. The monitor already pruned its items; the resource rules had
        no equivalent.
        """

        alerts = self.state.get("alerts", {})
        if not isinstance(alerts, dict):
            return
        rules = alerts.get("rules")
        if not isinstance(rules, dict):
            return
        stale = [key for key in rules if key not in valid_keys]
        if not stale:
            return
        labels = {
            "cpu": "CPU 使用率",
            "ram": "内存使用率",
            "swap": "SWAP 使用率",
            "load": "系统负载",
            "disk": "磁盘使用率",
        }
        for key in stale:
            state = rules.pop(key, None)
            if not isinstance(state, dict) or not state.get("alerting"):
                continue
            base, _, suffix = key.partition(":")
            name = labels.get(base, base)
            if suffix:
                name = f"{name} ({suffix})"
            self._record_event("resource", name, "规则已移除，自动关闭", "recovery")
        self.state.save()
        logger.info("资源告警清理了 %d 个已失效规则状态", len(stale))

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
        rules = alerts.get("rules")
        if not isinstance(rules, dict):
            # A hand-edited state.json could hold a list here; the old code
            # raised on every sample, which silently disabled alerting.
            rules = {}
            alerts["rules"] = rules
        st = rules.get(key)
        if not isinstance(st, dict):
            st = {}
            rules[key] = st
        now = time.time()
        st["last_seen"] = now
        # Remember the display label: the aggregator's abandon callback only
        # has labels, and resolving a key back to its label (disk paths) is
        # otherwise guesswork.
        st["label"] = name
        count = _safe_int(st.get("count"))
        alerting = bool(st.get("alerting"))
        last_sent = _safe_float(st.get("last_sent"))
        if st.pop("send_inflight", False) and not alerting:
            # The process was reloaded or crashed while this alert was being
            # sent. The message may already be in the chat, so treat the
            # episode as announced instead of firing the same alert again
            # after ``sustain`` more samples. ``alerted=False`` keeps the
            # recovery notice quiet: if the alert never arrived, an
            # unexplained "已恢复" is worse than the missing row.
            alerting = True
            st["alerting"] = True
            st["alerted"] = False
            st["alert_since"] = _safe_float(st.get("last_attempt")) or now
            st["count"] = 0
            # Log an alert row so the episode has a start in the event log;
            # without it the later recovery had no matching beginning.
            self._record_event(
                "resource",
                name,
                "上次告警发送被中断（重载/重启），已按告警中处理",
                "alert",
            )

        if threshold <= 0:
            # A threshold of 0 (slider misjudged as "rule off") would make
            # "value >= threshold" true for every sample and stick the rule
            # in alerting forever; treat it as disabled instead. Closing the
            # open interruption matters: without a recovery event the alert
            # stayed "unresolved" in the daily report for up to 30 days.
            was_alerting = alerting
            st["count"] = 0
            st["alerting"] = False
            st["alerted"] = False
            st["alert_since"] = 0
            st["retry_count"] = 0
            st["next_attempt"] = 0
            # An episode latched while the summary was pending must not keep
            # that flag: with alerting now False it is an illegal combo and
            # the next recovery would queue behind a window that no longer
            # exists (v2.20.0).
            st.pop("awaiting_summary", None)
            # A queued recovery from the disabled rule must not survive:
            # after re-enabling it would be delivered as a stale notice.
            for stale_key in (
                "pending_recovery",
                "recovery_attempt",
                "recovery_next_attempt",
                "recovery_retry_count",
            ):
                st.pop(stale_key, None)
            if was_alerting:
                self._record_event(
                    "resource",
                    name,
                    "阈值改为 0，规则已停用",
                    "recovery",
                )
            return

        if value >= threshold:
            # Keep any unsent recovery notice: dropping it here would silently
            # lose the "recovered" message when the value crosses the threshold
            # again before the retry succeeds.
            # count only tracks the sustain run-up; once the rule is alerting
            # it serves no purpose and used to grow unbounded for days-long
            # episodes (v2.20.0).
            if not alerting:
                count += 1
                st["count"] = count
            # A fault latched inside the cooldown was recorded but never
            # pushed. If it is still over the threshold when the cooldown
            # expires, the user has never seen it: send the alert now.
            cooldown_retry = (
                alerting
                and not bool(st.get("alerted"))
                and _safe_int(st.get("retry_count")) == 0
                and _safe_float(st.get("next_attempt")) == 0
                and _safe_float(st.get("last_sent")) > 0
                and now - _safe_float(st.get("last_sent")) >= self.cooldown
            )
            if (not alerting and count >= self.sustain) or cooldown_retry:
                if last_sent and now - last_sent < self.cooldown:
                    # A second episode inside the cooldown used to vanish
                    # completely. Latch it and write one event (with the
                    # suppression noted); only the duplicate push is skipped.
                    st["alerting"] = True
                    st["alerted"] = False
                    st["alert_since"] = now
                    st["count"] = 0
                    self._record_event(
                        "resource",
                        name,
                        f"{value_text}（阈值 {threshold:.1f}{unit}，冷却窗口内仅记录）",
                        "alert",
                    )
                    return
                next_attempt = _safe_float(st.get("next_attempt"))
                if now >= next_attempt:
                    # A recovery notice that never got through is folded into
                    # this alert. Retrying it separately after a new episode
                    # produced two "recovered" messages, the first one quoting
                    # the previous episode's numbers (2026-10-29 审计实测).
                    had_pending = bool(st.pop("pending_recovery", None))
                    if had_pending:
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                    text = (
                        f"⚠️ 系统告警 · {name}\n"
                        "──────────────\n"
                        f"当前: {value_text}（阈值 {threshold:.1f}{unit}）\n"
                        f"主机: {self._hostname()}\n"
                        f"时间: {_now_text(self.config)}\n"
                        f"判定条件: 连续 {self.sustain} 个采样周期超过阈值。"
                    )
                    if had_pending:
                        text += "\n（上一次的恢复通知未送达，已并入本条）"
                    # Persist before the await: a reload or crash during a
                    # slow send used to leave the rule "not alerting" on
                    # disk, so the same alert fired again later.
                    st["last_attempt"] = now
                    st["send_inflight"] = True
                    self.state.save()
                    result = await self._push_alert(
                        name, f"{value_text}（阈值 {threshold:.1f}{unit}）", text
                    )
                    buffered = result is None
                    delivered = (not buffered) and self._delivered(result)
                    st["last_attempt"] = now
                    st.pop("send_inflight", None)
                    st["last_error"] = "" if delivered or buffered else "send_failed"
                    if buffered:
                        # Covered by the aggregator summary; the rule is in
                        # alerting and the event log stays complete.
                        st["alerting"] = True
                        st["alerted"] = True
                        if not alerting:
                            # A cooldown-retry keeps the episode's original
                            # start; only a fresh episode sets it.
                            st["alert_since"] = now
                        st["count"] = 0
                        # The summary did reach the chat, so the cooldown
                        # applies to this rule too; without it the next
                        # episode pushed a second alert minutes later.
                        st["last_sent"] = now
                        # The alert itself is still waiting for the summary:
                        # a recovery inside the window must queue behind it.
                        st["awaiting_summary"] = True
                        self._record_event(
                            "resource",
                            name,
                            f"{value_text}（阈值 {threshold:.1f}{unit}）",
                            "alert",
                        )
                    elif delivered:
                        st["alerting"] = True
                        st["alerted"] = True
                        if not alerting:
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
                        retry_count = _safe_int(st.get("retry_count")) + 1
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
                            st["alerted"] = True
                            # Keep the episode's original start: resetting it
                            # here shrank the recovery notice's "告警持续" to
                            # just the retry window. A stored 0 (set when a
                            # previous episode recovered) must not survive:
                            # now - 0 rendered as "20757 天".
                            st["alert_since"] = (
                                _safe_float(st.get("alert_since")) or now
                            )
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
                    # Persist alerting/alerted/last_sent in the same write that
                    # clears send_inflight: saving before those fields were set
                    # left a reload window where the disk said "not alerting",
                    # and the same outage alerted again after restart (v2.20.2).
                    self.state.save()
        else:
            st["count"] = 0
            # A push that failed and then recovered before the retry budget
            # ran out never latched ``alerting``, so the recovery branch below
            # was skipped and its "failed" event stayed unresolved in the
            # daily report for up to 30 days. Close that event here; no
            # message is sent because the alert itself never arrived.
            # Only for episodes that never latched: a rule that exhausted its
            # retries (alerting=True) closes through the recovery branch
            # below, which reads ``last_error`` for the "此前未能送达" note --
            # clearing it here used to swallow that note and add a second
            # recovery row for the same episode.
            if not alerting and (
                _safe_int(st.get("retry_count")) > 0
                or st.get("last_error") == "send_failed"
            ):
                self._record_event(
                    "resource",
                    name,
                    "告警未能送达，本次波动已结束",
                    "recovery",
                )
                st["last_error"] = ""
            # A new episode starts with a clean retry budget. Leaving the
            # counter behind meant a later failure could give up on its first
            # attempt and never deliver any alert at all.
            st["retry_count"] = 0
            st["next_attempt"] = 0
            pending_text = st.get("pending_recovery")
            if pending_text and not self.recover_notify:
                # The operator turned recovery notices off after this one was
                # queued for retry; honouring the switch means dropping the
                # backlog instead of delivering it later anyway.
                st.pop("pending_recovery", None)
                st.pop("recovery_attempt", None)
                st.pop("recovery_next_attempt", None)
                st.pop("recovery_retry_count", None)
                pending_text = None
            if pending_text:
                next_recovery = _safe_float(st.get("recovery_next_attempt"))
                if now >= next_recovery:
                    result = await self._send(str(pending_text))
                    if self._delivered(result):
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                    else:
                        retry_count = _safe_int(st.get("recovery_retry_count")) + 1
                        if retry_count > MAX_RECOVERY_RETRIES:
                            logger.warning(
                                "%s 的恢复通知连续 %d 次未送达，放弃补发",
                                name,
                                retry_count - 1,
                            )
                            st.pop("pending_recovery", None)
                            st.pop("recovery_attempt", None)
                            st.pop("recovery_next_attempt", None)
                            st.pop("recovery_retry_count", None)
                        else:
                            st["recovery_retry_count"] = retry_count
                            st["recovery_next_attempt"] = now + retry_delay(retry_count)
            if alerting:
                stale = bool(st.pop("stale", False))
                # Only announce the recovery for an episode that was actually
                # pushed. A second episode inside the cooldown is logged but
                # never sent, so its recovery notice would arrive without a
                # matching alert and look like a duplicate (2026-10-29 实测).
                notified = bool(st.pop("alerted", True))
                # Read before clearing: an episode whose alerts were abandoned
                # after the retry budget still sends a recovery, and the user
                # deserves to know the alert itself never arrived.
                had_failed = st.get("last_error") == "send_failed"
                st["alerting"] = False
                st["retry_count"] = 0
                st["next_attempt"] = 0
                st["last_error"] = ""
                duration = now - _safe_float(st.get("alert_since"), now)
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
                # Consume the flag even when no notice is sent (recover_notify
                # off / stale / suppressed): leaving it behind made the next
                # unrelated recovery queue behind an already-closed window.
                awaiting = st.pop("awaiting_summary", False)
                if self.recover_notify and not stale and notified:
                    text = (
                        f"✅ 已恢复 · {name}\n"
                        "──────────────\n"
                        f"当前: {value_text}\n"
                        f"主机: {self._hostname()}\n"
                        f"告警持续: {format_duration(duration)}\n"
                        f"时间: {_now_text(self.config)}"
                    )
                    if had_failed:
                        text += "\n（异常告警此前未能送达）"
                    remaining = (
                        self._aggregator.window_remaining("resource")
                        if self._aggregator is not None
                        else 0.0
                    )
                    if awaiting and remaining > 0:
                        # This rule's alert is still inside the aggregation
                        # window. Posting the recovery now would let it beat
                        # the summary to the chat (恢复先于告警到达，看起来像
                        # 两条不相关消息)；queue it instead so the summary is
                        # posted first. On abandon the queued notice still
                        # goes out -- the user then at least learns the
                        # episode happened.
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        # Schedule after the window actually closes: a fixed
                        # 15s raced the flush whenever the sampling interval
                        # was shorter than the window.
                        st["recovery_next_attempt"] = now + max(15.0, remaining + 3.0)
                        logger.info(
                            "告警 %s 在聚合窗口内恢复，恢复通知延后 %.0f 秒发送",
                            name,
                            max(15.0, remaining + 3.0),
                        )
                        return
                    result = await self._send(text)
                    if not self._delivered(result):
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + retry_delay(1)
                        logger.warning("告警 %s 恢复通知未送达，已排队稍后补发", name)

    async def _broadcast_recovery(self, offline_ts: float, duration_text: str) -> None:
        """Announce the recovery to every group the bot has joined.

        The bot cannot post while it is offline, so this is necessarily a
        recovery-time action. Off by default: most bots sit in groups whose
        members never asked for status updates, and posting to dozens of
        groups in a loop is exactly the behaviour QQ risk control punishes.
        """

        cfg = cfg_get(self.config, "alerts.offline_broadcast", {}) or {}
        if not isinstance(cfg, dict) or not cfg_bool(cfg, "enabled", False):
            return
        self_id = str(cfg_get(self.config, "alerts.self_id", "") or "").strip()
        client = _get_aiocqhttp_client(
            self.plugin.context, self.platform_id, self_id or None
        )
        if client is None:
            logger.warning("掉线广播：未找到 OneBot 客户端，跳过")
            return
        try:
            raw = await _call_action(client, "get_group_list", timeout=15)
        except Exception as exc:
            logger.warning("掉线广播：获取群列表失败（%s）", exc)
            return
        # aiocqhttp's ``call_action`` already unwraps the protocol envelope,
        # so ``get_group_list`` returns a bare list. Parsing it with the
        # object helper turned that list into ``{}`` and the broadcast scanned
        # zero groups on every recovery since v2.15.0 (2026-10-30 实测日志
        # 「扫描 0 个群」). Accept both shapes, like the collector does.
        groups = _normalize_list(raw)
        if groups is None:
            envelope = _normalize_object(raw)
            groups = envelope.get("data") or envelope.get("groups") or []
        if isinstance(groups, dict):
            groups = groups.get("data") or groups.get("groups") or []
        exclude = {
            str(item).strip()
            for item in (cfg.get("exclude_groups") or [])
            if str(item).strip()
        }
        interval = max(1, min(30, cfg_int(cfg, "interval_seconds", 3)))
        # Match the schema slider (1-500). A hand-edited 0 used to mean
        # "unlimited" (the truthiness check skipped the cap) and a 10000 was
        # taken literally, so a broken value posted to every group.
        max_groups = max(1, min(500, cfg_int(cfg, "max_groups", 100)))
        # The formal recovery notice goes to the targets that the notification
        # center actually resolves for ``offline``; those groups must not
        # receive the broadcast as well (once from the broadcast, once from
        # the reply). Using ``self.targets`` here missed the case where a
        # rule routes offline notices to another group, so that group got
        # two messages and the default target got none.
        notify_groups: set[str] = set()
        for target in self._targets_for("offline"):
            text_target = str(target or "").strip()
            # Only group targets contribute a group id to skip; a private
            # chat whose QQ number happens to equal a group id must not
            # suppress that group's broadcast (v2.20.0).
            if "GroupMessage" not in text_target:
                continue
            tail = text_target.rsplit(":", 1)[-1]
            if tail.isdigit():
                notify_groups.add(tail)
        template = str(cfg.get("template") or BROADCAST_TEMPLATE_DEFAULT)
        offline_text = _fmt_ts(offline_ts, self.config)
        recover_text = _now_text(self.config)
        sent = failed = considered = 0
        for group in groups:
            group_id = (
                str(group.get("group_id") or "") if isinstance(group, dict) else ""
            )
            if not group_id or group_id in exclude or group_id in notify_groups:
                continue
            considered += 1
            if considered > max_groups:
                logger.warning("掉线广播：群数超过上限 %d，剩余群未发送", max_groups)
                break
            try:
                # Replace known tokens one by one: ``str.format`` raised
                # KeyError on any unknown brace and then sent the template
                # verbatim, so a note like "{备注}" also froze the real
                # placeholders. The hint promises the rest is preserved.
                text = template
                for token, value in (
                    ("{offline_time}", offline_text),
                    ("{recover_time}", recover_text),
                    ("{duration}", duration_text),
                ):
                    text = text.replace(token, value)
            except (TypeError, ValueError):
                text = template
            if sent or failed:
                await asyncio.sleep(interval)
            target = f"{self.platform_id}:GroupMessage:{group_id}"
            try:
                result = await self.plugin.send_text(
                    [target], text, source="offline_broadcast"
                )
            except Exception:
                failed += 1
                logger.debug("掉线广播群 %s 发送异常", group_id, exc_info=True)
                continue
            if self._delivered(result):
                sent += 1
            else:
                failed += 1
        logger.info(
            "掉线恢复广播完成：成功 %d / 失败 %d（扫描 %d 个群，排除 %d 个）",
            sent,
            failed,
            len(groups),
            max(0, len(groups) - considered),
        )

    async def _check_offline(self) -> None:
        # Without a self_id filter a multi-bot instance could probe another
        # bot's connection and report the wrong one as offline.
        self_id = str(cfg_get(self.config, "alerts.self_id", "") or "").strip()
        client = _get_aiocqhttp_client(
            self.plugin.context, self.platform_id, self_id or None
        )
        if client is None:
            # A platform_id that does not match the AstrBot instance id makes
            # disconnect detection and OneBot direct sends silently no-op;
            # warn once so the diagnosis is not "the bot just never alerts".
            if not self._client_missing_warned:
                self._client_missing_warned = True
                logger.warning(
                    "掉线检测：未找到 aiocqhttp 客户端（platform_id=%s、self_id=%s），"
                    "掉线告警与 OneBot 直发不可用；请确认「告警平台 ID」与 AstrBot "
                    "平台实例 ID 一致（或留空 self_id）",
                    self.platform_id,
                    self_id or "（空）",
                )
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
        count = _safe_int(st.get("count"))
        unreachable = _safe_int(st.get("unreachable"))

        if online:
            st["count"] = 0
            st["unreachable"] = 0
            st["unreachable_warned"] = False
            pending_text = st.get("pending_recovery") if self.offline_notify else None
            if pending_text and not self.recover_notify:
                # Recovery notices were turned off after this one was queued:
                # honour the switch and drop the backlog instead of delivering
                # it on the next online sample (resource/provider paths do the
                # same).
                for key in (
                    "pending_recovery",
                    "recovery_attempt",
                    "recovery_next_attempt",
                    "recovery_retry_count",
                ):
                    st.pop(key, None)
                pending_text = None
            if not self.offline_notify:
                for key in (
                    "pending_recovery",
                    "recovery_attempt",
                    "recovery_next_attempt",
                    "recovery_retry_count",
                    "pending_alert_text",
                ):
                    st.pop(key, None)
                st["pending_alert"] = False
            if pending_text:
                next_recovery = _safe_float(st.get("recovery_next_attempt"))
                if now >= next_recovery:
                    result = await self._send(str(pending_text), kind="offline")
                    if self._delivered(result):
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                        logger.info("Bot 连接恢复通知补发成功")
                    else:
                        retry_count = _safe_int(st.get("recovery_retry_count")) + 1
                        if retry_count > MAX_RECOVERY_RETRIES:
                            logger.warning(
                                "Bot 连接恢复通知连续 %d 次未送达，放弃补发",
                                retry_count - 1,
                            )
                            for stale_key in (
                                "pending_recovery",
                                "recovery_attempt",
                                "recovery_next_attempt",
                                "recovery_retry_count",
                            ):
                                st.pop(stale_key, None)
                        else:
                            st["recovery_retry_count"] = retry_count
                            st["recovery_next_attempt"] = now + retry_delay(retry_count)
                            logger.warning("Bot 连接恢复通知补发失败，等待下次重试")
            if st.get("alerting") or st.get("pending_alert_text"):
                stale = bool(st.pop("stale", False))
                duration = now - _safe_float(st.get("since"), now)
                queued_raw = st.pop("pending_alert_text", None)
                # A stale episode is one where the outage window is too old
                # to push anything; delivering the hours-old alert text now
                # -- without any context -- contradicts that. Drop it (the
                # event log has the full story), same as the recovery notice.
                queued = queued_raw if (self.offline_notify and not stale) else None
                st["alerting"] = False
                st["since"] = 0
                parts: list[str] = []
                if queued:
                    parts.append(str(queued))
                if self.offline_notify and self.recover_notify and not stale:
                    text = (
                        "✅ Bot 连接已恢复\n"
                        "──────────────\n"
                        f"主机: {self._hostname()}\n"
                        f"掉线时间: {_fmt_ts(now - duration, self.config)}\n"
                        f"恢复时间: {_now_text(self.config)}\n"
                        f"离线时长: {format_duration(duration)}"
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
                if not stale:
                    # A hundred-group broadcast with a 3s gap can take five
                    # minutes; running it inline stalled every later sample
                    # and delayed the operator's own recovery notice.
                    if self._broadcast_task and not self._broadcast_task.done():
                        # A second recovery while the first broadcast is still
                        # running used to orphan that task: stop() could only
                        # cancel the newest one and the old one kept posting
                        # after shutdown.
                        self._broadcast_task.cancel()
                    self._broadcast_task = asyncio.create_task(
                        self._broadcast_recovery(
                            now - duration, format_duration(duration)
                        )
                    )
                    self._broadcast_task.add_done_callback(self._on_task_done)
                if self.offline_notify and parts:
                    text = "\n\n".join(parts)
                    result = await self._send(text, kind="offline")
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
            # bot, which would raise a false alarm. The confirmed-offline
            # counter is left untouched instead of reset, so alternating
            # timeouts cannot keep the alert from ever firing (v2.20.2).
            st["unreachable"] = unreachable + 1
            if unreachable + 1 >= OFFLINE_ALERT_SAMPLES * 3 and not st.get(
                "unreachable_warned"
            ):
                st["unreachable_warned"] = True
                logger.warning(
                    "OneBot 状态查询连续 %d 次失败，暂时无法确认 Bot 在线状态",
                    unreachable + 1,
                )
            return

        st["unreachable"] = 0
        st["unreachable_warned"] = False
        if not st.get("alerting"):
            # Once latched the counter is not used and grew without bound for
            # multi-day outages (state.json bloat).
            count += 1
            st["count"] = count
        if not st.get("alerting") and count >= OFFLINE_ALERT_SAMPLES:
            st["alerting"] = True
            st["since"] = now
            st["count"] = 0
            if not self.offline_notify:
                # Keep the incident in the event log; only the push is off.
                self._record_event("offline", "Bot 连接", "连接离线", "alert")
                return
            text = (
                "🔴 Bot 连接离线\n"
                "──────────────\n"
                f"主机: {self._hostname()}\n"
                f"时间: {_now_text(self.config)}\n"
                "若本条未能送达，将在连接恢复后补发。"
            )
            result = await self._send(text, kind="offline")
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

    def _close_provider_watch(self, reason: str) -> None:
        """Resolve a latched LLM alert when the rule stops running.

        Turning ``provider_watch`` off (or setting its threshold to 0) used to
        leave ``alerting=True`` behind: the daily report kept showing an
        "unresolved" LLM incident no later event could close.
        """

        alerts_state = self.state.setdefault("alerts", {})
        watch = alerts_state.get("provider_watch")
        if not isinstance(watch, dict):
            return
        # Drop a queued recovery even when the rule is no longer alerting:
        # pending_recovery survives in exactly that state, and re-enabling the
        # watch used to deliver a stale "已恢复" from the previous episode.
        watch.pop("pending_recovery", None)
        watch.pop("recovery_retry_count", None)
        watch.pop("recovery_next_attempt", None)
        watch.pop("send_failed", None)
        if not watch.get("alerting"):
            return
        watch["alerting"] = False
        watch["alerted"] = False
        watch["alert_since"] = 0
        self._record_event("provider", "LLM 服务", reason, "recovery")
        logger.info("LLM 服务告警已关闭：%s", reason)

    async def _check_provider_quality(self) -> None:
        """Alert when the newest provider_stats rows fail consecutively.

        Re-checked at most once a minute on top of the sampling loop; the
        walk itself reads a handful of rows from data_v4.db read-only in a
        worker thread. ``aborted`` rows neither count nor break the streak,
        so a user stop in the middle cannot hide a real failure streak.
        Recovery requires an explicit ``completed`` as the newest row -
        an unreadable table keeps the current state instead of resolving
        the alert. A send failure is recorded as a ``failed`` event, the
        rule still latches and the recovery notice is queued with retries
        (``pending_recovery``), so one unreachable window cannot swallow
        the whole episode. A second threshold hit inside the cooldown
        still latches and records an event - only the duplicate push is
        suppressed, so the report keeps the full incident trail.
        """

        now = time.time()
        if now - self._provider_last_check < 60:
            return
        if self.provider_watch_threshold <= 0:
            self._close_provider_watch("LLM 服务监控阈值已关闭")
            return
        self._provider_last_check = now
        try:
            streak, latest, sampled = await asyncio.to_thread(
                provider_recent_streak,
                None,
                max(30, self.provider_watch_threshold * 3),
            )
        except Exception:
            logger.debug("LLM 服务质量检查失败", exc_info=True)
            return
        if not sampled:
            return

        alerts_state = self.state.setdefault("alerts", {})
        # setdefault (not get): the live nested dict is required, otherwise
        # the alerting latch below would mutate a detached local dict.
        watch = alerts_state.setdefault("provider_watch", {})
        alerting = bool(watch.get("alerting"))
        last_alert_ts = _safe_float(watch.get("last_alert_ts"))

        # Retries for a recovery notice whose send failed earlier.
        pending_text = str(watch.get("pending_recovery") or "")
        if pending_text and not self.recover_notify:
            # The operator turned recovery notices off after this one was
            # queued; drop the backlog like the resource/monitor rules do
            # instead of delivering it later anyway (v2.20.0).
            for stale_key in (
                "pending_recovery",
                "recovery_retry_count",
                "recovery_next_attempt",
            ):
                watch.pop(stale_key, None)
            self.state.save()
            pending_text = ""
        if pending_text:
            next_attempt = _safe_float(watch.get("recovery_next_attempt"))
            if now >= next_attempt:
                result = await self._send(
                    pending_text, source="provider", kind="provider"
                )
                if self._delivered(result):
                    watch.pop("pending_recovery", None)
                    watch.pop("recovery_retry_count", None)
                    watch.pop("recovery_next_attempt", None)
                    self.state.save()
                    logger.info("LLM 服务恢复通知补发成功")
                else:
                    retry_count = int(watch.get("recovery_retry_count") or 1) + 1
                    if retry_count > MAX_RECOVERY_RETRIES:
                        watch.pop("pending_recovery", None)
                        watch.pop("recovery_retry_count", None)
                        watch.pop("recovery_next_attempt", None)
                        self.state.save()
                        logger.warning(
                            "LLM 服务恢复通知连续发送失败 %d 次，放弃补发",
                            retry_count - 1,
                        )
                    else:
                        watch["recovery_retry_count"] = retry_count
                        watch["recovery_next_attempt"] = now + retry_delay(retry_count)
                        self.state.save()
            # Fall through to the streak check: a new LLM failure arriving
            # while the old recovery notice was being retried used to be
            # silent for up to ~30 minutes (v2.20.0).

        if not alerting and streak >= self.provider_watch_threshold:
            text = (
                "⚠️ LLM 服务连续失败\n"
                "──────────────\n"
                f"主机: {self._hostname()}\n"
                f"连续失败: {streak} 次（阈值 {self.provider_watch_threshold}）\n"
                f"最近状态: {_PROVIDER_STATUS_TEXT.get(str(latest or ''), latest or '未知')}\n"
                f"时间: {_now_text(self.config)}"
            )
            in_cooldown = bool(
                last_alert_ts and now - last_alert_ts < self.provider_watch_cooldown
            )
            if in_cooldown:
                # Latch and record without pushing. ``alerted=False`` keeps
                # the recovery notice from arriving without a matching alert
                # -- the exact defect v2.18.1 fixed for the resource rules.
                watch["alerting"] = True
                watch["alerted"] = False
                watch["last_streak"] = streak
                self.state.save()
                self._record_event(
                    "provider",
                    "LLM 服务",
                    f"连续 {streak} 次 LLM 调用失败（冷却窗口内，未重复推送）",
                    "alert",
                )
                logger.warning(
                    "LLM 服务冷却窗口内再次连续失败 %d 次，已记录事件不重复推送",
                    streak,
                )
                return
            result = await self._send(text, source="provider", kind="provider")
            delivered = self._delivered(result)
            watch["alerting"] = True
            # A failed push still owes the user an explanation, which the
            # recovery notice carries; only cooldown suppression is silent.
            watch["alerted"] = True
            watch["send_failed"] = not delivered
            watch["last_alert_ts"] = now
            watch["last_streak"] = streak
            self.state.save()
            if delivered:
                self._record_event(
                    "provider",
                    "LLM 服务",
                    f"连续 {streak} 次 LLM 调用失败",
                    "alert",
                )
                logger.warning("LLM 服务连续失败 %d 次，已发送告警", streak)
            else:
                self._record_event(
                    "provider",
                    "LLM 服务",
                    f"连续 {streak} 次 LLM 调用失败（推送失败）",
                    "failed",
                )
                logger.warning("LLM 服务告警发送失败，恢复时将一并通知")
            return

        if alerting and latest == "completed":
            watch["alerting"] = False
            # Only announce a recovery for an episode that actually reached
            # the user; a cooldown-suppressed one is logged but silent.
            notified = bool(watch.pop("alerted", True))
            self.state.save()
            self._record_event(
                "provider",
                "LLM 服务",
                "最近一次 LLM 调用成功",
                "recovery",
            )
            if self.recover_notify and notified:
                last_streak = _safe_int(watch.get("last_streak"))
                had_failed = bool(watch.pop("send_failed", False))
                text = (
                    "✅ 已恢复 · LLM 服务\n"
                    "──────────────\n"
                    f"主机: {self._hostname()}\n"
                    + (f"此前连续失败: {last_streak} 次\n" if last_streak else "")
                    + "最近状态: 调用成功\n"
                    + ("（异常告警此前未能送达）\n" if had_failed else "")
                    + f"时间: {_now_text(self.config)}"
                )
                result = await self._send(text, source="provider", kind="provider")
                if not self._delivered(result):
                    # The recovery notice is the only signal for a suppressed
                    # or failed-alert episode; retry it before giving up.
                    watch["pending_recovery"] = text
                    watch["recovery_retry_count"] = 1
                    watch["recovery_next_attempt"] = now + retry_delay(1)
                    self.state.save()
                    logger.warning("LLM 服务恢复通知发送失败，稍后补发")
                else:
                    # A queued notice from an earlier episode must not fire a
                    # second "已恢复" after this one was delivered.
                    for stale_key in (
                        "pending_recovery",
                        "recovery_retry_count",
                        "recovery_next_attempt",
                    ):
                        watch.pop(stale_key, None)
            logger.info("LLM 服务已恢复（最近一次调用成功）")

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
                attempt_day = now_in_timezone(self.config).strftime("%Y-%m-%d")
                try:
                    sent_ok = await self._send_daily_summary()
                    if not sent_ok:
                        for attempt in range(3):
                            await asyncio.sleep(600)
                            if not self._running:
                                break
                            if (
                                now_in_timezone(self.config).strftime("%Y-%m-%d")
                                != attempt_day
                            ):
                                # Crossed midnight: the next attempt would
                                # render "today's" image for yesterday's slot
                                # and mark the new day as sent, skipping its
                                # scheduled run. Let the new day start clean.
                                logger.warning(
                                    "每日定时状态图重试跨天，已停止；等待当天计划时间"
                                )
                                break
                            if await self._send_daily_summary():
                                break
                            logger.warning(
                                "每日定时状态图重试失败（第 %d 次）",
                                attempt + 1,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # An exception escaped the normal "give up after four
                    # attempts" path, so the loop retried the whole collect +
                    # T2I cycle every 60 seconds for the rest of the day.
                    logger.exception("每日定时状态图发送异常，今天不再重试")
                if now_in_timezone(self.config).strftime("%Y-%m-%d") == attempt_day:
                    # Only mark the day the attempt started on; a cross-midnight
                    # break must not consume the new day's scheduled run.
                    self._mark_daily_attempt(attempt_day)
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
        targets = self._targets_for("daily_image")
        if not targets:
            logger.warning(
                "每日定时状态图已启用，但推送目标为空（通知中心无 daily_image "
                "规则，alerts.targets 也未填写），跳过发送"
            )
            return True
        # Record the attempt before sending: if the process is reloaded right
        # after the image was delivered but before _mark_daily_sent, the
        # restart saw "not attempted today" and sent a second copy.
        self._mark_daily_attempt(today)
        with_image = cfg_bool(self.config, "alerts.daily_summary.with_image", True)
        if with_image:
            try:
                image_url = await self.plugin.build_status_image()
            except Exception:
                logger.exception("每日定时状态图渲染失败")
                image_url = None
            if image_url:
                result = await self.plugin.send_image(
                    targets,
                    image_url,
                    source="daily_summary",
                )
                if self._delivered(result):
                    self._mark_daily_sent(today)
                    logger.info("每日定时状态图已发送（图片）：%s", targets)
                    return True
        summary = await self.plugin.build_summary_text()
        result = await self._send(
            "📅 每日定时状态图\n──────────────\n" + summary,
            source="daily_summary",
            kind="daily_image",
        )
        if self._delivered(result):
            self._mark_daily_sent(today)
            logger.info("每日定时状态图已发送（文字）：%s", targets)
            return True
        logger.warning("每日定时状态图发送失败：%s", targets)
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

        if not _webhook_bind_allowed(host, require_token, token):
            # Exposing the webhook on every interface without an enforced
            # token would let anyone post to it and push arbitrary messages
            # into the operator's chat. A token that is filled in but not
            # required is equally open, because handle() only checks it when
            # require_token is on, so the listener refuses to start.
            logger.error(
                "Komari Webhook 监听所有网卡（%s）但没有启用 Token 鉴权，已拒绝启动；"
                "请改用 127.0.0.1，或勾选「强制要求 Token」并填写 Token 后再启用",
                host,
            )
            return

        app = web.Application()
        max_body_bytes = 64 * 1024

        async def handle(request: web.Request) -> web.Response:
            # The switch decides whether a token is required; a token that is
            # filled in but with the switch off must not be enforced, or the
            # documented "off" setting silently rejects every request.
            if require_token and token:
                auth = request.headers.get("Authorization", "")
                scheme, _, credentials = auth.partition(" ")
                # Scheme match is case-insensitive per RFC 7235; older code
                # rejected "bearer <token>" outright (v2.20.0). The token
                # itself stays case-sensitive and constant-time compared.
                if scheme.lower() != "bearer" or not hmac.compare_digest(
                    credentials.strip().encode("utf-8"),
                    token.encode("utf-8"),
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
            body_key = hashlib.sha256(raw).hexdigest()
            last_key, last_ts = self._webhook_last
            if body_key == last_key and time.time() - last_ts < 5.0:
                logger.info("Komari Webhook 重复请求（5 秒内相同内容），已忽略")
                return web.json_response({"status": "ok", "duplicate": True})
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {"message": raw.decode("utf-8", errors="replace")[:4000]}
            text = self._format_komari(data)
            result = await self._send(text, source="webhook", kind="service")
            if not self._delivered(result):
                return web.json_response(
                    {"status": "error", "message": "delivery failed"},
                    status=503,
                )
            # Only remember a body that really reached the chat; a failed send
            # must stay retryable.
            self._webhook_last = (body_key, time.time())
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
        if not isinstance(alerts, dict):
            alerts = {}
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
