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
from .redactor import HIDDEN, Redactor, read_rules
from .state import StateStore
from .utils import (
    clamp_config,
    delivery_offline,
    normalize_targets,
    offline_retry_delay,
    retry_delay,
    summary_item_parts,
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


def _ram_value_text(mem: Any) -> str:
    """Format the RAM alert value with the unit word on the available figure.

    psutil's RAM percent is ``(total - available) / total``, so the pair is
    "available / total". The old text put "可用" after the total and read as
    "259 MB used / 3.64 GB available", contradicting the percentage beside
    it (2026-11-06 user screenshot review).
    """

    if mem.available is not None:
        return (
            f"{mem.percent:.1f}% "
            f"({format_bytes(mem.available)} 可用 / {format_bytes(mem.total)})"
        )
    return f"{mem.percent:.1f}% ({format_bytes(mem.used)} / {format_bytes(mem.total)})"


def _disk_path_from_label(label: Any) -> str:
    """Extract the mount path from a disk rule label, if it carries one.

    ``_evaluate`` writes the label as ``磁盘使用率 (<path>)``. The value is
    used once, to move the historical position-bound ``disk`` key onto a
    path-bound key without losing an open episode.
    """

    text = str(label or "").strip()
    prefix = "磁盘使用率 ("
    if text.startswith(prefix) and text.endswith(")"):
        return text[len(prefix) : -1]
    return ""


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
# A half-open WebSocket (NAT timeout, a hung NapCat) makes every get_status
# call time out without proving the bot is offline. Stay quiet at first so a
# container restart does not raise a false alarm, but do not stay silent
# forever: after this many consecutive failures send one notice (v2.20.5).
UNREACHABLE_ALERT_SAMPLES = 30
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
        self._provider_task: asyncio.Task | None = None
        self._daily_task: asyncio.Task | None = None
        self._webhook_task: asyncio.Task | None = None
        self._running = False
        self._webhook_runner: Any = None
        self._webhook_site: Any = None
        # Body hash of the last successfully delivered webhook request: Komari
        # retries the same payload after a timeout, which used to post the
        # event to the group twice.
        self._webhook_last: tuple[str, float] = ("", 0.0)
        self._webhook_inflight: set[str] = set()
        self._broadcast_task: asyncio.Task | None = None
        # Groups that already received the current recovery broadcast. A
        # flapping link used to cancel and restart the loop, sending a second
        # copy to the groups handled in the first pass (v2.20.5).
        self._broadcast_done: set[str] = set()
        self._client_missing_warned = False
        # When the engine started. The OneBot client does not exist for the
        # first seconds after a start-up (NapCat is still connecting), so a
        # missing client is expected during that window and must not be
        # reported as a configuration problem (v2.20.12).
        self._started_at: float = time.time()
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

    def _daily_pending(self) -> dict[str, Any] | None:
        """The queued "send today's image after the channel returns" record."""

        try:
            alerts_state = self.state.get("alerts", {}) or {}
            if not isinstance(alerts_state, dict):
                return None
            summary = alerts_state.get("daily_summary")
            if isinstance(summary, dict):
                pending = summary.get("pending")
                if isinstance(pending, dict):
                    # A queued image is only worth sending while it is
                    # still today's picture. Six hours is the outer bound:
                    # the in-loop retries (three attempts ten minutes
                    # apart) are followed by the loop's own pending retry
                    # every ten minutes up to this limit, so a sustained
                    # failure re-renders ~36 times and then drops the
                    # backlog instead of pushing a stale image hours after
                    # the slot (v2.20.20, comment corrected to match).
                    queued_at = _safe_float(pending.get("queued_at"))
                    if queued_at and time.time() - queued_at > 6 * 3600:
                        logger.warning(
                            "每日定时状态图的补发标记已超过 6 小时，丢弃（报告日 %s）",
                            pending.get("day") or "--",
                        )
                        self._clear_daily_pending()
                        return None
                    return pending
        except Exception:
            logger.debug("读取每日状态图补发标记失败", exc_info=True)
        return None

    def _set_daily_pending(self, day: str, next_ts: float) -> None:
        try:
            with self.state.section("alerts") as alerts:
                summary = alerts.get("daily_summary")
                if not isinstance(summary, dict):
                    summary = {}
                    alerts["daily_summary"] = summary
                pending = summary.get("pending")
                queued_at = time.time()
                if isinstance(pending, dict) and pending.get("day") == day:
                    # Keep the age of the FIRST failure: refreshing it on
                    # every retry made the six-hour limit unreachable, so a
                    # broken target could queue the picture forever (and
                    # every attempt rendered a new image).
                    queued_at = _safe_float(pending.get("queued_at")) or queued_at
                summary["pending"] = {
                    "day": day,
                    "next": float(next_ts),
                    # The age of the record, so an old backlog can be
                    # dropped instead of sending a stale picture.
                    "queued_at": queued_at,
                }
            self.state.save()
        except Exception:
            logger.debug("记录每日状态图补发标记失败", exc_info=True)

    def _clear_daily_pending(self) -> None:
        try:
            with self.state.section("alerts") as alerts:
                summary = alerts.get("daily_summary")
                if isinstance(summary, dict) and "pending" in summary:
                    summary.pop("pending", None)
            self.state.save()
        except Exception:
            logger.debug("清除每日状态图补发标记失败", exc_info=True)

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

        changed = False
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
                if not isinstance(st, dict):
                    continue
                if st.get("recovery_awaits_summary"):
                    # The recovery was queued because its alert sat in a
                    # summary that lived only in the aggregator's memory. A
                    # restart took that summary away, so the recovery must
                    # go with it: otherwise the user receives a stale
                    # "已恢复" for an incident they were never told about
                    # (v2.20.12).
                    #
                    # This case is NOT covered by the ``alerting`` check
                    # below: the recovery is queued after the episode was
                    # closed, so ``alerting`` is already False.
                    st.pop("pending_recovery", None)
                    st.pop("recovery_offline_count", None)
                    st.pop("recovery_attempt", None)
                    st.pop("recovery_next_attempt", None)
                    st.pop("recovery_retry_count", None)
                    st.pop("recovery_awaits_summary", None)
                    st.pop("summary_key", None)
                    st.pop("summary_reason", None)
                    changed = True
                    logger.info(
                        "告警规则 %s 的排队恢复在重启前失去对应的汇总，已撤回", key
                    )
                if not st.get("alerting"):
                    continue
                if st.get("awaiting_summary"):
                    # The buffered alert lived only in the aggregator's
                    # memory. A hard stop (systemctl restart, OOM) inside the
                    # window leaves the rule marked "notified" while the
                    # summary that carried it is gone: the alert is lost for
                    # good, and a later recovery announces an outage the user
                    # never heard about. Treat it like an interrupted direct
                    # send: retryable, not announced.
                    st["alerted"] = False
                    st["summary_abandoned"] = True
                    st.pop("awaiting_summary", None)
                    st.pop("summary_buffered", None)
                    st.pop("summary_key", None)
                    st.pop("summary_reason", None)
                    # The queued recovery belongs to the summary that never
                    # arrived, so it has to go with it: leaving it behind
                    # delivered a stale "已恢复" for an incident the user
                    # was never told about (v2.20.12).
                    st.pop("pending_recovery", None)
                    st.pop("recovery_offline_count", None)
                    st.pop("recovery_attempt", None)
                    st.pop("recovery_next_attempt", None)
                    st.pop("recovery_retry_count", None)
                    st.pop("recovery_awaits_summary", None)
                    changed = True
                    logger.info(
                        "告警规则 %s 的聚合汇总在重启前未送达，已标记为可重试", key
                    )
                # A corrupted/hand-edited state value must not abort startup:
                # monitor.start() and every later background task would never
                # run while the plugin still looked loaded. Degrade to "no
                # timestamp" (skip the stale mark) instead of raising.
                last_seen = _safe_float(st.get("last_seen") or st.get("alert_since"))
                if not last_seen and key == "offline":
                    last_seen = _safe_float(st.get("since"))
                if last_seen and now - last_seen > 86400:
                    st["stale"] = True
                    changed = True
                    logger.warning(
                        "告警规则 %s 已超过 24 小时未采样（可能关闭了告警），"
                        "恢复时只记事件、不发恢复消息",
                        key,
                    )
        # These markers only matter across a restart, so they have to reach
        # the disk: without the save the next process reads the stale
        # "alerted=True" and loses the alert again (v2.20.12).
        if changed:
            self.state.save()

    def start(self) -> None:
        if self._running or (
            self._monitor_task is not None and not self._monitor_task.done()
        ):
            logger.debug("状态卡告警任务已在运行，忽略重复启动")
            return
        alerts_on = cfg_bool(self.config, "alerts.enabled", False)
        daily_on = cfg_bool(self.config, "alerts.daily_summary.enabled", False)
        # ``provider_watch`` is documented as independent from the resource
        # on/off switch (schema and README say the master switch only covers
        # resource + offline alerts). Its check used to run only inside the
        # resource sampling loop, so enabling ONLY the LLM watch did nothing
        # at all -- no check, no alert, no recovery (v2.20.21).
        provider_on = cfg_bool(self.config, "alerts.provider_watch.enabled", False)
        self._running = alerts_on or daily_on or provider_on
        if alerts_on:
            # A hand-edited state.json can hold a list/scalar here; replacing
            # it under the store's lock keeps start-up from crashing.
            with self.state.section("alerts"):
                pass
            self._mark_stale_rules()
        if alerts_on:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            self._monitor_task.add_done_callback(self._on_task_done)
        elif provider_on:
            # No resource loop to piggyback on: run the LLM check on its own.
            self._provider_task = asyncio.create_task(self._provider_loop())
            self._provider_task.add_done_callback(self._on_task_done)
        if daily_on:
            self._daily_task = asyncio.create_task(self._daily_loop())
            self._daily_task.add_done_callback(self._on_task_done)
            self._log_next_daily()
        self._start_webhook_task()
        if not provider_on:
            # A latch left behind by an earlier enabled watch would otherwise
            # stay "unresolved" in the daily report forever.
            self._close_provider_watch("LLM 服务监控已关闭")
        if not self._running:
            logger.info("状态卡主动告警与每日定时状态图均未启用")
            return
        logger.info(
            "状态卡任务已启动：阈值告警=%s，每日定时状态图=%s，LLM 服务告警=%s，"
            "目标=%s，间隔=%ss，阈值 CPU %.0f%% / RAM %.0f%% / SWAP %.0f%% / DISK %.0f%%",
            alerts_on,
            daily_on,
            provider_on,
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
            self._provider_task,
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
        self._provider_task = None
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

    async def _push_alert(
        self,
        name: str,
        reason: str,
        text: str,
        retry: bool = False,
        item_key: str = "",
    ) -> Any | None:
        """Send the first alert of an episode, or buffer it in the window.

        A retry of the same episode bypasses the aggregator: when the first
        push was rejected the 30-second retry lands inside the 120-second
        window, and the summary then delivered "⚠️ 告警汇总 · 1 项异常" with
        the detailed text thrown away (v2.20.8).
        """

        if self._aggregator is None or retry:
            return await self._send(text)
        return await self._aggregator.push(
            label=name,
            reason=reason,
            text=text,
            send=lambda text: self._send(text, kind="resource"),
            kind="resource",
            on_abandon=self._on_summary_abandoned,
            on_delivered=self._on_summary_delivered,
            item_key=item_key,
        )

    def _on_summary_delivered(self, items: list[tuple[str, str]]) -> None:
        """Clear the "waiting for the summary" markers after it lands.

        A buffered alert is announced exactly when its summary reaches the
        chat. Keeping ``awaiting_summary``/``summary_buffered`` behind made
        the reload repair (``_mark_stale_rules``) believe the summary had
        been lost: it withdrew ``alerted`` and marked the rule abandoned, so
        a still-failing rule alerted a second time and a recovered one lost
        its recovery notice (v2.20.17).
        """

        parts = [summary_item_parts(item) for item in items]
        labels = {label for label, _reason, _key in parts if label}
        pairs = {(label, reason) for label, reason, _key in parts}
        keys = {key for _label, _reason, key in parts if key}
        if not labels:
            return
        changed = False
        with self.state.section("alerts") as alerts:
            rules = alerts.get("rules")
            if not isinstance(rules, dict):
                return
            for st in rules.values():
                if not isinstance(st, dict):
                    continue
                # Match by the exact rule key FIRST: the batch label may be a
                # MASKED name (the hostname rule rewrites an address-shaped
                # disk path to "已隐藏") while the state keeps the original
                # label, so filtering by label before the key made every
                # masked rule miss its own summary (v2.20.20 reverse audit).
                # Batches without keys (legacy) still match by
                # (name, reason), and a rule with no key keeps that fallback.
                key = str(st.get("summary_key") or "")
                matched_by_key = bool(keys and key and key in keys)
                if keys and key and not matched_by_key:
                    # The batch carries keys and this rule owns a different
                    # one: not its summary.
                    continue
                if not matched_by_key:
                    label = str(st.get("label") or "")
                    if label not in labels:
                        continue
                    reason = str(st.get("summary_reason") or "")
                    if reason and (label, reason) not in pairs:
                        continue
                st.pop("summary_key", None)
                st.pop("summary_reason", None)
                # A rule that recovered inside the aggregation window is no
                # longer ``alerting``, but its queued recovery still waits
                # for this very summary. Clear the marker here: the summary
                # DID reach the chat, so a later reload must not mistake the
                # recovery for an orphan and delete it (v2.20.19).
                if st.pop("recovery_awaits_summary", False):
                    changed = True
                if not st.get("alerting"):
                    continue
                # Only rules whose alert was actually buffered. A same-named
                # rule that delivered its own alert already cleared this
                # marker (v2.20.14); touching it here would hide a *real*
                # abandon of its own later summary.
                if not st.pop("summary_buffered", None):
                    continue
                st.pop("awaiting_summary", None)
                changed = True
        if changed:
            self.state.save()

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

        parts = [summary_item_parts(item) for item in items]
        labels = {label for label, _reason, _key in parts if label}
        pairs = {(label, reason) for label, reason, _key in parts}
        keys = {key for _label, _reason, key in parts if key}
        if not labels:
            return
        changed = False
        with self.state.section("alerts") as alerts:
            rules = alerts.get("rules")
            if not isinstance(rules, dict):
                return
            for st in rules.values():
                if not isinstance(st, dict):
                    continue
                # Key first, label only as the legacy fallback: the batch
                # label may be masked while the state keeps the original name
                # (v2.20.20 reverse audit, same shape as the delivered
                # callback above).
                key = str(st.get("summary_key") or "")
                matched_by_key = bool(keys and key and key in keys)
                if keys and key and not matched_by_key:
                    continue
                if not matched_by_key:
                    label = str(st.get("label") or "")
                    if not label or label not in labels:
                        continue
                    reason = str(st.get("summary_reason") or "")
                    if reason and (label, reason) not in pairs:
                        # A different rule with the same name owns this summary.
                        continue
                st.pop("summary_key", None)
                st.pop("summary_reason", None)
                if st.get("recovery_awaits_summary"):
                    # The rule recovered while its alert was still buffered in
                    # this very summary, so the queued "已恢复" exists only
                    # because the summary promised to announce the alert. The
                    # summary never arrived: drop the recovery too, otherwise
                    # the chat gets "✅ 已恢复 · 内存使用率" for an outage the
                    # user was never told about.
                    st.pop("pending_recovery", None)
                    st.pop("recovery_offline_count", None)
                    st.pop("recovery_attempt", None)
                    st.pop("recovery_next_attempt", None)
                    st.pop("recovery_retry_count", None)
                    st.pop("recovery_awaits_summary", None)
                    # The summary was the only thing that would have
                    # announced this rule, so the rule was never notified.
                    # Leaving ``alerted`` set kept every later fault of this
                    # rule silent for the rest of the outage (the push gates
                    # all require "not alerted"), while the recovery notice
                    # still went out (v2.20.12).
                    st["alerted"] = False
                    st["summary_abandoned"] = True
                    st.pop("awaiting_summary", None)
                    changed = True
                    continue
                if not st.get("alerting"):
                    continue
                # Only withdraw rules that were actually BUFFERED. A
                # same-named rule whose alert went out on its own keeps its
                # "notified" state: the abandon callback matches by name, so
                # without this gate the rule that delivered its own alert
                # was marked "not notified" too, and its next fault pushed a
                # duplicate alert (v2.20.14, same fix as the service
                # monitors).
                if not st.pop("summary_buffered", None):
                    continue
                st["alerted"] = False
                # Keep the episode retryable. Clearing ``last_sent``
                # removed the only signal the cooldown-retry path uses,
                # so the rule stayed latched-but-unannounced for the rest
                # of the outage: no alert, and (because ``alerted`` was
                # False) no recovery notice either. The user never heard
                # about the incident at all.
                st["summary_abandoned"] = True
                # ``last_sent`` is deliberately kept: it is the only
                # signal the cooldown-retry path has for *later* episodes,
                # and v2.20.4 relies on it. The abandoned summary itself
                # skips the cooldown gate below instead (v2.20.5) -- that
                # was the actual 30-minute delay, without breaking the
                # retryable state.
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

    async def send_test(self, target_umo: str | None = None) -> dict[str, Any]:
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
            return {
                "ok": False,
                "targets": [],
                "ok_targets": [],
                "failed_targets": [],
                "uncertain_targets": [],
            }
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
            # Return the per-target lists so the command can name the bad
            # target instead of a bare "发送失败" (the invoking chat often
            # DID receive the message, which made the reply contradict the
            # notification the operator was looking at) (v2.20.19).
            ok_targets = list(result.ok_targets)
            failed = list(result.failed_targets)
            uncertain = list(result.uncertain_targets)
            return {
                "ok": bool(ok_targets) and not (failed or uncertain),
                "targets": targets,
                "ok_targets": ok_targets,
                "failed_targets": failed,
                "uncertain_targets": uncertain,
            }
        ok = bool(result)
        return {
            "ok": ok,
            "targets": targets,
            "ok_targets": list(targets) if ok else [],
            "failed_targets": [] if ok else list(targets),
            "uncertain_targets": [],
        }

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

    async def _provider_loop(self) -> None:
        """Run the LLM quality check when the resource rules are disabled."""

        await asyncio.sleep(1)
        while self._running:
            try:
                with self.state.section("alerts"):
                    pass
                if not self.provider_watch_enabled:
                    break
                await self._check_provider_quality()
                # This loop is the ONLY place that persists provider state
                # when the resource loop is off: some ``_check_provider_quality``
                # branches pop queued copies after their last save, so the
                # unconditional save is what makes those removals reach disk.
                self.state.save()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("LLM 服务检查失败")
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
            _ram_value_text(mem),
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

        # Every mount owns a path-based key. The first mount used to take the
        # historical "disk" key, which bound a latch to a POSITION rather
        # than to a mount: editing display.disk_paths (dropping the first
        # entry, reordering) moved one mount's alerting/alerted/cooldown onto
        # another disk -- a fake recovery carrying the old mount's duration,
        # and silence for a disk that was still full (v2.20.21).
        #
        # Migrate the historical "disk" key once. Its label carries the mount
        # path, so an open episode can move to "disk:<path>" without writing
        # a bogus "rule removed" recovery.
        with self.state.section("alerts") as alerts:
            rules = alerts.get("rules")
            if isinstance(rules, dict):
                legacy = rules.get("disk")
                if isinstance(legacy, dict):
                    legacy_path = _disk_path_from_label(legacy.get("label"))
                    if not legacy_path:
                        # No path in the label (very old or hand-edited
                        # state). Attaching it to disk_paths[0] would move
                        # the latch onto a DIFFERENT mount -- the exact bug
                        # this change removes. Leave the key in place: the
                        # ``_prune_rules`` pass at the end of this sample
                        # sees "disk" as removed and closes the episode with
                        # a proper recovery event.
                        pass
                    else:
                        target_key = f"disk:{legacy_path}"
                        existing = rules.get(target_key)
                        if isinstance(existing, dict):
                            # A path-keyed record from an older layout
                            # exists. The historical "disk" entry is the
                            # newer state of the same mount, so let it win.
                            rules[target_key] = {**existing, **legacy}
                        else:
                            rules[target_key] = dict(legacy)
                        rules.pop("disk", None)
        valid_keys = {"cpu", "ram", "swap", "load"}
        for disk_path in disk_paths:
            rule_key = f"disk:{disk_path}"
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
                        st.pop("summary_buffered", None)
                        st.pop("summary_key", None)
                        st.pop("summary_reason", None)
                        st.pop("recovery_awaits_summary", None)
                        for stale_key in (
                            "pending_recovery",
                            "recovery_offline_count",
                            "recovery_attempt",
                            "recovery_next_attempt",
                            "recovery_retry_count",
                            # The episode is closed for good (mount gone): a
                            # leftover offline backoff would slow the first
                            # alert after the mount returns.
                            "offline_retry_count",
                        ):
                            st.pop(stale_key, None)
                        closed = True
                        if notified and self.recover_notify:
                            close_text = (
                                "✅ 告警已关闭 · 磁盘挂载点不可读\n"
                                "──────────────\n"
                                f"挂载点: {self._display_name(disk_path)}\n"
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
            # sent. The message may or may not have reached the chat, so the
            # episode is latched -- but it must stay *retryable*: the previous
            # version latched it with ``alerted=False`` and no cooldown
            # reference, which left the rule silent for the whole outage and
            # withheld the recovery notice as well. The operator ended up
            # with no message at all. Retry it once the cooldown allows.
            alerting = True
            st["alerting"] = True
            st["alerted"] = False
            st["alert_since"] = _safe_float(st.get("last_attempt")) or now
            st["count"] = 0
            # Mark it as "never announced" so the cooldown-retry path picks
            # it up instead of treating it as an already-delivered alert.
            st["summary_abandoned"] = True
            # Log an alert row so the episode has a start in the event log;
            # without it the later recovery had no matching beginning.
            self._record_event(
                "resource",
                name,
                "上次告警发送被中断（重载/重启），将重新补发",
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
            # Buffered-summary markers are illegal once the rule is off:
            # leaving ``summary_buffered`` behind let a later same-name
            # abandon withdraw an alert that had already been delivered.
            st.pop("summary_buffered", None)
            st.pop("summary_key", None)
            st.pop("summary_reason", None)
            st.pop("recovery_awaits_summary", None)
            # A queued recovery from the disabled rule must not survive:
            # after re-enabling it would be delivered as a stale notice.
            for stale_key in (
                "pending_recovery",
                "recovery_offline_count",
                "recovery_attempt",
                "recovery_next_attempt",
                "recovery_retry_count",
                # A disabled rule starts clean: a leftover offline backoff
                # would push the re-enabled rule's first alert into the
                # hourly ladder.
                "offline_retry_count",
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
            # ``summary_abandoned`` covers the case where the aggregation
            # window was given up: the alert was never delivered either, and
            # it must not wait for a cooldown that may never elapse.
            abandoned = bool(st.pop("summary_abandoned", False))
            cooldown_retry = (
                alerting
                and not bool(st.get("alerted"))
                and _safe_int(st.get("retry_count")) == 0
                and _safe_float(st.get("next_attempt")) == 0
                and (
                    abandoned
                    or (
                        _safe_float(st.get("last_sent")) > 0
                        and now - _safe_float(st.get("last_sent")) >= self.cooldown
                    )
                )
            )
            if (
                (not alerting and count >= self.sustain)
                or cooldown_retry
                # A cooldown-retry push that failed left retry_count>0 and the
                # episode latched; the outer ``cooldown_retry`` then went false
                # and the queued retry was never consumed, so the alert was
                # lost and the recovery notice withheld. Honour a pending
                # retry while the fault persists (same ladder as a fresh
                # episode, capped by the retry-exhausted branch below).
                or (
                    alerting
                    and not bool(st.get("alerted"))
                    and (
                        _safe_int(st.get("retry_count")) > 0
                        # A cooldown-retry push that failed while the
                        # connection was down only writes ``next_attempt``
                        # (v2.20.5 keeps the budget untouched then), leaving
                        # retry_count at 0. Without this the rule matched
                        # none of the three gates and stayed silent for the
                        # whole episode -- no alert, and the recovery notice
                        # was swallowed too.
                        or _safe_float(st.get("next_attempt")) > 0
                    )
                )
            ):
                # Remember when the fault started, even if every push fails:
                # the recovery notice used to report a duration measured from
                # the last retry (about one minute) instead of the whole
                # episode (v2.20.8).
                if not _safe_float(st.get("alert_since")):
                    st["alert_since"] = now
                # A queued retry must run when it is due even inside the
                # cooldown created by the previous alert. The branch below
                # records one event and returns, so a retry that already
                # failed once (retry_count/next_attempt set) used to be
                # swallowed on every sample: the alert stayed silent for the
                # whole cooldown (30 min by default), the event table grew one
                # duplicate row per sample, and a fault that cleared inside
                # the cooldown produced no recovery notice at all (2026-11-06
                # audit, reproduced with the production retry ladder).
                pending_retry = (
                    _safe_int(st.get("retry_count")) > 0
                    or _safe_float(st.get("next_attempt")) > 0
                )
                if (
                    last_sent
                    and now - last_sent < self.cooldown
                    and not abandoned
                    and not pending_retry
                ):
                    # A second episode inside the cooldown used to vanish
                    # completely. Latch it and write one event (with the
                    # suppression noted); only the duplicate push is skipped.
                    st["alerting"] = True
                    st["alerted"] = False
                    # Keep an existing episode start: a summary abandon resets
                    # ``last_sent`` and the rule then re-enters here, so
                    # overwriting ``alert_since`` shrank the reported duration
                    # to the retry window (v2.20.5).
                    if not _safe_float(st.get("alert_since")):
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
                    st.pop("recovery_offline_count", None)
                    if had_pending:
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                        # The queued recovery is folded into this alert, so
                        # it no longer waits for a summary. A stale marker
                        # made the next abandon callback return early and
                        # leave the rule marked "already notified" -- a
                        # permanent silence for the rest of the outage
                        # (v2.20.12).
                        st.pop("recovery_awaits_summary", None)
                    display_name = self._display_name(name)
                    text = (
                        f"⚠️ 系统告警 · {display_name}\n"
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
                    # Only a retry of an already-attempted episode bypasses
                    # aggregation; the first alert must still open the window
                    # (v2.20.8).
                    is_retry = cooldown_retry or _safe_int(st.get("retry_count")) > 0
                    reason_text = f"{value_text}（阈值 {threshold:.1f}{unit}）"
                    result = await self._push_alert(
                        display_name,
                        reason_text,
                        text,
                        retry=is_retry,
                        item_key=key,
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
                        # A fresh alert has reached the user, so a recovery
                        # must not be suppressed by a stale marker left over
                        # from a >24h sampling gap (the alert itself reset the
                        # episode's "was the user told?" state).
                        st.pop("stale", None)
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
                        # Identifies THIS rule as the buffered one: two rules
                        # may share a name (the same disk path twice, a
                        # renamed monitor), and the callbacks match by name --
                        # without the marker they also touched the rule whose
                        # alert was delivered on its own, which made the next
                        # fault of that rule alert a second time (v2.20.14,
                        # same fix as the service monitors).
                        st["summary_buffered"] = True
                        # The exact rule key this alert was buffered under.
                        # Two rules can share a name; the delivered summary
                        # must only clear the matching rule (v2.20.19).
                        st["summary_key"] = key
                        # The exact reason this alert was buffered. Two rules
                        # can share a name; matching the delivered summary by
                        # (name, reason) prevents one rule's summary from
                        # clearing the other rule's queued recovery (v2.20.19).
                        st["summary_reason"] = reason_text
                        self._record_event(
                            "resource",
                            name,
                            f"{value_text}（阈值 {threshold:.1f}{unit}）",
                            "alert",
                        )
                    elif delivered:
                        st["alerting"] = True
                        st["alerted"] = True
                        st.pop("stale", None)
                        st.pop("offline_retry_count", None)
                        if not _safe_float(st.get("alert_since")):
                            st["alert_since"] = now
                        st["count"] = 0
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        # The alert went out on its own; the buffered marker
                        # (if a previous episode ever buffered) must go or a
                        # later same-name abandon would withdraw this rule's
                        # "notified" state.
                        st.pop("summary_buffered", None)
                        st.pop("summary_key", None)
                        st.pop("summary_reason", None)
                        self._record_event(
                            "resource",
                            name,
                            f"{value_text}（阈值 {threshold:.1f}{unit}）",
                            "alert",
                        )
                    else:
                        if self._transport_offline() or delivery_offline(result):
                            # The bot is disconnected, so this failure says
                            # nothing about the alert: keep it queued without
                            # spending the three-attempt budget. The old code
                            # gave up during a long outage, and the recovery
                            # notice was the first the user ever heard of the
                            # fault (v2.20.5).
                            #
                            # The ladder is throttled after a few rounds
                            # (v2.20.24): a target the direct path keeps
                            # rejecting while the framework returns False was
                            # retried every 15 minutes forever, with no end
                            # and no disclosure.
                            offline_attempt = (
                                _safe_int(st.get("offline_retry_count")) + 1
                            )
                            st["offline_retry_count"] = offline_attempt
                            if offline_attempt == 1:
                                # Give the episode a start row in the event
                                # log: a value that cleared during the outage
                                # used to leave a recovery event with no
                                # matching beginning. The non-offline path
                                # writes one at the first failure (below).
                                self._record_event(
                                    "resource",
                                    name,
                                    f"{value_text}（阈值 {threshold:.1f}{unit}）",
                                    "failed",
                                )
                            st["next_attempt"] = now + offline_retry_delay(
                                offline_attempt
                            )
                            st["count"] = self.sustain
                            if offline_retry_delay(
                                offline_attempt
                            ) != offline_retry_delay(offline_attempt - 1):
                                logger.warning(
                                    "告警 %s 连续 %d 次因连接不可用未能送达，已改为每小时重试（恢复后仍会补发）",
                                    name,
                                    offline_attempt - 1,
                                )
                        else:
                            st.pop("offline_retry_count", None)
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
                                # The user will be told (or the recovery will
                                # say the alert never arrived); either way the
                                # stale-recovery suppression no longer applies.
                                st.pop("stale", None)
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
            # The offline backoff belongs to the episode that just ended.
            # Keeping it made the NEXT fault start at the slow hourly step:
            # the operator had just seen the recovery, and the new alert
            # waited an hour (v2.20.24 audit).
            st.pop("offline_retry_count", None)
            # ``summary_abandoned`` marks a summary that never reached the
            # chat. Keeping it made the next fault skip the cooldown gate
            # and push a duplicate alert (v2.20.12).
            st.pop("summary_abandoned", None)
            pending_text = st.get("pending_recovery")
            if pending_text and not self.recover_notify:
                # The operator turned recovery notices off after this one was
                # queued for retry; honouring the switch means dropping the
                # backlog instead of delivering it later anyway.
                st.pop("pending_recovery", None)
                st.pop("recovery_offline_count", None)
                st.pop("recovery_attempt", None)
                st.pop("recovery_next_attempt", None)
                st.pop("recovery_retry_count", None)
                pending_text = None
            if pending_text:
                next_recovery = _safe_float(st.get("recovery_next_attempt"))
                if st.get("recovery_awaits_summary") and self._aggregator is not None:
                    # The recovery was queued because its alert sat in a
                    # summary that has still not reached the chat. Posting
                    # the recovery now tells the user about a recovery for an
                    # outage they were never told about -- and the summary may
                    # still fail and be abandoned, in which case the recovery
                    # has to be withdrawn entirely.
                    #
                    # The notice was scheduled for "window end + 3s", but the
                    # summary is only *sent* after the window closes and it
                    # may still be in flight or waiting for its retry
                    # backoff, so the notice could overtake it (v2.20.14).
                    if self._aggregator.summary_pending("resource"):
                        next_recovery = now + 15.0
                        st["recovery_next_attempt"] = next_recovery
                    else:
                        # The summary is gone: delivered (the marker is
                        # cleared by the send path) or abandoned (the abandon
                        # callback drops the recovery). Either way the
                        # recovery may go out now.
                        st.pop("recovery_awaits_summary", None)
                if now >= next_recovery:
                    result = await self._send(str(pending_text))
                    if self._delivered(result):
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                        st.pop("recovery_offline_count", None)
                        # The recovery reached the chat, so it no longer
                        # depends on a pending summary. Leaving the marker
                        # behind made the NEXT abandon callback take the
                        # "drop the recovery" branch and return early, so
                        # ``alerted`` was never cleared and the rule stayed
                        # silent for the rest of the outage -- while the
                        # recovery notice still went out for an incident the
                        # user was never told about (v2.20.12).
                        st.pop("recovery_awaits_summary", None)
                    else:
                        if self._transport_offline() or delivery_offline(result):
                            # Keep the recovery notice queued across the
                            # outage instead of spending its retry budget:
                            # the user should learn about the fault when the
                            # channel is back (v2.20.5). Throttle the polling
                            # after a few rounds (v2.20.24) so a channel
                            # that never comes back is not retried every 15
                            # minutes forever.
                            st["recovery_retry_count"] = _safe_int(
                                st.get("recovery_retry_count")
                            )
                            offline_attempt = (
                                _safe_int(st.get("recovery_offline_count")) + 1
                            )
                            st["recovery_offline_count"] = offline_attempt
                            st["recovery_next_attempt"] = now + offline_retry_delay(
                                offline_attempt
                            )
                            if offline_retry_delay(
                                offline_attempt
                            ) != offline_retry_delay(offline_attempt - 1):
                                logger.warning(
                                    "%s 的恢复通知连续 %d 次因连接不可用未能送达，已改为每小时重试",
                                    name,
                                    offline_attempt - 1,
                                )
                        else:
                            st.pop("recovery_offline_count", None)
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
                                st.pop("recovery_offline_count", None)
                            else:
                                st["recovery_retry_count"] = retry_count
                                st["recovery_next_attempt"] = now + retry_delay(
                                    retry_count
                                )
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
                # The episode is over: the offline backoff belongs to it.
                # Keeping it made the next fault of this rule start at the
                # slow hourly step (v2.20.24 audit).
                st.pop("offline_retry_count", None)
                duration = now - _safe_float(st.get("alert_since"), now)
                st["alert_since"] = 0
                # Persist the closed episode *before* the slow send: a
                # reload/crash during delivery used to leave "alerting=True"
                # on disk, so the next sample replayed the whole recovery
                # (a second "已恢复" with the same duration). The offline
                # path already does this (v2.20.5); the resource/monitor
                # recovery branches did not (v2.20.21).
                self.state.save()
                # The event log is the source of truth for interruption
                # durations and the daily report's unresolved list, so it must
                # not depend on whether the recovery notice is enabled.
                self._record_event(
                    "resource",
                    name,
                    f"持续 {format_duration(duration)}",
                    "recovery",
                )
                # Read before clearing: only an alert that was ACTUALLY
                # buffered may hand its recovery to the summary. An older
                # version could leave ``awaiting_summary`` behind (a direct
                # send cleared only this marker), and a stale flag made the
                # recovery queue behind an unrelated same-name summary; when
                # that summary was abandoned the queued recovery was deleted
                # and the user never learned the outage had ended (v2.20.17).
                buffered = bool(st.pop("summary_buffered", None))
                # Consume the flag even when no notice is sent (recover_notify
                # off / stale / suppressed): leaving it behind made the next
                # unrelated recovery queue behind an already-closed window.
                awaiting = st.pop("awaiting_summary", False)
                if self.recover_notify and not stale and notified:
                    text = (
                        f"✅ 已恢复 · {self._display_name(name)}\n"
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
                    # ``window_remaining`` reaches zero when the window
                    # closes, but the summary may still be buffered, in
                    # flight, or waiting for its retry backoff. Posting the
                    # recovery in that gap beats the summary to the chat --
                    # the user sees a recovery for an outage they were never
                    # told about (v2.20.20; ``summary_pending`` is what the
                    # send path already waits on).
                    summary_pending = (
                        self._aggregator.summary_pending("resource")
                        if self._aggregator is not None
                        else False
                    )
                    if awaiting and buffered and (remaining > 0 or summary_pending):
                        # This rule's alert is still inside the aggregation
                        # window. Posting the recovery now would let it beat
                        # the summary to the chat (恢复先于告警到达，看起来像
                        # 两条不相关消息)；queue it instead so the summary is
                        # posted first. On abandon the queued notice still
                        # goes out -- the user then at least learns the
                        # episode happened.
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        # This recovery may only be posted if the summary that
                        # carries its alert actually reaches the chat. The
                        # abandon callback reads this marker to drop it.
                        st["recovery_awaits_summary"] = True
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
                    st.pop("summary_key", None)
                    st.pop("summary_reason", None)
                    result = await self._send(text)
                    if not self._delivered(result):
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + retry_delay(1)
                        # This notice does not depend on a buffered summary:
                        # an abandoned summary must not withdraw it.
                        st["recovery_awaits_summary"] = False
                        logger.warning("告警 %s 恢复通知未送达，已排队稍后补发", name)
            else:
                # The episode never latched (``alerting`` stayed False),
                # which means no alert ever reached the chat -- the push
                # failed every time. ``alert_since`` was still set when the
                # push was attempted, so the NEXT episode inherited this
                # stale start time and reported a duration measured from
                # the old outage ("告警持续 10 分钟" for a 0.1 s blip).
                # The latched branch above clears the marker itself (it
                # needs it to compute the duration first), so only the
                # never-latched case is handled here (v2.20.12).
                st["alert_since"] = 0

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
        if not self.offline_notify:
            # The operator turned the offline/recovery notices off, so the
            # broadcast must not resurrect them: it posted a "已恢复" to
            # every joined group even though no offline notice was ever
            # sent, and the audit did not point out the contradiction.
            logger.info("掉线广播已跳过：掉线/恢复通知开关处于关闭状态")
            return
        if not self.recover_notify:
            # The broadcast is just another delivery channel for the same
            # recovery notice: when "恢复时发送通知" is off, posting it to
            # every group contradicts the setting (v2.20.5).
            logger.info("掉线广播已跳过：恢复通知开关处于关闭状态")
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
        sent = failed = uncertain = considered = 0
        consecutive_failures = 0
        for group in groups:
            group_id = (
                str(group.get("group_id") or "") if isinstance(group, dict) else ""
            )
            if not group_id or group_id in exclude or group_id in notify_groups:
                continue
            if group_id in self._broadcast_done:
                # A flapping connection restarted the broadcast from the
                # first group after a cancel(), so groups that already saw
                # the notice got a second copy (v2.20.5).
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
                consecutive_failures += 1
                logger.debug("掉线广播群 %s 发送异常", group_id, exc_info=True)
                if consecutive_failures >= 5:
                    logger.warning(
                        "掉线广播：连续 %d 个群发送失败，疑似限流/风控，已中止本轮",
                        consecutive_failures,
                    )
                    break
                continue
            if self._delivered(result):
                sent += 1
                consecutive_failures = 0
                if len(self._broadcast_done) > 5000:
                    # Bound the in-memory set; it only needs to cover the
                    # current outage window.
                    self._broadcast_done.clear()
                self._broadcast_done.add(group_id)
            else:
                failed += 1
                if isinstance(result, DeliveryResult) and result.uncertain_targets:
                    uncertain += 1
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    logger.warning(
                        "掉线广播：连续 %d 个群发送失败，疑似限流/风控，已中止本轮",
                        consecutive_failures,
                    )
                    break
        logger.info(
            "掉线恢复广播完成：成功 %d / 未确认 %d / 失败 %d（扫描 %d 个群，排除 %d 个）",
            sent,
            uncertain,
            failed,
            len(groups),
            max(0, len(groups) - considered),
        )

    def _transport_offline(self) -> bool:
        """True when the bot connection is currently latched offline.

        Used by every retry ladder: a failed send while the channel is down
        proves nothing about the alert, so the finite retry budget must not
        be consumed. Without this, a long outage burned all retries and the
        recovery notice was the only message the user ever received
        (v2.20.5).
        """

        try:
            alerts_state = self.state.get("alerts", {}) or {}
            if not isinstance(alerts_state, dict):
                return False
            offline = alerts_state.get("offline")
            return bool(isinstance(offline, dict) and offline.get("alerting"))
        except Exception:
            return False

    async def _check_offline(self) -> None:
        # Without a self_id filter a multi-bot instance could probe another
        # bot's connection and report the wrong one as offline.
        self_id = str(cfg_get(self.config, "alerts.self_id", "") or "").strip()
        client = _get_aiocqhttp_client(
            self.plugin.context, self.platform_id, self_id or None
        )
        if client is None:
            # The client is missing for two very different reasons:
            #
            # 1. The bot has not connected yet. Right after a start-up the
            #    reverse WebSocket of NapCat is still coming up, so the
            #    first sample lands before the client exists. That is the
            #    normal case, and it says nothing about the configuration.
            # 2. The ``platform_id`` does not match an AstrBot platform
            #    instance. Then the disconnect detection and the OneBot
            #    direct sends silently no-op forever.
            #
            # The old code reported both as a warning about the
            # configuration, so every restart produced a scary "掉线告警
            # 与 OneBot 直发不可用" line even though the configuration was
            # perfectly fine and the bot connected a second later. The
            # first samples are now expected, and only a client that stays
            # missing is reported (v2.20.12).
            if self._started_at and time.time() - self._started_at < 120:
                # Still inside the start-up grace period: wait for the
                # connection instead of warning about it. The warning flag
                # stays untouched so a client that never shows up is still
                # reported once the grace period is over.
                return
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
        if not isinstance(alerts, dict):
            # A hand-edited state.json can hold a scalar/list here; the
            # section helper replaces it with a dict, keeping the sample loop
            # alive (every other section had this guard, this one and
            # provider_watch were missing it).
            with self.state.section("alerts") as alerts_live:
                alerts = alerts_live
        st = alerts.get("offline")
        if not isinstance(st, dict):
            st = {}
            alerts["offline"] = st
        now = time.time()
        # Record that the detector actually ran. ``_mark_stale_rules`` uses
        # this field to tell "alerts were disabled/stopped for a day" apart
        # from "the bot has been offline for a day". Without it a restart
        # during a long outage marked the episode stale and suppressed the
        # only "connection is back" notice the operator would get.
        st["last_seen"] = now
        count = _safe_int(st.get("count"))
        unreachable = _safe_int(st.get("unreachable"))
        # Read before any branch clears it: the recovery of a "cannot confirm
        # the online state" episode must close its event row (v2.20.24).
        unreachable_warned = bool(st.get("unreachable_warned"))

        if online:
            st["count"] = 0
            st["unreachable"] = 0
            st["unreachable_warned"] = False
            if unreachable_warned:
                # The status probe answers again: close the episode the
                # operator was warned about, otherwise the report keeps an
                # unclosed "Bot 在线状态" row forever (v2.20.24).
                self._record_event(
                    "connection",
                    "Bot 在线状态",
                    "状态查询恢复正常，确认 Bot 在线",
                    "recovery",
                )
                logger.info("Bot 在线状态已确认恢复")
            pending_text = st.get("pending_recovery") if self.offline_notify else None
            if pending_text and not self.recover_notify:
                # Recovery notices were turned off after this one was queued:
                # honour the switch and drop the backlog instead of delivering
                # it on the next online sample (resource/provider paths do the
                # same).
                for key in (
                    "pending_recovery",
                    "recovery_offline_count",
                    "recovery_attempt",
                    "recovery_next_attempt",
                    "recovery_retry_count",
                ):
                    st.pop(key, None)
                pending_text = None
            if not self.offline_notify:
                for key in (
                    "pending_recovery",
                    "recovery_offline_count",
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
                        st.pop("recovery_offline_count", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                        logger.info("Bot 连接恢复通知补发成功")
                    else:
                        # Keep the baseline behaviour: this branch spends the
                        # retry budget and gives up after five attempts.
                        # It runs only while the status probe says "online",
                        # so a delivery that still reports an unreachable
                        # transport is a half-open connection -- and a notice
                        # about a past outage goes stale. Queuing it forever
                        # (the rule used for a genuinely offline bot) would
                        # leave ``pending_recovery`` in the state file with
                        # no end (v2.20.24 audit).
                        retry_count = _safe_int(st.get("recovery_retry_count")) + 1
                        if retry_count > MAX_RECOVERY_RETRIES:
                            logger.warning(
                                "Bot 连接恢复通知连续 %d 次未送达，放弃补发",
                                retry_count - 1,
                            )
                            for stale_key in (
                                "pending_recovery",
                                "recovery_offline_count",
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
                # ``since`` may be missing or 0 in a hand-edited/old state;
                # ``_safe_float(value, now)`` would accept 0 and report an
                # outage of ~20000 days.
                duration = now - (_safe_float(st.get("since")) or now)
                queued_raw = st.pop("pending_alert_text", None)
                # A stale episode is one where the outage window is too old
                # to push anything; delivering the hours-old alert text now
                # -- without any context -- contradicts that. Drop it (the
                # event log has the full story), same as the recovery notice.
                queued = queued_raw if (self.offline_notify and not stale) else None
                if queued:
                    # The queued text was written while offline and ends with
                    # "若本条未能送达，将在连接恢复后补发。" Sending that
                    # sentence *as* the补发 message read like a contradiction,
                    # so rewrite it in the delivered copy (v2.20.5).
                    queued = str(queued).replace(
                        "若本条未能送达，将在连接恢复后补发。",
                        "（本条为补发：离线期间无法送达）",
                    )
                st["alerting"] = False
                st["since"] = 0
                # Whether this episode ever reached the user. Older state has
                # no marker: keep the old behaviour (assume it did) so a real
                # outage never loses its recovery notice.
                notified = bool(st.pop("alerted", True))
                parts: list[str] = []
                if queued:
                    parts.append(str(queued))
                if (
                    self.offline_notify
                    and self.recover_notify
                    and not stale
                    and (notified or queued)
                ):
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
                # Persist the closed episode *before* the slow send/broadcast:
                # a reload or crash during the 20-60 s delivery used to leave
                # "alerting=True" on disk, so the next start replayed the whole
                # recovery (a second notice, a second 17-group broadcast and a
                # second event) with a wrong duration (v2.20.5).
                self.state.save()
                # The event log does not depend on the notification pref.
                self._record_event(
                    "offline",
                    "Bot 连接",
                    f"离线时长 {format_duration(duration)}",
                    "recovery",
                )
                # Reports that failed only because the bot was offline are
                # queued instead of being given up; retry the newest one now
                # that the channel is back (v2.20.5).
                try:
                    report = getattr(self.plugin, "daily_report", None)
                    if report is not None and hasattr(
                        report, "notify_offline_recovery"
                    ):
                        report.notify_offline_recovery()
                except Exception:
                    logger.debug("安排日报补发失败", exc_info=True)
                # Alerts that failed only because the channel was down are
                # queued with a 15-minute backoff. Waiting it out after the
                # channel came back made the operator see "Bot 已恢复" first
                # and the actual outage alert much later (v2.20.21).
                self._nudge_queued_alerts()
                if not stale and (notified or queued):
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
            if unreachable + 1 >= UNREACHABLE_ALERT_SAMPLES and not st.get(
                "unreachable_warned"
            ):
                st["unreachable_warned"] = True
                logger.warning(
                    "OneBot 状态查询连续 %d 次失败，暂时无法确认 Bot 在线状态",
                    unreachable + 1,
                )
                # An unknown state is not a confirmed outage, but it is a real
                # incident for the operator: record it so the daily report can
                # show the window instead of pretending everything was fine.
                # The recovery branch closes it (v2.20.24).
                self._record_event(
                    "connection",
                    "Bot 在线状态",
                    f"连续 {unreachable + 1} 次状态查询失败，无法确认在线状态",
                    "alert",
                )
                if self.offline_notify:
                    # Silence was the old behaviour: a half-open connection
                    # blocked every alert for hours while the log showed a
                    # single warning (v2.20.5).
                    minutes = max(1, round((unreachable + 1) * self.interval / 60))
                    text = (
                        "⚠️ 无法确认 Bot 在线状态\n"
                        "──────────────\n"
                        f"主机: {self._hostname()}\n"
                        f"连续 {unreachable + 1} 次状态查询失败（约 {minutes} 分钟）\n"
                        f"时间: {_now_text(self.config)}\n"
                        "连接可能已断开但未正常关闭，请检查 NapCat / QQ 登录状态。"
                    )
                    try:
                        await self._send(text, kind="offline")
                    except Exception:
                        logger.debug("无法确认在线状态的通知发送失败", exc_info=True)
            return

        st["unreachable"] = 0
        st["unreachable_warned"] = False
        if unreachable_warned:
            # The probe now confirms the bot is offline; the offline alert
            # below owns the episode. Close the "cannot confirm" row so the
            # report does not keep an unclosed warning (v2.20.24).
            self._record_event(
                "connection",
                "Bot 在线状态",
                "已确认离线，转入离线告警",
                "recovery",
            )
            logger.info("Bot 在线状态：已确认离线，转入离线告警")
        if not st.get("alerting"):
            # Once latched the counter is not used and grew without bound for
            # multi-day outages (state.json bloat).
            count += 1
            st["count"] = count
        if not st.get("alerting") and count >= OFFLINE_ALERT_SAMPLES:
            st["alerting"] = True
            # Backdate by one sampling interval: the first offline sample can
            # be up to ``interval`` seconds after the real disconnect, and the
            # old ``since`` made the reported outage short by that much.
            st["since"] = max(0.0, now - max(0, self.interval))
            st["count"] = 0
            # A new outage starts a new broadcast round. The dedupe set used
            # to live for the whole process, so only the first outage ever
            # reached the groups; every later recovery silently skipped all
            # of them (v2.20.8).
            self._broadcast_done.clear()
            last_sent = _safe_float(st.get("last_sent"))
            in_cooldown = bool(last_sent and now - last_sent < self.cooldown)
            st["alerted"] = False
            if not self.offline_notify or in_cooldown:
                # Keep the incident in the event log; only the push is off.
                # A flapping link (NapCat reconnecting every few minutes)
                # used to push a "离线 + 已恢复" pair every round; repeated
                # outages inside the cooldown are recorded but silent, and
                # the recovery notice/broadcast is skipped too, so the user
                # never sees a recovery for an alert they did not receive.
                self._record_event(
                    "offline",
                    "Bot 连接",
                    "连接离线（通知冷却中）" if in_cooldown else "连接离线",
                    "alert",
                )
                if in_cooldown:
                    logger.info(
                        "Bot 连接离线（%d 秒内已通知过，本次只记事件）",
                        self.cooldown,
                    )
                return
            had_pending_recovery = bool(st.pop("pending_recovery", None))
            st.pop("recovery_offline_count", None)
            if had_pending_recovery:
                # Fold the previous episode's undelivered recovery into this
                # alert, exactly like the resource rules. Keeping it queued
                # and retrying it on the next online sample produced TWO
                # "已恢复" messages in one sample: the old episode's notice
                # followed by the new one (v2.20.20).
                for stale_key in (
                    "recovery_attempt",
                    "recovery_next_attempt",
                    "recovery_retry_count",
                ):
                    st.pop(stale_key, None)
            text = (
                "🔴 Bot 连接离线\n"
                "──────────────\n"
                f"主机: {self._hostname()}\n"
                # ``since`` is backdated by one sampling interval to estimate
                # the real disconnect, and the recovery card already calls it
                # "掉线时间". The alert card used to print only the detection
                # moment under "时间", so the two cards for one outage showed
                # timestamps 30 s apart and looked inconsistent (2026-11-06
                # user screenshot review).
                f"掉线时间: {_fmt_ts(_safe_float(st.get('since')) or now, self.config)}\n"
                f"检测时间: {_now_text(self.config)}\n"
                "若本条未能送达，将在连接恢复后补发。"
            )
            if had_pending_recovery:
                text += "\n（上一次的恢复通知未送达，已并入本条）"
            result = await self._send(text, kind="offline")
            delivered = self._delivered(result)
            st["pending_alert"] = not delivered
            if delivered:
                st.pop("pending_alert_text", None)
                st["alerted"] = True
                st["last_sent"] = now
                logger.info("Bot 连接离线告警已发送")
                self._record_event("offline", "Bot 连接", "连接离线", "alert")
            else:
                st["pending_alert_text"] = text
                logger.warning("Bot 连接离线告警发送失败，已排队，恢复后补发")
                self._record_event(
                    "offline", "Bot 连接", "连接离线（推送失败）", "failed"
                )

        # A sustained outage that started inside the notification cooldown
        # used to stay silent for its whole duration: the latch branch above
        # returns on the first sample, and later samples never re-evaluate.
        # Mirror the resource/monitor ``cooldown_retry``: once the cooldown
        # has elapsed and the bot is still offline, push the alert (marked as
        # a deferred copy) so a multi-hour outage is not invisible.
        if (
            st.get("alerting")
            and not st.get("alerted")
            and not st.get("pending_alert_text")
            and self.offline_notify
            and not st.get("stale")
        ):
            last_sent = _safe_float(st.get("last_sent"))
            if last_sent and now - last_sent >= self.cooldown:
                text = (
                    "🔴 Bot 连接离线\n"
                    "──────────────\n"
                    f"主机: {self._hostname()}\n"
                    f"掉线时间: {_fmt_ts(_safe_float(st.get('since')) or now, self.config)}\n"
                    f"检测时间: {_now_text(self.config)}\n"
                    "（本条为通知冷却结束后的补发）"
                )
                result = await self._send(text, kind="offline")
                if self._delivered(result):
                    st["alerted"] = True
                    st["last_sent"] = now
                    st["pending_alert"] = False
                    logger.info("Bot 连接离线告警已在冷却结束后补发")
                    self._record_event("offline", "Bot 连接", "连接离线", "alert")
                else:
                    st["pending_alert_text"] = text
                    st["pending_alert"] = True
                    logger.warning("Bot 连接离线告警补发失败，已排队，恢复后补发")
                    self._record_event(
                        "offline", "Bot 连接", "连接离线（推送失败）", "failed"
                    )

    def _nudge_queued_alerts(self) -> None:
        """Make alerts queued during an outage retry on the next sample.

        A push that failed because the channel was offline keeps the alert
        and backs off 15 minutes. Waiting that long after the channel came
        back made the operator see "Bot 已恢复" first and the real alert
        much later (v2.20.21). A recovery notice queued behind the same
        backoff is released too.
        """

        now = time.time()
        with self.state.section("alerts") as alerts:
            rules = alerts.get("rules")
            if isinstance(rules, dict):
                for st in rules.values():
                    if not isinstance(st, dict):
                        continue
                    if _safe_float(st.get("next_attempt")) > now:
                        st["next_attempt"] = 0
                    if (
                        st.get("pending_recovery")
                        and _safe_float(st.get("recovery_next_attempt")) > now
                    ):
                        st["recovery_next_attempt"] = 0
            watch = alerts.get("provider_watch")
            if isinstance(watch, dict):
                if _safe_float(watch.get("next_attempt")) > now:
                    watch["next_attempt"] = 0
                if (
                    watch.get("pending_recovery")
                    and _safe_float(watch.get("recovery_next_attempt")) > now
                ):
                    watch["recovery_next_attempt"] = 0
        monitor = getattr(self.plugin, "monitor", None)
        nudge = getattr(monitor, "nudge_queued", None)
        if callable(nudge):
            try:
                nudge()
            except Exception:
                logger.debug("催促监测项排队告警失败", exc_info=True)

    def _close_provider_watch(self, reason: str) -> None:
        """Resolve a latched LLM alert when the rule stops running.

        Turning ``provider_watch`` off (or setting its threshold to 0) used to
        leave ``alerting=True`` behind: the daily report kept showing an
        "unresolved" LLM incident no later event could close.
        """

        alerts_state = self.state.get("alerts")
        if not isinstance(alerts_state, dict):
            return
        watch = alerts_state.get("provider_watch")
        if not isinstance(watch, dict):
            return
        # Drop a queued recovery even when the rule is no longer alerting:
        # pending_recovery survives in exactly that state, and re-enabling the
        # watch used to deliver a stale "已恢复" from the previous episode.
        # Track whether anything was removed: these pops used to be returned
        # from without a save(), so a shutdown right after closing the rule
        # resurrected them and re-enabling the watch replayed an old notice
        # (2026-11-06 audit).
        changed = False
        for key in (
            "pending_recovery",
            "recovery_offline_count",
            "recovery_retry_count",
            "recovery_next_attempt",
            "pending_recovery_is_alert",
            "send_failed",
            "pending_alert_text",
            "next_attempt",
            "alert_retry_count",
            # Re-enabling the watch starts a fresh episode: a leftover offline
            # backoff would push its first alert into the hourly ladder.
            "offline_retry_count",
        ):
            if key in watch:
                watch.pop(key, None)
                changed = True
        if not watch.get("alerting"):
            if changed:
                self.state.save()
            return
        watch["alerting"] = False
        watch["alerted"] = False
        watch["alert_since"] = 0
        self.state.save()
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

        alerts_state = self.state.get("alerts")
        if not isinstance(alerts_state, dict):
            # A hand-edited state.json can hold a list/scalar here. Calling
            # .get on it raised inside the sampling loop, which then aborted
            # before _prune_rules() and state.save(): every sample warned and
            # nothing (including last_seen) reached the disk (2026-11-06
            # audit).
            alerts_state = {}
            self.state.set("alerts", alerts_state)
        # get (not setdefault): the live nested dict is required, otherwise
        # the alerting latch below would mutate a detached local dict.
        watch = alerts_state.get("provider_watch")
        if not isinstance(watch, dict):
            watch = {}
            alerts_state["provider_watch"] = watch
        alerting = bool(watch.get("alerting"))
        last_alert_ts = _safe_float(watch.get("last_alert_ts"))

        # An alert push that failed while the channel was down is retried here
        # instead of waiting for the recovery notice (v2.20.5).
        queued_alert = str(watch.get("pending_alert_text") or "")
        if (
            queued_alert
            and alerting
            and streak >= self.provider_watch_threshold
            and now >= _safe_float(watch.get("next_attempt"))
        ):
            result = await self._send(queued_alert, source="provider", kind="provider")
            if self._delivered(result):
                watch.pop("pending_alert_text", None)
                watch.pop("next_attempt", None)
                watch.pop("alert_retry_count", None)
                watch.pop("offline_retry_count", None)
                watch["alerted"] = True
                watch["send_failed"] = False
                self._record_event(
                    "provider",
                    "LLM 服务",
                    f"连续 {streak} 次 LLM 调用失败",
                    "alert",
                )
                logger.warning("LLM 服务连续失败 %d 次，补发告警成功", streak)
            elif self._transport_offline() or delivery_offline(result):
                # Throttle the polling after a few rounds (v2.20.24): the
                # queued alert is still delivered once the channel returns,
                # but a channel that never returns is not retried every 15
                # minutes forever.
                offline_attempt = _safe_int(watch.get("offline_retry_count")) + 1
                watch["offline_retry_count"] = offline_attempt
                watch["next_attempt"] = now + offline_retry_delay(offline_attempt)
                if offline_retry_delay(offline_attempt) != offline_retry_delay(
                    offline_attempt - 1
                ):
                    logger.warning(
                        "LLM 服务告警连续 %d 次因连接不可用未能送达，已改为每小时重试",
                        offline_attempt - 1,
                    )
            else:
                watch.pop("offline_retry_count", None)
                attempts = _safe_int(watch.get("alert_retry_count")) + 1
                watch["alert_retry_count"] = attempts
                if attempts >= 3:
                    watch.pop("pending_alert_text", None)
                    watch.pop("next_attempt", None)
                    watch["alerted"] = True
                    watch["send_failed"] = True
                    logger.warning(
                        "LLM 服务告警连续发送失败 3 次，停止重试（恢复时说明）"
                    )
                else:
                    watch["next_attempt"] = now + retry_delay(attempts)
            self.state.save()

        # Retries for a recovery notice whose send failed earlier.
        pending_text = str(watch.get("pending_recovery") or "")
        if (
            pending_text
            and not self.recover_notify
            and not watch.get("pending_recovery_is_alert")
        ):
            # The operator turned recovery notices off after this one was
            # queued; drop the backlog like the resource/monitor rules do
            # instead of delivering it later anyway (v2.20.0). An alert-only
            # backlog is NOT a recovery notice and must still be delivered
            # (v2.20.22).
            for stale_key in (
                "pending_recovery",
                "recovery_offline_count",
                "recovery_retry_count",
                "recovery_next_attempt",
                "pending_recovery_is_alert",
            ):
                watch.pop(stale_key, None)
            self.state.save()
            pending_text = ""
        if pending_text and latest == "completed":
            # Retry the queued recovery only while the service is actually
            # healthy. A new failure means the notice is stale; sending it
            # here raced the alert branch below and delivered "✅ 已恢复"
            # during an outage (v2.20.19).
            next_attempt = _safe_float(watch.get("recovery_next_attempt"))
            if now >= next_attempt:
                result = await self._send(
                    pending_text, source="provider", kind="provider"
                )
                if self._delivered(result):
                    watch.pop("pending_recovery", None)
                    watch.pop("recovery_retry_count", None)
                    watch.pop("recovery_next_attempt", None)
                    watch.pop("recovery_offline_count", None)
                    watch.pop("pending_recovery_is_alert", None)
                    self.state.save()
                    logger.info("LLM 服务恢复通知补发成功")
                else:
                    if self._transport_offline() or delivery_offline(result):
                        # The channel is down, so this failure says nothing
                        # about the notice: keep it queued without spending
                        # the retry budget, exactly like the resource rules
                        # do. Burning the five attempts during the outage
                        # gave up permanently and the recovery notice never
                        # reached the chat.
                        offline_attempt = (
                            _safe_int(watch.get("recovery_offline_count")) + 1
                        )
                        watch["recovery_offline_count"] = offline_attempt
                        watch["recovery_next_attempt"] = now + offline_retry_delay(
                            offline_attempt
                        )
                        self.state.save()
                        if offline_retry_delay(offline_attempt) != offline_retry_delay(
                            offline_attempt - 1
                        ):
                            logger.warning(
                                "LLM 服务恢复通知连续 %d 次因连接不可用未能送达，已改为每小时重试",
                                offline_attempt - 1,
                            )
                    else:
                        # ``int()`` on a hand-edited state value raised on every
                        # sample, which skipped the rest of the sample (including
                        # the state save) until the field was rewritten.
                        watch.pop("recovery_offline_count", None)
                        retry_count = (
                            _safe_int(watch.get("recovery_retry_count")) or 1
                        ) + 1
                        if retry_count > MAX_RECOVERY_RETRIES:
                            watch.pop("pending_recovery", None)
                            watch.pop("recovery_offline_count", None)
                            watch.pop("recovery_retry_count", None)
                            watch.pop("recovery_next_attempt", None)
                            watch.pop("pending_recovery_is_alert", None)
                            self.state.save()
                            logger.warning(
                                "LLM 服务恢复通知连续发送失败 %d 次，放弃补发",
                                retry_count - 1,
                            )
                        else:
                            watch["recovery_retry_count"] = retry_count
                            watch["recovery_next_attempt"] = now + retry_delay(
                                retry_count
                            )
                            self.state.save()
            # Fall through to the streak check: a new LLM failure arriving
            # while the old recovery notice was being retried used to be
            # silent for up to ~30 minutes (v2.20.0).

        # The alert branch must run for a *latched* episode too: a streak
        # that began inside the cooldown was recorded but never pushed, and
        # without this the rule stayed silent for the whole outage (and its
        # recovery notice was withheld as well, because ``alerted`` stayed
        # False). The previous version computed ``cooldown_retry`` inside the
        # ``not alerting`` guard, where it was always False -- dead code.
        cooldown_retry = (
            alerting
            and not bool(watch.get("alerted"))
            and _safe_float(watch.get("last_alert_ts")) > 0
            and now - _safe_float(watch.get("last_alert_ts"))
            >= self.provider_watch_cooldown
        )
        if (not alerting and streak >= self.provider_watch_threshold) or (
            cooldown_retry and streak >= self.provider_watch_threshold
        ):
            in_cooldown = bool(
                last_alert_ts and now - last_alert_ts < self.provider_watch_cooldown
            )
            if in_cooldown and not cooldown_retry:
                # Latch and record without pushing. ``alerted=False`` keeps
                # the recovery notice from arriving without a matching alert
                # -- the exact defect v2.18.1 fixed for the resource rules.
                # Keep any queued recovery from the previous episode: it is
                # still owed to the user and will be retried once the service
                # is healthy again; dropping it here left the previous outage
                # permanently "unrecovered" (v2.20.19 reverse audit).
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
            # A queued recovery from the PREVIOUS episode must not be
            # delivered while this new alert is being pushed: fold it into
            # the alert text. If the alert is cooldown-suppressed, the
            # branch above returns first and keeps the notice for a later
            # healthy retry (v2.20.19).
            had_pending_recovery = bool(watch.pop("pending_recovery", None))
            if had_pending_recovery:
                watch.pop("recovery_retry_count", None)
                watch.pop("recovery_next_attempt", None)
                watch.pop("pending_recovery_is_alert", None)
                # Same as the resource rules: the folded notice is no longer
                # its own retry object, so its offline ladder must not leak
                # into the next episode (v2.20.25 reverse audit).
                watch.pop("recovery_offline_count", None)
            text = (
                "⚠️ LLM 服务连续失败\n"
                "──────────────\n"
                f"主机: {self._hostname()}\n"
                f"连续失败: {streak} 次（阈值 {self.provider_watch_threshold}）\n"
                f"最近状态: {_PROVIDER_STATUS_TEXT.get(str(latest or ''), latest or '未知')}\n"
                f"时间: {_now_text(self.config)}"
                + (
                    "\n（上一次的恢复通知未送达，已并入本条）"
                    if had_pending_recovery
                    else ""
                )
            )
            result = await self._send(text, source="provider", kind="provider")
            delivered = self._delivered(result)
            watch["alerting"] = True
            # A failed push still owes the user an explanation, which the
            # recovery notice carries ("此前未能送达"); only cooldown
            # suppression is silent.
            #
            # The provider watch has no retry ladder of its own, so this is
            # the "retries exhausted" state of the resource rules: the
            # recovery notice is the last signal the user gets. The offline
            # branch below is the one that keeps ``alerted`` False, because
            # there the alert is queued and delivered later (v2.20.14).
            watch["alerted"] = True
            watch["send_failed"] = not delivered
            watch["last_alert_ts"] = now
            watch["last_streak"] = streak
            self.state.save()
            if delivered:
                # This fresh alert supersedes any copy queued by an earlier
                # failed push: leaving it behind delivered the same outage a
                # second time (the retry loop and the cooldown deadline can
                # land in different orders). Clear it only on delivery, so a
                # failed fresh attempt can still fall back to the queued copy.
                watch.pop("pending_alert_text", None)
                watch.pop("next_attempt", None)
                watch.pop("alert_retry_count", None)
                # The channel works again: the next failure must start at the
                # fast step of the backoff (v2.20.24).
                watch.pop("offline_retry_count", None)
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
                if self._transport_offline() or delivery_offline(result):
                    # Keep the alert queued instead of giving up: the channel
                    # is down, so this failure says nothing about the LLM
                    # outage. ``alerted`` stays False so the recovery notice
                    # keeps the "此前未能送达" note and the retry below can
                    # deliver the alert itself (v2.20.5). The retry ladder is
                    # throttled after a few rounds (v2.20.24).
                    watch["alerted"] = False
                    watch["pending_alert_text"] = text
                    offline_attempt = _safe_int(watch.get("offline_retry_count")) + 1
                    watch["offline_retry_count"] = offline_attempt
                    watch["next_attempt"] = now + offline_retry_delay(offline_attempt)
                    if offline_retry_delay(offline_attempt) != offline_retry_delay(
                        offline_attempt - 1
                    ):
                        logger.warning(
                            "LLM 服务告警连续 %d 次因连接不可用未能送达，已改为每小时重试",
                            offline_attempt - 1,
                        )
                    self.state.save()
                    logger.warning(
                        "LLM 服务告警发送失败（Bot 连接不可用），已排队，恢复后补发"
                    )
                else:
                    watch.pop("offline_retry_count", None)
                    logger.warning("LLM 服务告警发送失败，恢复时将一并通知")
            return

        if alerting and latest == "completed":
            watch["alerting"] = False
            # A queued alert from this episode must not survive it: the retry
            # block would re-send the stale text (with the old failure count)
            # during the *next* outage and the user got two alerts for one
            # incident (v2.20.8). Read it first: when the queue was never
            # delivered, the recovery below carries it so the outage is not
            # invisible.
            queued_alert = str(watch.pop("pending_alert_text", None) or "")
            for stale_key in (
                "next_attempt",
                "alert_retry_count",
                # The offline backoff belongs to the episode that just ended,
                # same as the resource rules clear theirs in the recovery
                # branch. Keeping it made the next LLM outage start at the
                # slow hourly step (v2.20.24 covered only the resource side).
                "offline_retry_count",
            ):
                watch.pop(stale_key, None)
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
            # The queued alert and the recovery notice are two different
            # messages. ``recover_notify`` only controls the notice: when it
            # is off, an alert that never left the queue must still be sent,
            # otherwise the whole LLM outage is invisible (v2.20.22).
            send_queued_alert = bool(queued_alert and not notified)
            send_recovery = bool(self.recover_notify and (notified or queued_alert))
            if send_queued_alert or send_recovery:
                last_streak = _safe_int(watch.get("last_streak"))
                had_failed = bool(watch.pop("send_failed", False)) or bool(queued_alert)
                recovery_text = ""
                if send_recovery:
                    recovery_text = (
                        "✅ 已恢复 · LLM 服务\n"
                        "──────────────\n"
                        f"主机: {self._hostname()}\n"
                        + (f"此前连续失败: {last_streak} 次\n" if last_streak else "")
                        + "最近状态: 调用成功\n"
                        + ("（异常告警此前未能送达）\n" if had_failed else "")
                        + f"时间: {_now_text(self.config)}"
                    )
                if send_queued_alert:
                    # The alert never left the queue: the channel was down
                    # when the LLM failed and the service recovered before it
                    # came back. Deliver it now; append the recovery only
                    # when the operator still wants recovery notices.
                    text = queued_alert
                    if recovery_text:
                        text += "\n\n" + recovery_text
                else:
                    text = recovery_text
                result = await self._send(text, source="provider", kind="provider")
                if not self._delivered(result):
                    # Keep the text for the retry path. An alert-only retry
                    # must survive ``recover_notify=false`` (the switch is for
                    # recovery notices, not for the outage alert itself).
                    watch["pending_recovery"] = text
                    watch["recovery_retry_count"] = 1
                    watch["recovery_next_attempt"] = now + retry_delay(1)
                    if send_queued_alert and not send_recovery:
                        watch["pending_recovery_is_alert"] = True
                    else:
                        watch.pop("pending_recovery_is_alert", None)
                    self.state.save()
                    logger.warning("LLM 服务恢复通知发送失败，稍后补发")
                else:
                    watch.pop("pending_recovery_is_alert", None)
                    # A queued notice from an earlier episode must not fire a
                    # second "已恢复" after this one was delivered.
                    for stale_key in (
                        "pending_recovery",
                        "recovery_offline_count",
                        "recovery_retry_count",
                        "recovery_next_attempt",
                    ):
                        watch.pop(stale_key, None)
            logger.info("LLM 服务已恢复（最近一次调用成功）")

        if pending_text and latest != "completed":
            # Keep a still-recent recovery queued: when the service is
            # failing again (or the newest call was aborted), the notice is
            # only valid once a completed call proves recovery. Do not drop
            # it immediately -- the previous outage is still owed a
            # "recovered" message (v2.20.19 reverse audit). A notice that
            # waits more than a day without ever seeing a completed call is
            # stale residue; drop it then.
            overdue = now - _safe_float(watch.get("recovery_next_attempt"))
            if overdue > 24 * 3600:
                for stale_key in (
                    "pending_recovery",
                    "recovery_offline_count",
                    "recovery_retry_count",
                    "recovery_next_attempt",
                    "pending_recovery_is_alert",
                ):
                    watch.pop(stale_key, None)
                self.state.save()
                logger.info("LLM 服务恢复通知等待超过 24 小时，已丢弃过期通知")

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

    def _display_name(self, name: str) -> str:
        """Mask a rule name that is itself an address or URL.

        The resource rules announced the disk path as-is, so with the
        hostname rule on a NAS mount ("//192.168.1.5/share") reached the
        alert, the recovery notice and the aggregation summary unchanged.
        The card, dashboard and report already used ``mask_name`` (v2.20.20).
        """

        try:
            return Redactor(read_rules(self.config, "display.redact")).mask_name(name)
        except Exception:
            return str(name or "")

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
                today_text = now.strftime("%Y-%m-%d")
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                pending = self._daily_pending()
                if isinstance(pending, dict) and pending.get("day") == today_text:
                    # The scheduled send failed because the channel was down.
                    # Retry after the backoff instead of waiting for tomorrow:
                    # "attempted today" used to drop the whole day silently
                    # (v2.20.8).
                    next_ts = _safe_float(pending.get("next"))
                    target = (
                        datetime.fromtimestamp(next_ts, tz)
                        if next_ts > now.timestamp()
                        else now
                    )
                elif target <= now:
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
            # A leftover pending record for a day that already went out
            # (sending marks the day and clears the record in two separate
            # saves, so a crash between them -- or a .bak restore -- can
            # revive it) made the loop schedule the same day every second:
            # ~3600 log lines an hour until the six-hour expiry (v2.20.20).
            self._clear_daily_pending()
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
                    self._clear_daily_pending()
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
            self._clear_daily_pending()
            logger.info("每日定时状态图已发送（文字）：%s", targets)
            return True
        logger.warning("每日定时状态图发送失败：%s", targets)
        if delivery_offline(result) or self._transport_offline():
            # The channel is unreachable, not the content: queue today's
            # image and retry after the backoff / on reconnect instead of
            # burning the four attempts inside the outage.
            self._set_daily_pending(today, time.time() + 900)
            logger.warning(
                "每日定时状态图发送失败（Bot 连接不可用），已排队，恢复后补发"
            )
        else:
            # A content failure (the T2I renderer timed out, the target
            # format is wrong) has no "the channel is back" hook, so the
            # pending record was only written for outages. A restart before
            # the four in-loop retries ran out therefore lost the whole
            # day: the attempt marker was already set, so the catch-up
            # guard refused to send it again (v2.20.12).
            #
            # The pending record is what the loop consults, so queue it for
            # every failure. It expires after six hours, so a day-old
            # backlog never resends a stale image.
            self._set_daily_pending(today, time.time() + 600)
            logger.warning(
                "每日定时状态图发送失败，已排队，%d 分钟后重试",
                10,
            )
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
            # Register the body as in-flight before the await so two identical
            # requests arriving while one send is still running cannot both
            # pass the dedup check and push the same notice twice (the dedup
            # window only covered a serial retry). A failed send is removed so
            # it stays retryable; a successful send keeps the 5-second window.
            if body_key in self._webhook_inflight:
                logger.info("Komari Webhook 重复请求（同一内容正在处理中），已忽略")
                return web.json_response({"status": "ok", "duplicate": True})
            self._webhook_inflight.add(body_key)
            try:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    data = {"message": raw.decode("utf-8", errors="replace")[:4000]}
                text = self._format_komari(
                    data, Redactor(read_rules(self.config, "display.redact"))
                )
                result = await self._send(text, source="webhook", kind="service")
                if not self._delivered(result):
                    return web.json_response(
                        {"status": "error", "message": "delivery failed"},
                        status=503,
                    )
                # Only remember a body that really reached the chat; a failed
                # send must stay retryable.
                self._webhook_last = (body_key, time.time())
                return web.json_response({"status": "ok"})
            finally:
                # An exception in _format_komari/_send used to leave the key
                # in the in-flight set forever: every later retry of the same
                # body answered {"duplicate": true} without sending anything
                # (v2.20.20).
                self._webhook_inflight.discard(body_key)

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
    def _format_komari(data: Any, redactor: Redactor | None = None) -> str:
        redactor = redactor or Redactor()
        if not isinstance(data, dict):
            return truncate_text(
                "🔔 Komari 监控通知\n──────────────\n"
                + redactor.mask_address_tokens(
                    json.dumps(data, ensure_ascii=False, indent=2)[:4000]
                ),
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
            # ``message`` and the fallback ``event`` field were masked while
            # the title was forwarded verbatim, so a node named
            # "10.1.2.3:8080" leaked the internal address with the hostname
            # rule on (the same leak was fixed for monitor names and reports).
            lines.append(f"事件: {redactor.mask_address_tokens(title)}")
        if message:
            message = redactor.mask_address_tokens(message)
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
                f"{label}: "
                + str(
                    redactor.mask_name(data[key])
                    if key == "node_name"
                    else redactor.mask_address_tokens(data[key])
                )
                for label, key in (
                    ("事件", "event"),
                    ("级别", "level"),
                    ("节点", "node_name"),
                    ("时间", "time"),
                )
                # "事件" was already printed from ``title`` (which falls back
                # to ``event``), so the same line appeared twice (v2.20.8).
                if data.get(key) and not (key == "event" and title)
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
