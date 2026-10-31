from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
import psutil
from astrbot.api import logger

from .aggregator import AlertAggregator
from .collectors import (
    cfg_bool,
    cfg_get,
    cfg_int,
    format_duration,
    now_in_timezone,
    resolve_targets,
)
from .delivery import DeliveryResult
from .state import StateStore
from .utils import (
    clamp_config,
    normalize_targets,
    redact_url,
    retry_delay,
    safe_float,
    safe_int,
)

KIND_LABELS = {
    "http": "HTTP",
    "tcp": "TCP",
    "docker": "Docker",
    "process": "进程",
    "systemd": "systemd",
}

REQUIRED_FIELDS = {
    "http": "url",
    "tcp": "port",
    "docker": "container",
    "process": "pattern",
    "systemd": "unit",
}

# Probe failures that mean the probe itself is misconfigured (missing binary,
# no permission, wrong container/unit name, daemon not reachable) rather than
# the service being down. Counting them as outages produced a fake alert every
# cooldown forever -- the same trap the TCP-port validation already avoids.
_CONFIG_ERROR_HINTS = (
    "permission denied",
    "system has not been booted with systemd",
    "could not be found",
    "no such file or directory",
    "executable not found",
    "command not found",
    "docker: not found",
    "cannot connect to the docker daemon",
    # ``docker inspect`` on a wrong container name answers with
    # "Error: No such object: <name>" (or "No such container"), which is a
    # typo in the config, not an outage.
    "no such object",
    "no such container",
    # ``systemctl`` reports "Unit x.service not found." / "Failed to get
    # properties" for a wrong unit name; without these the monitor raised a
    # fake outage every cooldown forever.
    "not found",
    "failed to get properties",
)

CONFIG_ERROR_PREFIX = "配置错误: "
# The probe infrastructure failed (spawn timeout, systemctl hang), so the
# service state is unknown. Counting it as an outage produced fake alerts on
# a busy docker daemon; these rounds keep the previous state instead.
PROBE_ERROR_PREFIX = "探测失败: "

# A permanently unreachable push target must not keep the monitor retrying
# forever: every attempt costs a full send timeout plus a state write. Alert
# retries are wider than recovery retries because the outage is still real,
# but both are bounded (the resource engine gives up after three).
MAX_ALERT_RETRIES = 10
MAX_RECOVERY_RETRIES = 5


def _looks_like_config_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in _CONFIG_ERROR_HINTS)


def _now_text(config: Any = None) -> str:
    if config is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return now_in_timezone(config).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_ts(ts: float, config: Any = None) -> str:
    try:
        return datetime.fromtimestamp(
            float(ts), now_in_timezone(config).tzinfo
        ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "--"


class ServiceMonitor:
    """Uptime-Kuma-like probes for HTTP, TCP, Docker, processes and systemd.

    The monitor never runs a browser and performs only lightweight checks.
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
        self.enabled = cfg_bool(config, "monitors.enabled", False)
        self.interval = clamp_config(
            cfg_int(config, "monitors.interval_seconds", 60),
            15,
            600,
            "monitors.interval_seconds",
        )
        self.fail_threshold = clamp_config(
            cfg_int(config, "monitors.fail_threshold", 2),
            1,
            10,
            "monitors.fail_threshold",
        )
        self.cooldown = max(
            0, min(86400, cfg_int(config, "monitors.cooldown_seconds", 1800))
        )
        self.recover_notify = cfg_bool(config, "monitors.recover_notify", True)
        self.platform_id = str(
            cfg_get(config, "alerts.platform_id", "default") or "default"
        )
        targets = cfg_get(config, "monitors.targets", [])
        if not targets:
            targets = cfg_get(config, "alerts.targets", [])
        self.targets = normalize_targets(targets, self.platform_id)
        self.items = self._normalize_items(cfg_get(config, "monitors.items", []))
        self._task: asyncio.Task | None = None
        self._running = False
        self._silence_warned = False
        self._silence_active = False
        self._suppress_warned: set[str] = set()
        self._targets_warned = False
        self._bg_tasks: set[asyncio.Task] = set()
        # Reuse one HTTP client per (verify, follow_redirects) pair: a fresh
        # AsyncClient per probe repeated the TLS handshake every interval and
        # left each item its own connection pool.
        self._http_clients: dict[tuple[bool, bool], httpx.AsyncClient] = {}
        self._prune_keys: set[str] | None = None

    def _normalize_items(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("__template_key") or item.get("type") or "").lower()
            if kind not in KIND_LABELS:
                logger.warning(
                    "监测项 %r 的类型 %r 不受支持，已跳过（可用类型：%s）",
                    str(item.get("name") or ""),
                    kind or "(空)",
                    "、".join(KIND_LABELS),
                )
                continue
            if not cfg_bool(item, "enabled", True):
                continue
            required = REQUIRED_FIELDS[kind]
            config_error: str | None = None
            required_value = item.get(required)
            if isinstance(required_value, str):
                required_value = required_value.strip()
            if not required_value:
                config_error = f"缺少 {required} 配置"
            elif kind == "tcp":
                port = cfg_int(item, "port", 0)
                # cfg_int swallows a non-numeric port into 0; both cases are
                # config errors, not outages.
                if not (1 <= port <= 65535):
                    config_error = f"端口 {item.get('port')!r} 不是有效的 1–65535 端口"
            if config_error:
                # A misconfigured probe would fail on every round and hit the
                # alert cooldown as a fake "service down"; keep it visible as
                # a config problem instead of an outage.
                logger.warning(
                    "监测项 %r（%s）配置不完整：%s，已跳过探测（修正后会自动恢复）",
                    str(item.get("name") or kind),
                    kind,
                    config_error,
                )
            normalized = dict(item)
            normalized["__template_key"] = kind
            normalized["name"] = str(item.get("name") or kind)
            if isinstance(item.get(required), str):
                normalized[required] = str(item.get(required)).strip()
            if config_error:
                normalized["config_error"] = config_error
            out.append(normalized)
        return out

    def _mark_stale_items(self) -> None:
        """Mark items that were alerting while the probe loop was not running.

        ``monitors.enabled=false`` (or a long restart) leaves ``alerting`` and
        ``since`` in state.json. Reporting that whole window as one recovery
        duration would be misleading, so such a recovery only writes the
        event and skips the notification.
        """

        with self.state.section("monitors") as monitors:
            items = monitors.get("items") or {}
            if not isinstance(items, dict):
                return
            now = time.time()
            for key, st in items.items():
                if not isinstance(st, dict) or not st.get("alerting"):
                    continue
                last_checked = safe_float(
                    st.get("last_checked") or st.get("since") or 0
                )
                if last_checked and now - last_checked > 86400:
                    st["stale"] = True
                    logger.warning(
                        "监测项 %s 已超过 24 小时未探测（可能关闭了监测），"
                        "恢复时只记事件、不发恢复消息",
                        key,
                    )

    def start(self) -> None:
        if not self.enabled:
            logger.info("服务监测未启用（monitors.enabled=false）")
            return
        if not self.items:
            logger.warning("服务监测已启用，但没有配置任何监测目标")
        elif not self._targets_for("service"):
            logger.error(
                "服务监测已启用但没有推送目标（通知中心无 service 规则，"
                "monitors.targets 与 alerts.targets 也均为空），"
                "监测项不会探测也不会告警；请在配置页填写推送目标"
            )
        self._prune_state()
        # A hand-edited state.json can hold a list/scalar here; replacing it
        # with a dict under the store's lock keeps start-up from crashing.
        with self.state.section("monitors"):
            pass
        self._mark_stale_items()
        self._running = True
        self._is_silenced()
        self._validate_suppression()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "服务监测已启动：%d 个目标，间隔=%ss，失败阈值=%d",
            len(self.items),
            self.interval,
            self.fail_threshold,
        )

    def _validate_suppression(self) -> None:
        if not cfg_bool(self.config, "monitors.suppress_duplicates", False):
            return
        for item in self.items:
            dependency = str(item.get("suppress_when_down") or "").strip()
            if not dependency:
                continue
            if self._find_dependency(dependency) is None:
                logger.warning(
                    "监测项 %r 的 suppress_when_down=%r 找不到对应监测项，抑制不会生效",
                    item.get("name") or "",
                    dependency,
                )
                self._suppress_warned.add(dependency)

    def _record_event(
        self,
        name: str,
        target: str,
        detail: str,
        status: str,
    ) -> None:
        if self.report_store is None:
            return
        try:
            now = time.time()
            day = now_in_timezone(self.config).strftime("%Y-%m-%d")
            self.report_store.add_event(
                int(now),
                day,
                "service",
                name,
                redact_url(detail),
                status,
                redact_url(target),
            )
            if status == "recovery":
                # Close the open alert of the same target so the report can
                # show how long the interruption lasted, even across a
                # plugin restart. Matching on kind+target keeps two monitor
                # items that share a name from closing each other's alerts.
                self.report_store.resolve_event(
                    int(now),
                    name,
                    int(now),
                    kind="service",
                    target=redact_url(target),
                    close_all=True,
                )
        except Exception:
            logger.exception("记录服务监测事件失败")

    def _prune_state(self) -> None:
        # No early return on an empty item list: clearing every monitor used
        # to leave its latched alerts open in the daily report until the
        # 30-day window rolled over.
        with self.state.section("monitors") as monitors:
            states = monitors.setdefault("items", {})
            if not isinstance(states, dict):
                states = {}
                monitors["items"] = states
            valid_keys = {self._item_key(item) for item in self.items}
            stale_keys = [key for key in states if key not in valid_keys]
            if not stale_keys:
                return
            for key in stale_keys:
                state = states.pop(key, None)
                if not isinstance(state, dict) or not state.get("alerting"):
                    continue
                # Close the episode before dropping its state: the old alert
                # row never got resolved_ts, so the daily report kept showing
                # an "未恢复" entry for up to 30 days. The name/target come
                # from the state when available (a colon inside a name used to
                # produce the wrong name and leave the alert open), otherwise
                # from the key itself.
                name = str(state.get("name") or "")
                if not name:
                    parts = str(key).split(":", 2)
                    name = parts[1] if len(parts) > 1 and parts[1] else "服务"
                target = str(state.get("target") or "")
                self._record_event(name, target, "监测项已移除，自动关闭", "recovery")
        self.state.save()
        logger.info("服务监测清理了 %d 个已失效状态项", len(stale_keys))

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
        # Best-effort notices queued by config-error closures used to be
        # destroyed with the plugin ("Task was destroyed but it is pending").
        background = [item for item in self._bg_tasks if not item.done()]
        for item in background:
            item.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._bg_tasks.clear()
        for client in self._http_clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._http_clients.clear()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("服务监测轮询失败")
            await asyncio.sleep(self.interval)

    async def check_all(self) -> None:
        if not self._targets_for("service"):
            # A missing push target means alerts could never reach anyone;
            # warn once at start-up (see audit) instead of every round.
            # The notification center may supply the target even when both
            # legacy lists are empty, so resolve it before giving up.
            if not self._targets_warned:
                logger.error(
                    "服务监测已启用但没有推送目标（通知中心无 service 规则，"
                    "monitors.targets 与 alerts.targets 也均为空），"
                    "不会探测也不会告警；请在配置页填写推送目标"
                )
                self._targets_warned = True
            return
        # Dozens of targets used to be probed at once; a hung docker daemon or
        # a slow endpoint then produced a burst of subprocesses/connections.
        # Eight at a time is plenty for the 30s interval.
        semaphore = asyncio.Semaphore(8)

        async def _probe_limited(item: dict[str, Any]) -> Any:
            async with semaphore:
                return await self._check_item(item)

        results = await asyncio.gather(
            *(
                _probe_limited(item)
                for item in self.items
                if not item.get("config_error")
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("服务监测检查异常: %s", result)
        # Items skipped for config errors never reach _check_item; if one
        # was already alerting when its config broke, close that episode
        # here instead of freezing it in the "unresolved" list forever.
        for item in self.items:
            if not item.get("config_error"):
                continue
            key = self._item_key(item)
            name = str(item.get("name") or item.get("__template_key") or "服务")
            kind = KIND_LABELS.get(str(item.get("__template_key")), "服务")
            with self.state.section("monitors") as monitors:
                if not isinstance(monitors, dict):
                    continue
                states = monitors.get("items")
                if not isinstance(states, dict):
                    continue
                st = states.get(key)
                if not isinstance(st, dict) or not st.get("alerting"):
                    continue
                target_text = str(
                    item.get("url")
                    or item.get("host")
                    or item.get("container")
                    or item.get("unit")
                    or item.get("pattern")
                    or name
                )
                self._close_episode_for_config_error(
                    st,
                    name,
                    redact_url(target_text),
                    kind,
                    f"配置不完整（{item.get('config_error')}）",
                )
        with self.state.section("monitors") as monitors:
            monitors["last_run"] = time.time()
        # A config change that does not reload the plugin (rare, but the WebUI
        # can rewrite items in place) used to leave the removed item latched:
        # its alert stayed "unresolved" until a restart. Prune whenever the
        # key set changes.
        current_keys = {self._item_key(item) for item in self.items}
        if self._prune_keys is None or current_keys != self._prune_keys:
            self._prune_keys = current_keys
            await asyncio.to_thread(self._prune_state)
        self.state.save()

    def _silence_until(self) -> datetime | None:
        """Parse ``monitors.silence_until`` as a wall-clock time in display.timezone."""

        raw = str(cfg_get(self.config, "monitors.silence_until", "") or "").strip()
        if not raw:
            return None
        parsed: datetime | None = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            if not self._silence_warned:
                logger.warning(
                    "monitors.silence_until 格式非法：%s（应为 YYYY-MM-DD HH:MM）",
                    raw,
                )
                self._silence_warned = True
            return None
        return parsed.replace(tzinfo=now_in_timezone(self.config).tzinfo)

    def _is_silenced(self) -> bool:
        if not cfg_bool(self.config, "monitors.silence_enabled", False):
            if self._silence_active:
                logger.info("服务监测维护静默开关已关闭，恢复推送")
                self._silence_active = False
            return False
        until = self._silence_until()
        silenced = bool(until is not None and now_in_timezone(self.config) < until)
        if silenced != self._silence_active:
            if silenced and until is not None:
                logger.info(
                    "服务监测进入维护静默，截止 %s；期间只探测，不推送异常/恢复通知",
                    until.strftime("%Y-%m-%d %H:%M"),
                )
            else:
                logger.info("服务监测维护静默已结束，恢复推送")
            self._silence_active = silenced
        return silenced

    def _find_dependency(self, name: str) -> dict[str, Any] | None:
        for other in self.items:
            if str(other.get("name")) == name:
                return other
        return None

    def _dependency_state(self, name: str) -> dict[str, Any] | None:
        other = self._find_dependency(name)
        if other is None:
            return None
        states = self.state.get("monitors", {}).get("items", {})
        if not isinstance(states, dict):
            return {}
        value = states.get(self._item_key(other))
        return value if isinstance(value, dict) else {}

    def _suppress_alert(self, item: dict[str, Any]) -> str | None:
        """Return the dependency name when an HTTP alert should be suppressed.

        ``suppress_when_down`` is intentionally a monitor item name, so the
        user can point an HTTP check at the container/service check that must
        be healthy first. This avoids two alarms for one outage.
        """

        if not cfg_bool(self.config, "monitors.suppress_duplicates", False):
            return None
        dependency = str(item.get("suppress_when_down") or "").strip()
        if not dependency:
            return None
        if self._find_dependency(dependency) is None:
            if dependency not in self._suppress_warned:
                logger.warning(
                    "监测项 %r 的 suppress_when_down=%r 找不到对应监测项，抑制不会生效",
                    item.get("name") or "",
                    dependency,
                )
                self._suppress_warned.add(dependency)
            return None
        state = self._dependency_state(dependency) or {}
        if state.get("alerting") or int(state.get("fails") or 0) >= self.fail_threshold:
            return dependency
        return None

    async def _check_item(self, item: dict[str, Any]) -> None:
        key = self._item_key(item)
        ok, detail, target_text = await self._probe(item)
        target_text = redact_url(target_text)
        now = time.time()
        monitors = self.state.setdefault("monitors", {})
        if not isinstance(monitors, dict):
            monitors = {}
            self.state.set("monitors", monitors)
        states = monitors.get("items")
        if not isinstance(states, dict):
            # A hand-edited state.json holding a list here made every probe
            # raise, which silently disabled monitoring.
            states = {}
            monitors["items"] = states
        st = states.get(key)
        if not isinstance(st, dict):
            st = {}
            states[key] = st
        fails = safe_int(st.get("fails") or 0)
        alerting = bool(st.get("alerting"))
        pending = bool(st.get("pending"))
        last_sent = safe_float(st.get("last_sent") or 0)
        name = str(item.get("name") or item.get("__template_key") or "服务")
        kind = KIND_LABELS.get(str(item.get("__template_key")), "服务")
        # Persisted so pruning a removed item can close its alert under the
        # right name and target even when the name contains a colon.
        st["name"] = name
        st["target"] = target_text

        if st.pop("send_inflight", False) and not alerting:
            # Reloaded/crashed during a slow alert send. The message may have
            # reached the chat, so do not fire the same alert again after the
            # threshold is met once more; keep the recovery notice quiet for
            # the same reason the resource rules do.
            alerting = True
            st["alerting"] = True
            st["alerted"] = False
            st["since"] = safe_float(st.get("last_attempt")) or now
            st["fails"] = 0
            # Log an alert row so the episode has a start in the event log;
            # without it the later recovery had no matching beginning.
            self._record_event(
                name,
                target_text,
                "上次告警发送被中断（重载/重启），已按告警中处理",
                "alert",
            )

        if not ok and detail.startswith(CONFIG_ERROR_PREFIX):
            # Misconfiguration is not an outage: warn once, never alert.
            # An episode that was already alerting must be closed here though:
            # returning with alerting still set kept the item in the daily
            # report's "unresolved" list (and the recovery notice silent)
            # until the 30-day window rolled over -- fixed in v2.20.0.
            st["last_checked"] = now
            st["last_error"] = detail
            st["fails"] = 0
            if not st.get("config_error_warned"):
                st["config_error_warned"] = True
                logger.warning(
                    "监测项 %s 探测配置错误，已跳过告警"
                    "（请检查容器名/unit 名/权限）: %s",
                    name,
                    detail,
                )
            if alerting:
                self._close_episode_for_config_error(
                    st, name, target_text, kind, detail
                )
            states[key] = st
            return

        if not ok and detail.startswith(PROBE_ERROR_PREFIX):
            # The probe itself failed (subprocess timeout / spawn error). The
            # service may be fine, so do not count this as a failed check;
            # keep the previous state and warn at most every 10 minutes.
            st["last_checked"] = now
            st["last_error"] = detail
            last_warn = safe_float(st.get("probe_error_at") or 0)
            if now - last_warn > 600:
                st["probe_error_at"] = now
                logger.warning("监测项 %s 探测失败，本轮跳过判定: %s", name, detail)
            states[key] = st
            return

        if self._is_silenced():
            # The maintenance window mutes the push, not the state machine.
            # Freezing it entirely meant a fault that started and ended inside
            # the window left no event at all, and one that outlived it
            # reported the whole window as the outage duration.
            st["last_checked"] = now
            if ok:
                # Capture the fault text before clearing it: the alert row
                # should describe the outage, not the successful probe.
                prev_error = str(st.get("last_error") or "服务异常")
                st["last_ok"] = now
                st["last_error"] = ""
                started = float(st.pop("silent_since", 0) or 0)
                if started:
                    self._record_event(
                        name,
                        target_text,
                        f"{prev_error}（维护静默期内自愈，未推送）",
                        "alert",
                    )
                    self._record_event(
                        name,
                        target_text,
                        f"持续 {format_duration(now - started)}"
                        "（维护静默期内，未推送）",
                        "recovery",
                    )
            else:
                st["last_error"] = detail
                st.setdefault("silent_since", now)
            states[key] = st
            return

        # A fault that became healthy right at the end of the window: record
        # it with the real start time instead of dropping the episode. A
        # fault that outlives the window keeps its start time and simply
        # rejoins the normal state machine below (v2.20.0: the start used
        # to be dropped, so the pre-silence outage vanished from the log).
        silent_since = float(st.pop("silent_since", 0) or 0)
        if silent_since and ok:
            prev_error = str(st.get("last_error") or "服务异常")
            self._record_event(
                name,
                target_text,
                f"{prev_error}（维护静默期内自愈，未推送）",
                "alert",
            )
            self._record_event(
                name,
                target_text,
                f"持续 {format_duration(now - silent_since)}（维护静默期内，未推送）",
                "recovery",
            )
            st["last_error"] = ""

        if not ok and not alerting:
            suppressed_by = self._suppress_alert(item)
            if suppressed_by:
                st["fails"] = fails
                st["last_error"] = detail
                st["last_checked"] = now
                states[key] = st
                logger.info(
                    "监测项 %s 异常，但依赖项 %s 也异常，已抑制重复告警",
                    name,
                    suppressed_by,
                )
                return

        if not ok:
            if not alerting:
                # Once latched the counter is not used for anything; letting
                # it grow made state.json and the dashboard show an ever
                # increasing "fails" number during a long outage.
                fails += 1
            will_alert = not alerting and fails >= self.fail_threshold
            st["fails"] = fails
            st["last_error"] = detail
            st["last_checked"] = now
            if will_alert:
                next_attempt = safe_float(st.get("next_attempt") or 0)
                if last_sent and now - last_sent < self.cooldown:
                    # Inside the cooldown: latch the episode and record one
                    # event (with the suppression noted) instead of vanishing.
                    # Staying silent here meant an entire outage never reached
                    # the event log and the recovery notice later arrived
                    # without any matching alert -- the defect v2.18.1 fixed
                    # for the resource rules.
                    st["alerting"] = True
                    st["alerted"] = False
                    st["since"] = now
                    st["last_attempt"] = now
                    st["fails"] = 0
                    st["pending"] = False
                    self._record_event(
                        name,
                        target_text,
                        f"{detail}（冷却窗口内仅记录，未重复推送）",
                        "alert",
                    )
                    logger.info(
                        "监测项 %s 在冷却窗口内再次异常，已记录事件不重复推送",
                        name,
                    )
                    states[key] = st
                    return
                if now >= next_attempt:
                    # A second failure after a recovery notice could not be
                    # delivered used to drop that notice silently. Fold it
                    # into the new alert instead of losing the fact that the
                    # previous recovery never reached the operator.
                    pending_recovery = st.pop("pending_recovery", None)
                    st.pop("recovery_attempt", None)
                    st.pop("recovery_next_attempt", None)
                    st.pop("recovery_retry_count", None)
                    text = (
                        f"🔴 服务异常 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"原因: {detail}\n"
                        f"时间: {_now_text(self.config)}\n"
                        f"已连续 {self.fail_threshold} 次检测失败。"
                    )
                    if pending_recovery:
                        text += "\n（上次恢复通知未送达，已合并到本次告警）"
                    # Persist before the await: a reload or crash during a
                    # slow send used to leave the item "not alerting" on disk,
                    # so the same outage alerted again after restart.
                    st["last_attempt"] = now
                    st["send_inflight"] = True
                    self.state.save()
                    result = await self._push_alert(name, detail, text, target_text)
                    buffered = result is None
                    delivered = (not buffered) and self._delivered(result)
                    st.pop("send_inflight", None)
                    st["alerting"] = True
                    st["alerted"] = True
                    st["since"] = now
                    st["last_attempt"] = now
                    st["fails"] = 0
                    st["pending"] = not delivered and not buffered
                    if buffered:
                        # Covered by the aggregator summary, which does reach
                        # the chat: start the cooldown now and keep a single
                        # "alert" event. Treating a buffered alert as a failed
                        # push also wrote a duplicate "failed" row for one
                        # outage and skipped the cooldown entirely.
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        # The alert itself is still waiting for the summary:
                        # a recovery inside the window must queue behind it.
                        st["awaiting_summary"] = True
                        # Identifies THIS item as the buffered one: two
                        # monitors may share a name (keys differ by target),
                        # and the abandon callback matches by name -- without
                        # the marker it also withdrew the item whose alert
                        # was delivered on its own.
                        st["summary_buffered"] = True
                        self._record_event(name, target_text, detail, "alert")
                    elif delivered:
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        # The alert went out on its own; the buffered marker
                        # (if a previous episode ever buffered) must go or a
                        # later same-name abandon would withdraw this item's
                        # "notified" state (v2.20.0).
                        st.pop("summary_buffered", None)
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        retry_count = safe_int(st.get("retry_count") or 0) + 1
                        st["retry_count"] = retry_count
                        st["next_attempt"] = now + retry_delay(retry_count)
                        self._record_event(name, target_text, detail, "failed")
            elif alerting and pending:
                next_attempt = safe_float(st.get("next_attempt") or 0)
                if now >= next_attempt:
                    text = (
                        f"🔴 服务异常 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"原因: {detail}\n"
                        f"时间: {_now_text(self.config)}"
                    )
                    # Persist the in-flight marker before the await so a reload
                    # during a slow retry cannot replay the same alert from a
                    # stale `pending` value (v2.20.1).
                    st["send_inflight"] = True
                    self.state.save()
                    result = await self._send(text)
                    st.pop("send_inflight", None)
                    st["last_attempt"] = now
                    if self._delivered(result):
                        st["pending"] = False
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        st.pop("summary_buffered", None)
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        retry_count = safe_int(st.get("retry_count") or 0) + 1
                        if retry_count > MAX_ALERT_RETRIES:
                            # The operator never received this outage. Stop
                            # retrying the alert, but keep the episode latched
                            # and let the recovery notice announce it with the
                            # "此前未能送达" note -- the same semantics as the
                            # resource rules, so a silent channel produces a
                            # consistent story instead of silence.
                            st["pending"] = False
                            st["alerted"] = True
                            st["send_exhausted"] = True
                            st["retry_count"] = 0
                            st["next_attempt"] = 0
                            logger.warning(
                                "监测项 %s 告警连续 %d 次发送失败，已停止重试（恢复时将一并说明）",
                                name,
                                MAX_ALERT_RETRIES,
                            )
                        else:
                            st["retry_count"] = retry_count
                            st["next_attempt"] = now + retry_delay(retry_count)
        else:
            st.pop("config_error_warned", None)
            st["fails"] = 0
            st["last_checked"] = now
            st["last_ok"] = now
            st["last_error"] = ""
            pending_text = st.get("pending_recovery")
            if pending_text and not self.recover_notify:
                # The operator turned recovery notices off after this one was
                # queued for retry; honouring the switch means dropping the
                # backlog instead of delivering it later anyway (same rule as
                # the resource alerts).
                for stale_key in (
                    "pending_recovery",
                    "recovery_attempt",
                    "recovery_next_attempt",
                    "recovery_retry_count",
                    "send_exhausted",
                    "awaiting_summary",
                ):
                    st.pop(stale_key, None)
                pending_text = None
            if pending_text:
                next_recovery = safe_float(st.get("recovery_next_attempt") or 0)
                if now >= next_recovery:
                    result = await self._send(str(pending_text))
                    if self._delivered(result):
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
                        st.pop("recovery_next_attempt", None)
                        st.pop("recovery_retry_count", None)
                    else:
                        retry_count = safe_int(st.get("recovery_retry_count") or 0) + 1
                        if retry_count > MAX_RECOVERY_RETRIES:
                            # A permanently unreachable target would otherwise
                            # retry this notice every ten minutes until the
                            # process restarts.
                            for stale_key in (
                                "pending_recovery",
                                "recovery_attempt",
                                "recovery_next_attempt",
                                "recovery_retry_count",
                            ):
                                st.pop(stale_key, None)
                            logger.warning(
                                "监测项 %s 恢复通知连续 %d 次发送失败，已停止重试",
                                name,
                                MAX_RECOVERY_RETRIES,
                            )
                        else:
                            st["recovery_retry_count"] = retry_count
                            st["recovery_next_attempt"] = now + retry_delay(retry_count)
            if alerting:
                stale = bool(st.pop("stale", False))
                # Only announce a recovery for an episode that actually
                # reached the operator; a cooldown-suppressed one is logged
                # but stays silent (same rule as the resource alerts).
                notified = bool(st.pop("alerted", True))
                duration = now - safe_float(st.get("since") or now)
                st["alerting"] = False
                st["since"] = 0
                st["retry_count"] = 0
                st["next_attempt"] = 0
                # Episode is over: the buffered marker must not survive into
                # the next one, or a later same-name abandon would withdraw
                # this item's "notified" state (v2.20.0).
                st.pop("summary_buffered", None)
                # The event log does not depend on the notification pref: the
                # daily report's interruption durations and unresolved list
                # rely on resolved_ts being written on every recovery.
                self._record_event(name, target_text, detail, "recovery")
                # Pop unconditionally: leaving it set across a
                # recover_notify=False recovery made the NEXT healthy
                # recovery claim an undelivered alert out of nowhere.
                exhausted = bool(st.pop("send_exhausted", False))
                if self.recover_notify and not stale and notified:
                    had_failed = exhausted or bool(pending)
                    extra = "\n（异常告警此前未能送达）" if had_failed else ""
                    text = (
                        f"✅ 服务恢复 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"故障开始: {_fmt_ts(now - duration, self.config)}\n"
                        f"恢复时间: {_now_text(self.config)}\n"
                        f"中断时长: {format_duration(duration)}{extra}"
                    )
                    awaiting = st.pop("awaiting_summary", False)
                    remaining = (
                        self._aggregator.window_remaining("service")
                        if self._aggregator is not None
                        else 0.0
                    )
                    if awaiting and remaining > 0:
                        # This item's alert is still inside the aggregation
                        # window. Posting the recovery now would let it beat
                        # the summary to the chat; queue it so the summary is
                        # posted first. On abandon the queued notice still
                        # goes out -- the user then at least learns the
                        # episode happened.
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + max(15.0, remaining + 3.0)
                        logger.info(
                            "监测项 %s 在聚合窗口内恢复，恢复通知延后 %.0f 秒发送",
                            name,
                            max(15.0, remaining + 3.0),
                        )
                    else:
                        result = await self._send(text)
                        if not self._delivered(result):
                            st["pending_recovery"] = text
                            st["recovery_retry_count"] = 1
                            st["recovery_next_attempt"] = now + retry_delay(1)
                            logger.warning(
                                "监测项 %s 恢复通知未送达，已排队稍后补发", name
                            )
                st["pending"] = False
        states[key] = st

    def _close_episode_for_config_error(
        self,
        st: dict[str, Any],
        name: str,
        target_text: str,
        kind: str,
        detail: str,
    ) -> None:
        """Close an in-flight alert because the target no longer exists.

        Deleting a container/unit while its monitor is alerting used to
        freeze the episode forever: the config-error branch returned
        without clearing ``alerting``, so the daily report kept listing
        the item as unresolved and no recovery notice was ever sent.
        """

        notified = bool(st.pop("alerted", True))
        duration = time.time() - safe_float(st.get("since") or time.time())
        st["alerting"] = False
        st["since"] = 0
        st["retry_count"] = 0
        st["next_attempt"] = 0
        st["pending"] = False
        for stale_key in (
            "awaiting_summary",
            "summary_buffered",
            "send_exhausted",
            "stale",
            "silent_since",
        ):
            st.pop(stale_key, None)
        self._record_event(
            name,
            target_text,
            f"目标已不存在或配置错误，告警自动关闭（{detail}）",
            "recovery",
        )
        if notified and self.recover_notify and not self._is_silenced():
            # The maintenance window promises "probe only, never push"; a
            # config-error close is still a recovery notice and used to slip
            # past the silence check because it returns before it (v2.20.1).
            text = (
                f"✅ 服务告警关闭 · {name}\n"
                "──────────────\n"
                f"类型: {kind}\n"
                f"目标: {target_text}\n"
                f"原因: 目标已不存在或配置错误\n"
                f"故障开始: {_fmt_ts(time.time() - duration, self.config)}\n"
                f"中断时长: {format_duration(duration)}\n"
                "（监测项配置已失效，请更新或删除该监测项）"
            )
            self._queue_send_now(text, name)

    def _queue_send_now(self, text: str, name: str) -> None:
        """Best-effort direct send for a notice that must not be aggregated."""

        async def _send_task() -> None:
            result = await self._send(text)
            if not self._delivered(result):
                logger.warning("监测项 %s 的通知发送未确认送达", name)

        task = asyncio.create_task(_send_task())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _http_client(self, verify: bool, follow: bool) -> httpx.AsyncClient:
        key = (verify, follow)
        client = self._http_clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                follow_redirects=follow,
                verify=verify,
                # Per-request timeout is passed to stream(); this is only the
                # pool-wide default for anything that forgets it.
                timeout=httpx.Timeout(60.0),
            )
            self._http_clients[key] = client
        return client

    async def _probe(self, item: dict[str, Any]) -> tuple[bool, str, str]:
        kind = str(item.get("__template_key") or "")
        timeout = max(1, min(60, cfg_int(item, "timeout_seconds", 10)))
        if kind == "http":
            url = str(item.get("url") or "").strip()
            if not url:
                return False, "URL 未配置", "-"
            expected = cfg_int(item, "expected_status", 0)
            keyword = str(item.get("keyword") or "").strip()
            try:
                started = time.perf_counter()

                # The httpx timeout applies to every single read, so a slow
                # drip can keep the probe alive far beyond the configured
                # limit. The outer wait_for caps the whole probe.
                async def _do_probe() -> tuple[bool, str, str]:
                    # A 3xx expected status cannot be matched while redirects
                    # are followed: the probe would compare the final 200.
                    follow = not (300 <= expected < 400)
                    verify_tls = cfg_bool(item, "verify_tls", True)
                    cli = self._http_client(verify_tls, follow)
                    async with cli.stream("GET", url, timeout=timeout) as resp:
                        status_code = resp.status_code
                        if expected and status_code != expected:
                            return (
                                False,
                                f"HTTP {status_code}（期望 {expected}）",
                                url,
                            )
                        if not expected and status_code >= 400:
                            return False, f"HTTP {status_code}", url
                        if keyword:
                            limit = max(
                                1024,
                                min(
                                    10 * 1024 * 1024,
                                    cfg_int(
                                        self.config,
                                        "monitors.max_response_bytes",
                                        262144,
                                    ),
                                ),
                            )
                            body = bytearray()
                            async for chunk in resp.aiter_bytes():
                                body.extend(chunk)
                                if len(body) >= limit:
                                    body = body[:limit]
                                    break
                            encoding = resp.charset_encoding or "utf-8"
                            try:
                                text = body.decode(encoding, errors="replace")
                            except LookupError:
                                text = body.decode("utf-8", errors="replace")
                            if keyword not in text:
                                return False, "响应中未找到关键字", url
                        ms = (time.perf_counter() - started) * 1000
                        return True, f"HTTP {status_code}（{ms:.0f}ms）", url

                ok, detail, target_text = await asyncio.wait_for(
                    _do_probe(), timeout=timeout + 2
                )
                return ok, detail, target_text
            except TimeoutError:
                return False, f"探测超时（超过 {timeout + 2} 秒）", url
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", url

        if kind == "tcp":
            host = str(item.get("host") or "127.0.0.1").strip()
            port = cfg_int(item, "port", 0)
            target = f"{host}:{port}"
            if not port or not (1 <= port <= 65535):
                return False, "端口未配置或超出 1–65535", target
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=timeout
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return True, f"TCP {target} 连接成功", target
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", target

        if kind == "docker":
            container = str(item.get("container") or "").strip()
            if not container:
                return False, "容器名未配置", "-"
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Running}}",
                        container,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if proc.returncode != 0:
                    detail = (
                        proc.stderr or proc.stdout or "docker inspect 失败"
                    ).strip()
                    if _looks_like_config_error(detail):
                        return (
                            False,
                            f"{CONFIG_ERROR_PREFIX}{detail[:180]}",
                            container,
                        )
                    return False, detail[:200], container
                running = proc.stdout.strip().lower() == "true"
                return running, f"容器 running={running}", container
            except subprocess.TimeoutExpired:
                return (
                    False,
                    f"{PROBE_ERROR_PREFIX}docker inspect 超过 {timeout}s 未返回",
                    container,
                )
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", container

        if kind == "process":
            pattern = str(item.get("pattern") or "").strip().lower()
            if not pattern:
                return False, "进程匹配串未配置", "-"

            def scan_processes() -> tuple[bool, str, str]:
                for proc in psutil.process_iter(["name", "cmdline"]):
                    try:
                        name = str(proc.info.get("name") or "")
                        cmdline = " ".join(proc.info.get("cmdline") or [])
                        if pattern in f"{name} {cmdline}".lower():
                            return True, f"匹配到进程 {name or proc.pid}", pattern
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                return False, f"未找到匹配进程: {pattern}", pattern

            try:
                # Scanning every /proc entry is a synchronous syscall loop;
                # run it off the event loop like the status-card collector.
                return await asyncio.to_thread(scan_processes)
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", pattern

        if kind == "systemd":
            unit = str(item.get("unit") or "").strip()
            if not unit:
                return False, "unit 未配置", "-"
            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "is-active", unit],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                state_text = (proc.stdout or proc.stderr or "unknown").strip()
                if _looks_like_config_error(state_text):
                    return (
                        False,
                        f"{CONFIG_ERROR_PREFIX}{unit}: {state_text[:180]}",
                        unit,
                    )
                if state_text != "active":
                    # ``is-active`` answers "inactive" both for a stopped
                    # service and for a unit that does not exist, so only
                    # LoadState can tell a typo from a real outage.
                    try:
                        show = await asyncio.to_thread(
                            subprocess.run,
                            [
                                "systemctl",
                                "show",
                                "-p",
                                "LoadState",
                                "--value",
                                unit,
                            ],
                            capture_output=True,
                            text=True,
                            timeout=timeout,
                        )
                        load_state = (show.stdout or "").strip()
                    except Exception:
                        load_state = ""
                    if load_state == "not-found":
                        return (
                            False,
                            (
                                f"{CONFIG_ERROR_PREFIX}{unit}: unit 不存在"
                                "（LoadState=not-found）"
                            ),
                            unit,
                        )
                return state_text == "active", f"{unit}: {state_text}", unit
            except subprocess.TimeoutExpired:
                return (
                    False,
                    f"{PROBE_ERROR_PREFIX}systemctl 超过 {timeout}s 未返回",
                    unit,
                )
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", unit

        return False, f"未知监测类型: {kind}", "-"

    @staticmethod
    def _item_key(item: dict[str, Any]) -> str:
        kind = str(item.get("__template_key") or "")
        name = str(item.get("name") or "")
        if kind == "http":
            target = str(item.get("url") or "")
            key_target = redact_url(target)
            try:
                query = urlsplit(target).query
            except Exception:
                query = ""
            if query:
                # Two probes on the same path with different query strings are
                # different targets: they must not share one state slot (one
                # success would otherwise reset the other's alerting latch).
                # Only a short hash is persisted, so tokens stay out of
                # state.json.
                key_target += "#q" + hashlib.sha1(query.encode("utf-8")).hexdigest()[:8]
            return f"{kind}:{name}:{key_target}"
        if kind == "tcp":
            target = f"{item.get('host', '')}:{item.get('port', '')}"
        elif kind == "docker":
            target = str(item.get("container") or "")
        elif kind == "process":
            target = str(item.get("pattern") or "")
        elif kind == "systemd":
            target = str(item.get("unit") or "")
        else:
            target = ""
        # Keep credentials out of state.json: the key is persisted and also
        # returned by status(); probe/event paths already redact the URL.
        return f"{kind}:{name}:{redact_url(target)}"

    def _targets_for(self, kind: str = "service") -> list[str]:
        picked = resolve_targets(self.config, kind)
        return self.targets if picked is None else picked

    async def _send(self, text: str) -> DeliveryResult:
        targets = self._targets_for("service")
        if not targets:
            logger.warning("服务监测推送目标为空")
            return DeliveryResult(ok=False)
        try:
            value = await self.plugin.send_text(targets, text, source="monitor")
            result = (
                value
                if isinstance(value, DeliveryResult)
                else DeliveryResult(ok=bool(value))
            )
            if result.partial:
                # Some targets received it: retrying would post twice to
                # those, so surface the partial delivery instead.
                logger.warning(
                    "服务监测部分送达：成功 %d 个、失败 %d 个目标（不重发）",
                    len(result.ok_targets),
                    len(result.failed_targets),
                )
            return result
        except Exception:
            logger.exception("服务监测推送失败")
            return DeliveryResult(ok=False)

    async def _push_alert(
        self, name: str, detail: str, text: str, target_text: str
    ) -> Any | None:
        """Send the first alert of an episode, or buffer it in the window.

        Returns ``None`` when the aggregator buffered the item; the summary
        covers it and no per-item retry is needed.
        """

        if self._aggregator is None:
            return await self._send(text)
        return await self._aggregator.push(
            label=name,
            reason=detail,
            text=text,
            send=self._send,
            kind="service",
            on_abandon=self._on_summary_abandoned,
        )

    def _on_summary_abandoned(self, items: list[tuple[str, str]]) -> None:
        """Withdraw "alerted" for monitors whose summary never arrived.

        ``last_sent`` goes with it: it was recorded when the alert was
        buffered ("the summary did reach the chat"), and keeping it would
        silence a new episode inside the cooldown for half an hour after the
        summary was given up on.
        """

        labels = {label for label, _reason in items if label}
        if not labels:
            return
        changed = False
        with self.state.section("monitors") as monitors:
            states = monitors.get("items")
            if not isinstance(states, dict):
                return
            for st in states.values():
                if not isinstance(st, dict) or not st.get("alerting"):
                    continue
                if str(st.get("name") or "") in labels:
                    # Only withdraw items that were actually buffered: a
                    # same-named item whose alert went out on its own keeps
                    # its "notified" state.
                    if not st.pop("summary_buffered", None):
                        continue
                    st["alerted"] = False
                    st.pop("last_sent", None)
                    st.pop("awaiting_summary", None)
                    changed = True
        # Abandon can be the last write before a reload (stop() runs after
        # the flush tasks are cancelled); persist it or the new instance
        # reads the stale "alerted=True" from disk.
        if changed:
            self.state.save()

    @staticmethod
    def _delivered(result: Any) -> bool:
        """Count a possibly-delivered message as delivered (no duplicates)."""

        if isinstance(result, DeliveryResult):
            return result.ok or bool(result.uncertain_targets)
        return bool(result)

    async def test_all(self) -> list[dict[str, Any]]:
        async def one(item: dict[str, Any]) -> dict[str, Any]:
            error = str(item.get("config_error") or "")
            if error:
                return {
                    "name": str(item.get("name") or ""),
                    "kind": KIND_LABELS.get(str(item.get("__template_key")), "?"),
                    "target": "",
                    "ok": False,
                    "detail": f"配置不完整（{error}），未执行探测",
                }
            ok, detail, target = await self._probe(item)
            return {
                "name": str(item.get("name") or ""),
                "kind": KIND_LABELS.get(str(item.get("__template_key")), "?"),
                "target": target,
                "ok": ok,
                "detail": detail,
            }

        sem = asyncio.Semaphore(6)

        async def limited(it: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                return await one(it)

        return list(await asyncio.gather(*(limited(item) for item in self.items)))

    def status(self) -> dict[str, Any]:
        monitors = self.state.get("monitors", {}) or {}
        if not isinstance(monitors, dict):
            # A hand-edited state.json must not make the dashboard route raise.
            monitors = {}
        item_states = monitors.get("items")
        if not isinstance(item_states, dict):
            item_states = {}
        return {
            "enabled": self.enabled,
            "targets": list(self.targets),
            "interval": self.interval,
            "fail_threshold": self.fail_threshold,
            "cooldown": self.cooldown,
            "silence_until": str(
                cfg_get(self.config, "monitors.silence_until", "") or ""
            ),
            "silenced": self._is_silenced(),
            "items": [
                {
                    "name": item.get("name"),
                    "kind": KIND_LABELS.get(str(item.get("__template_key")), "?"),
                    "target": self._item_key(item),
                    "state": item_states.get(self._item_key(item), {}),
                    "config_error": item.get("config_error"),
                }
                for item in self.items
            ],
            "last_run": monitors.get("last_run"),
        }
