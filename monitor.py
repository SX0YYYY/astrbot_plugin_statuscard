from __future__ import annotations

import asyncio
import hashlib
import re
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
from .redactor import Redactor, read_rules
from .state import StateStore
from .utils import (
    clamp_config,
    delivery_offline,
    normalize_targets,
    offline_retry_delay,
    redact_url,
    retry_delay,
    safe_float,
    safe_int,
    summary_item_parts,
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
    #
    # The bare "not found" was too wide: any service whose own stderr said
    # "... not found in /etc/app.conf" was classified as a configuration
    # error and never alerted. systemctl terminates that sentence with a
    # period, so anchor on it; the "could not be found" form is covered by
    # the hint above (v2.20.20).
    " not found.",
    "failed to get properties",
    # A URL without a scheme ("internal:8080/health") and an unresolvable
    # host are configuration typos, not outages. Reporting them as faults
    # raised a fake alert every time the cooldown expired.
    "unsupportedprotocol",
    "request url is missing an 'http://' or 'https://' protocol",
    "invalid url",
    "invalidurl",
    "name or service not known",
    "nodename nor servname provided",
    "no address associated with hostname",
    # Windows (and a localized Python) reports the same two cases with
    # different text: a missing docker/systemctl executable is
    # "FileNotFoundError: [WinError 2] 系统找不到指定的文件。" and a bad host
    # is "getaddrinfo failed". Neither matched the English/Linux hints, so a
    # Windows deployment alerted every cooldown and stayed "未恢复" forever
    # (v2.20.19).
    "[winerror 2]",
    "系统找不到指定的文件",
    "cannot find the file specified",
    "not recognized as an internal or external command",
    "is not recognized as the name of a cmdlet",
    # psutil.AccessDenied stringifies as "(pid=1)" -- the class name is the
    # only hint that this is an environment/permission problem. Without it a
    # restricted account alerted forever on a scan it cannot perform
    # (2026-11-06 audit).
    "accessdenied",
    "access is denied",
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
    if "getaddrinfo failed" in lowered:
        # Windows uses the same text for errno 11001 (host not found, a
        # typo) and 11002 (WSATRY_AGAIN, a temporary resolver failure).
        # Treating both as configuration suppressed real DNS outages
        # forever; only 11001 is a config error (v2.20.19 reverse audit).
        return "11001" in lowered or "host not found" in lowered
    return any(hint in lowered for hint in _CONFIG_ERROR_HINTS)


# Failures that look like a configuration typo on a brand-new target but mean
# a real outage once the same target has probed successfully before. A domain
# whose DNS record disappears and a docker daemon that goes down are not
# configuration mistakes, yet the classifier used to close the live episode
# and announce "配置已失效" -- the service was then never alerted again
# (2026-11-06 audit).
_TRANSIENT_PROBE_HINTS = (
    "name or service not known",
    "nodename nor servname provided",
    "no address associated with hostname",
    "cannot connect to the docker daemon",
)


def _looks_like_transient_probe_error(text: str) -> bool:
    lowered = str(text or "").lower()
    if "getaddrinfo failed" in lowered:
        # On a target that worked before, 11001 means the record is gone.
        return True
    return any(hint in lowered for hint in _TRANSIENT_PROBE_HINTS)


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
        # The push text must obey the same hostname rule as the card, the
        # report, the dashboard and the "立即检测" command. Event names stay
        # raw (the local DB is not the leak surface and report/dashboard mask
        # them on output); only operator-facing chat text is masked.
        self._redactor = Redactor(read_rules(config, "display.redact"))
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
        # Items dropped by _normalize_items (unknown type, exact duplicate).
        # The audit reads this list: otherwise a typo in a monitor type was
        # only a log line and every user-facing check said "no problems"
        # while the item never ran (2026-11-06 audit).
        self.skipped_items: list[str] = []
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
        normalized_items: list[tuple[dict[str, Any], tuple[str, ...]]] = []
        seen_signatures: set[tuple[str, ...]] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            # Disabled entries never probe, so a wrong type on one is not a
            # problem worth a startup warning (and the fallback audit path
            # already skips them; the two must agree).
            if not cfg_bool(item, "enabled", True):
                continue
            kind = str(item.get("__template_key") or item.get("type") or "").lower()
            if kind not in KIND_LABELS:
                logger.warning(
                    "监测项 %r 的类型 %r 不受支持，已跳过（可用类型：%s）",
                    str(item.get("name") or ""),
                    kind or "(空)",
                    "、".join(KIND_LABELS),
                )
                self.skipped_items.append(
                    f"监测项「{self._redactor.mask_name(str(item.get('name') or kind or '(未命名)'))}」"
                    f"的类型「{kind or '(空)'}」不受支持，已跳过探测"
                )
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
            normalized.pop("__key_suffix", None)
            normalized["__template_key"] = kind
            normalized["name"] = str(item.get("name") or kind)
            if isinstance(item.get(required), str):
                normalized[required] = str(item.get(required)).strip()
            if config_error:
                normalized["config_error"] = config_error
            # Two entries with the same probe are the same check: running both
            # interleaved on one shared state slot sent the same alert twice
            # (and, when they disagreed, a "recovered" notice for a service
            # that was still down). Keep the first and say so.
            signature = self._probe_signature(normalized)
            if signature in seen_signatures:
                logger.warning(
                    "监测项 %r（%s）与前面的条目重复，已忽略后面的重复项",
                    normalized["name"],
                    kind,
                )
                self.skipped_items.append(
                    f"监测项「{self._redactor.mask_name(normalized['name'])}」"
                    "与前面的条目完全重复，已忽略"
                )
                continue
            seen_signatures.add(signature)
            normalized_items.append((normalized, signature))

        # Group by the historical key. A key shared by different probes (same
        # kind + name + target but another keyword/expected status) would put
        # them in one state slot; suffix ALL of them, not just the later one,
        # so reordering the config cannot swap which probe owns which state.
        groups: dict[str, list[tuple[dict[str, Any], tuple[str, ...]]]] = {}
        for normalized, signature in normalized_items:
            groups.setdefault(self._item_key(normalized), []).append(
                (normalized, signature)
            )
        for base_key, entries in groups.items():
            if len(entries) < 2:
                # A group that shrank back to one entry (one duplicate was
                # deleted/disabled) must keep its suffixed state key; falling
                # back to the base key would prune the live state and write a
                # fake "monitor removed" recovery for an item that still
                # exists.
                normalized, signature = entries[0]
                suffix = self._signature_suffix(signature)
                if self._state_has_key(f"{base_key}#{suffix}"):
                    normalized["__key_suffix"] = suffix
                elif not self._state_has_key(base_key):
                    # The item was once part of a colliding group and kept a
                    # suffixed key; editing its probe (keyword, expected
                    # status, timeout...) changes the suffix. Dropping the old
                    # slot made a live fault alert a second time from scratch
                    # and left its previous alert row unresolved for the whole
                    # retention window (2026-11-06 audit). Treat a signature
                    # change on the surviving item as an edit and carry the
                    # state over.
                    self._migrate_suffixed_state(base_key)
                    if not self._state_has_key(base_key):
                        self._migrate_truncated_target_state(base_key, normalized)
                continue
            if self._state_has_key(base_key):
                # Legacy shared state from before the keys were split (an
                # upgrade with two colliding entries already alerting). Give
                # it to the first entry instead of letting the prune turn it
                # into a fake "monitor removed" recovery. One-time: the base
                # key is consumed.
                first_signature = entries[0][1]
                target = f"{base_key}#{self._signature_suffix(first_signature)}"
                if not self._state_has_key(target):
                    self._move_state_key(base_key, target)
            for normalized, signature in entries:
                normalized["__key_suffix"] = self._signature_suffix(signature)
            logger.info(
                "监测项 %r 有 %d 个同名同目标但检查参数不同的条目，已分配独立状态键",
                entries[0][0].get("name"),
                len(entries),
            )
        return [normalized for normalized, _signature in normalized_items]

    @staticmethod
    def _signature_suffix(signature: tuple[str, ...]) -> str:
        return hashlib.sha1(repr(signature).encode("utf-8")).hexdigest()[:8]

    # Only the suffix our own key builder appends is stripped. Splitting on
    # the first "#" was wrong: a monitor name may itself contain one
    # ("CPU #1"), and that made two different names look like the same item
    # during prune (v2.20.22).
    _KEY_SUFFIX_RE = re.compile(r"#(?:q)?[0-9a-f]{8}$")

    @classmethod
    def _key_bases(cls, key: str) -> set[str]:
        bases = {key}
        match = cls._KEY_SUFFIX_RE.search(key)
        if match:
            bases.add(key[: match.start()])
        return bases

    def _state_has_key(self, key: str) -> bool:
        try:
            monitors = self.state.get("monitors", {}) or {}
            items = monitors.get("items") if isinstance(monitors, dict) else None
            return isinstance(items, dict) and key in items
        except Exception:
            return False

    def _move_state_key(self, old: str, new: str) -> None:
        """Move one persisted state entry to a new key (one-time migration)."""

        try:
            with self.state.section("monitors") as monitors:
                items = monitors.get("items")
                if isinstance(items, dict) and old in items and new not in items:
                    items[new] = items.pop(old)
            self.state.save()
            logger.info("监测项状态键已从旧共享键迁移：%s -> %s", old, new)
        except Exception:
            logger.debug("迁移监测项状态键失败", exc_info=True)

    def _migrate_suffixed_state(self, base_key: str) -> None:
        """Carry an old suffixed state slot over to ``base_key``.

        Only called when a single-item group no longer finds the slot for its
        current probe signature. That happens after the item was edited while
        it had a colliding sibling and kept a ``#<hash>`` key: without this
        the prune drops a live ``alerting`` slot (second alert from scratch)
        and the old alert row never gets a recovery. Prefer the slot that is
        still alerting, then the most recent one.
        """

        try:
            monitors = self.state.get("monitors", {}) or {}
            items = monitors.get("items") if isinstance(monitors, dict) else None
            if not isinstance(items, dict):
                return
            # Only OUR 8-hex suffix may be carried over. A raw prefix match
            # also caught the "#q<8hex>" key that _item_key builds for an HTTP
            # query, so a new item with the same name/path (no query) stole
            # the live state of another, still-failing item -- a fake
            # "recovered" for a healthy item and a second alert from scratch
            # for the real one (2026-11-06 reverse audit).
            pattern = re.compile(rf"{re.escape(base_key)}#[0-9a-f]{{8}}")
            candidates = [str(key) for key in items if pattern.fullmatch(str(key))]
            # More than one old suffix means the group used to hold several
            # entries and there is no safe way to tell which slot belonged to
            # this one; alerting from scratch is safer than stealing a
            # sibling's live episode.
            if len(candidates) != 1:
                return

            def sort_key(key: str) -> tuple[int, float]:
                state = items.get(key)
                if not isinstance(state, dict):
                    return (0, 0.0)
                return (
                    1 if state.get("alerting") else 0,
                    safe_float(state.get("since") or 0),
                )

            self._move_state_key(max(candidates, key=sort_key), base_key)
        except Exception:
            logger.debug("迁移监测项后缀状态失败", exc_info=True)

    def _migrate_truncated_target_state(
        self, base_key: str, item: dict[str, Any]
    ) -> None:
        """Carry a state slot whose target lost its ``#``/``?`` tail.

        ``redact_url`` used to cut every free-form target at the first
        ``#``/``?``, so a process pattern "python #1" was stored under the
        key built from "python ". The key now keeps the tail; without this
        migration the old slot is pruned (fake "removed" event) and the item
        alerts again from zero on upgrade (2026-11-06 reverse audit).
        """

        try:
            current = self._probe_target(item)
            legacy = current.split("#", 1)[0].split("?", 1)[0].rstrip()
            if not legacy or legacy == current:
                return
            if self._state_has_key(base_key):
                return
            name = str(item.get("name") or "")
            monitors = self.state.get("monitors", {}) or {}
            items = monitors.get("items") if isinstance(monitors, dict) else None
            if not isinstance(items, dict):
                return
            candidates = [
                str(key)
                for key, state in items.items()
                if isinstance(state, dict)
                and str(state.get("name") or "") == name
                and str(state.get("target") or "") == legacy
            ]
            if len(candidates) == 1:
                self._move_state_key(candidates[0], base_key)
        except Exception:
            logger.debug("迁移截断目标状态失败", exc_info=True)

    def _mark_stale_items(self) -> None:
        """Mark items that were alerting while the probe loop was not running.

        ``monitors.enabled=false`` (or a long restart) leaves ``alerting`` and
        ``since`` in state.json. Reporting that whole window as one recovery
        duration would be misleading, so such a recovery only writes the
        event and skips the notification.

        A hard restart inside the aggregation window has the same shape as
        the resource rules: the buffered alert lived only in the aggregator's
        memory, so it is gone, while state.json still says the item was
        "notified". The resource rules already handle this (see
        ``alerts._mark_stale_rules``); the monitor side did not, so a restart
        inside the window lost the alert for good and the later recovery
        announced an outage the user never heard about (v2.20.12).
        """

        changed = False
        with self.state.section("monitors") as monitors:
            items = monitors.get("items") or {}
            if not isinstance(items, dict):
                return
            now = time.time()
            for key, st in items.items():
                if not isinstance(st, dict):
                    continue
                if st.get("recovery_awaits_summary"):
                    # The recovery was queued because its alert sat in a
                    # summary that lived only in the aggregator's memory.
                    # A restart took that summary away, so the recovery must
                    # go with it: otherwise the user receives a "✅ 服务恢复"
                    # for an outage they were never told about (v2.20.12).
                    #
                    # This case is NOT covered by the ``alerting`` check
                    # below: the recovery is queued after the episode was
                    # closed, so ``alerting`` is already False.
                    st.pop("pending_recovery", None)
                    st.pop("recovery_attempt", None)
                    st.pop("recovery_next_attempt", None)
                    st.pop("recovery_retry_count", None)
                    st.pop("recovery_awaits_summary", None)
                    st.pop("summary_key", None)
                    st.pop("summary_reason", None)
                    changed = True
                    logger.info(
                        "监测项 %s 的排队恢复在重启前失去对应的汇总，已撤回", key
                    )
                if not st.get("alerting"):
                    continue
                if st.get("awaiting_summary"):
                    # The buffered summary is gone with the process. Treat
                    # the item like an interrupted direct send: retryable,
                    # not announced.
                    st["alerted"] = False
                    st["summary_abandoned"] = True
                    st.pop("awaiting_summary", None)
                    st.pop("summary_buffered", None)
                    st.pop("summary_key", None)
                    st.pop("summary_reason", None)
                    changed = True
                    logger.info(
                        "监测项 %s 的聚合汇总在重启前未送达，已标记为可重试", key
                    )
                last_checked = safe_float(
                    st.get("last_checked") or st.get("since") or 0
                )
                if last_checked and now - last_checked > 86400:
                    st["stale"] = True
                    changed = True
                    logger.warning(
                        "监测项 %s 已超过 24 小时未探测（可能关闭了监测），"
                        "恢复时只记事件、不发恢复消息",
                        key,
                    )
        # The markers only matter across a restart, so they have to reach
        # the disk: without the save the next process reads the stale
        # "alerted=True" and loses the alert again.
        if changed:
            self.state.save()

    def start(self) -> None:
        if self._running or (self._task is not None and not self._task.done()):
            # start() is not idempotent: a second call used to create a second
            # probe loop, so every item was checked (and alerted) twice.
            logger.debug("服务监测已在运行，忽略重复启动")
            return
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

    @staticmethod
    def _probe_target(item: dict[str, Any]) -> str:
        """The target string ``_probe`` returns for this item (redacted)."""

        kind = str(item.get("__template_key") or "")
        if kind == "http":
            return redact_url(str(item.get("url") or ""))
        if kind == "tcp":
            host = str(item.get("host") or "127.0.0.1").strip()
            return f"{host}:{cfg_int(item, 'port', 0)}"
        if kind == "docker":
            return str(item.get("container") or "").strip()
        if kind == "process":
            return str(item.get("pattern") or "").strip().lower()
        if kind == "systemd":
            return str(item.get("unit") or "").strip()
        return ""

    def _prune_state(self) -> None:
        # No early return on an empty item list: clearing every monitor used
        # to leave its latched alerts open in the daily report until the
        # 30-day window rolled over.
        pending_events: list[tuple[str, str, str, str]] = []
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
                # The event table pairs rows by (kind, name, target), not by
                # the per-item state key. A recovery written here would also
                # close the open alert of a surviving duplicate (same name and
                # target, different keyword/timeout) -- the report then shows
                # a still-failing service as "recovered". Skip the event in
                # that case; the removed item's old row stays until retention,
                # which is safer than a false recovery for a live fault.
                stale_bases = self._key_bases(str(key))
                stale_target = str(state.get("target") or "")
                duplicate_alive = any(
                    stale_bases & self._key_bases(self._item_key(item))
                    # Keys alone miss the TCP case: an omitted host and an
                    # explicit 127.0.0.1 now share a signature (so the second
                    # entry is deduplicated) but still have different state
                    # keys. Comparing the same NAME + probe target keeps the
                    # prune from writing a fake recovery that closes the
                    # surviving entry's open alert row (2026-11-06 reverse
                    # audit). The name check is required: two differently
                    # named Docker items share the container target too.
                    or (
                        stale_target
                        and str(state.get("name") or "") == str(item.get("name") or "")
                        and stale_target == self._probe_target(item)
                    )
                    for item in self.items
                )
                if duplicate_alive:
                    logger.info(
                        "监测项 %r 已移除，但仍有同名同目标的监测项在用，"
                        "跳过自动关闭事件以免误关幸存项",
                        name,
                    )
                    continue
                pending_events.append(
                    (name, target, "监测项已移除，自动关闭", "recovery")
                )
        self.state.save()
        # Write the events after releasing the state lock. add_event is a
        # synchronous sqlite INSERT with a 5 s busy timeout; holding the
        # monitor lock across it blocked the sampler and the alert engine
        # whenever the database was slow (2026-11-06 audit).
        for name, target, detail, status in pending_events:
            self._record_event(name, target, detail, status)
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

    def nudge_queued(self) -> None:
        """Let alerts queued during an outage retry on the next sample.

        While the Bot channel is offline a failed push keeps the alert and
        sets ``next_attempt = now + 900`` so it does not burn its retry
        budget. Once the channel is back, waiting the full 15 minutes made
        the operator see "Bot 已恢复" first and the actual outage alert much
        later (v2.20.21).
        """

        now = time.time()
        with self.state.section("monitors") as monitors:
            items = monitors.get("items")
            if not isinstance(items, dict):
                return
            for st in items.values():
                if not isinstance(st, dict):
                    continue
                if safe_float(st.get("next_attempt") or 0) > now:
                    st["next_attempt"] = 0
                if (
                    st.get("pending_recovery")
                    and safe_float(st.get("recovery_next_attempt") or 0) > now
                ):
                    st["recovery_next_attempt"] = 0

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
            # Still record that a round ran: the dashboard shows ``last_run``
            # as "when the monitor last checked", and leaving the old value
            # made a silently disabled monitor look fresh (v2.20.20).
            with self.state.section("monitors") as monitors:
                monitors["last_run"] = time.time()
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
        if (
            state.get("alerting")
            or safe_int(state.get("fails") or 0) >= self.fail_threshold
        ):
            return dependency
        return None

    def _transport_offline(self) -> bool:
        """True when the bot connection is currently latched offline.

        A failed send while the channel is down proves nothing about the
        monitor item, so the retry budget must not be consumed (v2.20.5).
        """

        try:
            alerts_state = self.state.get("alerts", {}) or {}
            if not isinstance(alerts_state, dict):
                return False
            offline = alerts_state.get("offline")
            return bool(isinstance(offline, dict) and offline.get("alerting"))
        except Exception:
            return False

    async def _check_item(self, item: dict[str, Any]) -> None:
        key = self._item_key(item)
        # Read "this target worked at least once" before probing: a DNS
        # record that disappears or a docker daemon that goes down on a
        # target that used to work is an outage, not a configuration typo
        # (see the downgrade below). ``last_ok`` covers state written before
        # ever_ok existed, so the first failure right after the upgrade is
        # still treated as an outage.
        peek_state = self._peek_state(key)
        ever_ok = bool(peek_state.get("ever_ok")) or bool(peek_state.get("last_ok"))
        ok, detail, target_text = await self._probe(item)
        detail = self._downgrade_config_error(detail, ever_ok)
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
        display_name = self._redactor.mask_name(name)
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
            # The alert may or may not have arrived; retry it once the channel
            # allows instead of staying latched-but-silent for the whole
            # episode (v2.20.8).
            st["summary_abandoned"] = True
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
                st["ever_ok"] = True
                st["last_error"] = ""
                # A successful probe breaks the consecutive-failure streak
                # even inside the maintenance window. Leaving ``fails``
                # behind made the first post-window failure reach the
                # threshold with a stale count and alert on a service that
                # was healthy (v2.20.19).
                st["fails"] = 0
                started = safe_float(st.pop("silent_since", 0) or 0)
                if alerting:
                    # An item that was ALREADY alerting when the window began
                    # heals inside the window as well. The old code only ended
                    # the episode when ``silent_since`` existed, and the
                    # ``else`` branch deliberately never set it for an
                    # alerting item -- so ``alerting`` stayed True forever.
                    # Consequences: the alert row kept no resolved_ts (daily
                    # report showed "未恢复"), and the first post-window
                    # failure looked like the same continuous outage, so it
                    # pushed nothing and wrote no event until the eventual
                    # recovery billed the whole window as downtime.
                    origin = started or safe_float(st.get("since") or 0) or now
                    # ``alerting=True`` already has a matching alert/failed
                    # row: the latch path, the cooldown path and the
                    # interrupted-send repair each write one. Recording a
                    # second "alert" here made ``close_all`` stamp two rows
                    # for ONE episode, so the daily report could show the
                    # same fault twice (v2.20.21).
                    self._record_event(
                        name,
                        target_text,
                        f"持续 {format_duration(now - origin)}（维护静默期内，未推送）",
                        "recovery",
                    )
                    st["alerting"] = False
                    st["since"] = 0
                    st["retry_count"] = 0
                    st["next_attempt"] = 0
                    for stale_key in (
                        "alerted",
                        "pending",
                        "summary_buffered",
                        "summary_key",
                        "summary_reason",
                        "send_exhausted",
                        "awaiting_summary",
                        "recovery_attempt",
                        "recovery_next_attempt",
                        "recovery_retry_count",
                        # ``stale`` only suppresses the recovery notice of
                        # the episode it was written for. Keeping it here
                        # made the NEXT episode's recovery silent, because
                        # this branch already closed the old one (v2.20.17).
                        "stale",
                    ):
                        st.pop(stale_key, None)
                elif started:
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
                    # Close the episode here: leaving ``alerting=True`` made
                    # the first post-window probe take the normal-recovery
                    # branch and push a stale "已恢复" (with the whole
                    # maintenance window counted as the outage) plus a second
                    # recovery event that inflated the daily-report duration.
                    st["alerting"] = False
                    st["since"] = 0
                    st.pop("alerted", None)
                    st.pop("pending", None)
                    # Same reason as the alerting branch above: the episode
                    # is over, so an old "stale recovery" marker must not
                    # survive into the next one.
                    st.pop("stale", None)
            else:
                st["last_error"] = detail
                # Only a *new* fault gets a silence-window start marker. An
                # item that was already alerting before the window started
                # must keep its original episode: marking it here produced a
                # second alert+recovery pair at the end of the window (the
                # daily report then showed an extra row) and claimed the
                # outage was never pushed even though it was.
                if not st.get("alerting"):
                    st.setdefault("silent_since", now)
            states[key] = st
            return

        # A fault that became healthy right at the end of the window: record
        # it with the real start time instead of dropping the episode. A
        # fault that outlives the window keeps its start time and simply
        # rejoins the normal state machine below (v2.20.0: the start used
        # to be dropped, so the pre-silence outage vanished from the log).
        silent_since = safe_float(st.pop("silent_since", 0) or 0)
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
        elif silent_since and not ok and not alerting:
            # The fault started inside the maintenance window and outlived
            # it. This branch used to drop the marker, so the alert that
            # followed took the push moment as the start: the recovery
            # notice and the daily report collapsed a ten-minute outage into
            # “中断时长 00:00:00” (v2.20.17).
            st["since"] = silent_since

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
            # A fault latched inside the cooldown was recorded but never
            # pushed. If it is still failing when the cooldown expires the
            # operator has never seen it: push the alert now, mirroring the
            # resource-rule ``cooldown_retry`` (the previous version latched
            # the episode and then went permanently silent, so a sustained
            # outage produced neither an alert nor a recovery notice).
            abandoned = bool(st.pop("summary_abandoned", False))
            cooldown_retry = (
                alerting
                and not bool(st.get("alerted"))
                and safe_int(st.get("retry_count") or 0) == 0
                and safe_float(st.get("next_attempt") or 0) == 0
                and (
                    abandoned
                    or (
                        safe_float(st.get("last_sent") or 0) > 0
                        and now - safe_float(st.get("last_sent") or 0) >= self.cooldown
                    )
                )
            )
            will_alert = (not alerting and fails >= self.fail_threshold) or (
                alerting and cooldown_retry
            )
            st["fails"] = fails
            st["last_error"] = detail
            st["last_checked"] = now
            if will_alert:
                next_attempt = safe_float(st.get("next_attempt") or 0)
                if last_sent and now - last_sent < self.cooldown and not abandoned:
                    # Inside the cooldown: latch the episode and record one
                    # event (with the suppression noted) instead of vanishing.
                    # Staying silent here meant an entire outage never reached
                    # the event log and the recovery notice later arrived
                    # without any matching alert -- the defect v2.18.1 fixed
                    # for the resource rules.
                    st["alerting"] = True
                    st["alerted"] = False
                    # Keep an existing episode start (a fault that began in
                    # the maintenance window, or an earlier suppressed
                    # sample): overwriting it billed only the time since the
                    # suppression and shrank the reported duration. The
                    # resource path already preserves it (v2.20.21).
                    if not safe_float(st.get("since") or 0):
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
                    # The queued recovery is folded into this alert, so it
                    # no longer waits for a summary. A stale marker made the
                    # next abandon callback withdraw the recovery of an
                    # unrelated episode (v2.20.12).
                    st.pop("recovery_awaits_summary", None)
                    text = (
                        f"🔴 服务异常 · {display_name}\n"
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
                    result = await self._push_alert(
                        name,
                        detail,
                        text,
                        target_text,
                        item_key=key,
                        display_name=display_name,
                    )
                    buffered = result is None
                    delivered = (not buffered) and self._delivered(result)
                    st.pop("send_inflight", None)
                    st["alerting"] = True
                    # The episode counts as announced even when the push
                    # failed: the recovery notice carries the "（异常告警此前
                    # 未能送达）" note, so the user still learns that the
                    # service was down. Suppressing the notice instead left
                    # the user with NO message at all when the first push
                    # failed and the service recovered before the retry
                    # budget ran out (v2.20.14).
                    st["alerted"] = True
                    # Keep the episode's ORIGINAL start. Overwriting it with
                    # the push moment moved the start forward whenever the
                    # alert was not sent right away (a cooldown retry, a
                    # failed first attempt): the recovery notice then showed
                    # "故障开始 = 恢复时刻" and an interruption duration of
                    # 00:00:00 for an outage that had been running for
                    # half an hour (v2.20.14). The resource rules keep
                    # ``alert_since`` for the same reason.
                    if not safe_float(st.get("since") or 0):
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
                        # and the callbacks match by name -- without the
                        # marker they also touched the item whose alert
                        # was delivered on its own.
                        st["summary_buffered"] = True
                        # The exact monitor key this alert was buffered
                        # under; two items may share a name.
                        st["summary_key"] = key
                        # Match the delivered summary by (name, reason): two
                        # monitors can share a name, and one item's summary
                        # must not clear the other's queued recovery
                        # (v2.20.19).
                        st["summary_reason"] = detail
                        self._record_event(name, target_text, detail, "alert")
                    elif delivered:
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        # The channel works again: the next failure must
                        # start at the fast step of the backoff (v2.20.24).
                        st.pop("offline_retry_count", None)
                        # The alert went out on its own; the buffered marker
                        # (if a previous episode ever buffered) must go or a
                        # later same-name abandon would withdraw this item's
                        # "notified" state (v2.20.0).
                        st.pop("summary_buffered", None)
                        st.pop("summary_key", None)
                        st.pop("summary_reason", None)
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        if self._transport_offline() or delivery_offline(result):
                            # The channel is down, so this failure says
                            # nothing about the monitor item: keep the alert
                            # queued without spending the retry budget
                            # (v2.20.5). Throttle the polling after a few
                            # rounds (v2.20.24): the first attempt used to
                            # retry after 30 seconds and the target the
                            # direct path rejects while the framework
                            # returns False was polled at that rate.
                            offline_attempt = (
                                safe_int(st.get("offline_retry_count") or 0) + 1
                            )
                            st["offline_retry_count"] = offline_attempt
                            st["next_attempt"] = now + offline_retry_delay(
                                offline_attempt
                            )
                            if offline_retry_delay(
                                offline_attempt
                            ) != offline_retry_delay(offline_attempt - 1):
                                logger.warning(
                                    "监测项 %s 告警连续 %d 次因连接不可用未能送达，已改为每小时重试（恢复后仍会补发）",
                                    name,
                                    offline_attempt - 1,
                                )
                        else:
                            st.pop("offline_retry_count", None)
                            retry_count = safe_int(st.get("retry_count") or 0) + 1
                            st["retry_count"] = retry_count
                            st["next_attempt"] = now + retry_delay(retry_count)
                        self._record_event(name, target_text, detail, "failed")
            elif alerting and pending:
                next_attempt = safe_float(st.get("next_attempt") or 0)
                if now >= next_attempt:
                    text = (
                        f"🔴 服务异常 · {display_name}\n"
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
                        st.pop("offline_retry_count", None)
                        st.pop("summary_buffered", None)
                        st.pop("summary_key", None)
                        st.pop("summary_reason", None)
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        if self._transport_offline() or delivery_offline(result):
                            # The channel is down, so this failure says
                            # nothing about the monitor item: keep the alert
                            # queued without spending the retry budget
                            # (v2.20.5). Throttle the polling after a few
                            # rounds (v2.20.24): a channel that never comes
                            # back was retried every 15 minutes forever.
                            offline_attempt = (
                                safe_int(st.get("offline_retry_count") or 0) + 1
                            )
                            st["offline_retry_count"] = offline_attempt
                            st["next_attempt"] = now + offline_retry_delay(
                                offline_attempt
                            )
                            if offline_retry_delay(
                                offline_attempt
                            ) != offline_retry_delay(offline_attempt - 1):
                                logger.warning(
                                    "监测项 %s 告警连续 %d 次因连接不可用未能送达，已改为每小时重试（恢复后仍会补发）",
                                    name,
                                    offline_attempt - 1,
                                )
                        else:
                            st.pop("offline_retry_count", None)
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
            st["ever_ok"] = True
            st["last_error"] = ""
            # A fault that began inside the maintenance window and cleared
            # BEFORE reaching the threshold left its start in ``since``. The
            # next real outage then inherited that stale start and its
            # recovery notice reported "中断时长: 04:06:00" for a 2-minute
            # fault (v2.20.20). The recovery branch above already resets it;
            # this covers the never-alerted case.
            if not alerting:
                st["since"] = 0
            # The abandoned-summary marker belongs to the episode that just
            # ended. Keeping it made the NEXT failure bypass the cooldown and
            # push immediately, while the resource rules clear it here
            # (v2.20.17 audit, same asymmetry as the resource side).
            st.pop("summary_abandoned", None)
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
                if st.get("recovery_awaits_summary") and self._aggregator is not None:
                    # The recovery may only be posted once the summary that
                    # carries its alert reached the chat. The notice was
                    # scheduled for "window end + 3s", but the summary is
                    # only *sent* after the window closes and may still be in
                    # flight or waiting for its retry backoff -- so the
                    # notice could overtake it and announce a recovery for an
                    # outage the user was never told about (v2.20.14).
                    if self._aggregator.summary_pending("service"):
                        next_recovery = now + 15.0
                        st["recovery_next_attempt"] = next_recovery
                    else:
                        # The summary is gone: delivered or abandoned. Either
                        # way the recovery may go out now.
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
                        # behind made the next abandon callback withdraw the
                        # recovery of an unrelated episode (v2.20.12).
                        st.pop("recovery_awaits_summary", None)
                    else:
                        if self._transport_offline() or delivery_offline(result):
                            # Keep the notice queued without spending its
                            # retry budget, and throttle the polling after a
                            # few rounds (v2.20.24).
                            offline_attempt = (
                                safe_int(st.get("recovery_offline_count") or 0) + 1
                            )
                            st["recovery_offline_count"] = offline_attempt
                            st["recovery_next_attempt"] = now + offline_retry_delay(
                                offline_attempt
                            )
                            if offline_retry_delay(
                                offline_attempt
                            ) != offline_retry_delay(offline_attempt - 1):
                                logger.warning(
                                    "监测项 %s 恢复通知连续 %d 次因连接不可用未能送达，已改为每小时重试",
                                    name,
                                    offline_attempt - 1,
                                )
                        else:
                            st.pop("recovery_offline_count", None)
                            retry_count = (
                                safe_int(st.get("recovery_retry_count") or 0) + 1
                            )
                            if retry_count > MAX_RECOVERY_RETRIES:
                                # A permanently unreachable target would otherwise
                                # retry this notice every ten minutes until the
                                # process restarts.
                                for stale_key in (
                                    "pending_recovery",
                                    "recovery_attempt",
                                    "recovery_next_attempt",
                                    "recovery_retry_count",
                                    "recovery_offline_count",
                                ):
                                    st.pop(stale_key, None)
                                logger.warning(
                                    "监测项 %s 恢复通知连续 %d 次发送失败，已停止重试",
                                    name,
                                    MAX_RECOVERY_RETRIES,
                                )
                            else:
                                st["recovery_retry_count"] = retry_count
                                st["recovery_next_attempt"] = now + retry_delay(
                                    retry_count
                                )
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
                # The episode is over: the offline backoff belongs to it.
                # Keeping it made the next fault of this item start at the
                # slow hourly step (v2.20.24 audit).
                st.pop("offline_retry_count", None)
                # Episode is over: the buffered marker must not survive into
                # the next one, or a later same-name abandon would withdraw
                # this item's "notified" state (v2.20.0). Read it first: only
                # an alert that was actually buffered may queue its recovery
                # behind the summary (v2.20.17).
                buffered = bool(st.pop("summary_buffered", None))
                # Persist the closed episode *before* the slow send: a
                # reload/crash during delivery used to leave "alerting=True"
                # on disk and the next sample replayed the recovery notice
                # (a second "已恢复"). The offline path already did this
                # (v2.20.5); the service-monitor recovery branch did not
                # (v2.20.21).
                self.state.save()
                # The event log does not depend on the notification pref: the
                # daily report's interruption durations and unresolved list
                # rely on resolved_ts being written on every recovery.
                self._record_event(name, target_text, detail, "recovery")
                # Pop unconditionally: leaving it set across a
                # recover_notify=False recovery made the NEXT healthy
                # recovery claim an undelivered alert out of nowhere.
                exhausted = bool(st.pop("send_exhausted", False))
                # Pop unconditionally, exactly like the resource rules: this
                # marker describes one episode's alert, and a recovery that is
                # not announced (recover_notify off / stale / suppressed) must
                # not leave it behind. Leaving it made the NEXT recovery queue
                # behind an unrelated open summary, and that summary's abandon
                # then deleted the queued notice -- no recovery ever reached
                # the user (v2.20.17).
                awaiting = st.pop("awaiting_summary", False)
                if self.recover_notify and not stale and notified:
                    had_failed = exhausted or bool(pending)
                    extra = "\n（异常告警此前未能送达）" if had_failed else ""
                    text = (
                        f"✅ 服务恢复 · {display_name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"故障开始: {_fmt_ts(now - duration, self.config)}\n"
                        f"恢复时间: {_now_text(self.config)}\n"
                        f"中断时长: {format_duration(duration)}{extra}"
                    )
                    remaining = (
                        self._aggregator.window_remaining("service")
                        if self._aggregator is not None
                        else 0.0
                    )
                    # ``window_remaining`` hits zero the moment the window
                    # closes, but the summary may still be buffered, in
                    # flight, or waiting for its retry backoff. Posting the
                    # recovery in that gap tells the user about an outage
                    # whose alert summary has not arrived yet (v2.20.20,
                    # same gap the resource side had; ``summary_pending`` is
                    # the criterion the send path already uses).
                    summary_pending = (
                        self._aggregator.summary_pending("service")
                        if self._aggregator is not None
                        else False
                    )
                    if awaiting and buffered and (remaining > 0 or summary_pending):
                        # This item's alert is still inside the aggregation
                        # window. Posting the recovery now would let it beat
                        # the summary to the chat; queue it so the summary is
                        # posted first. On abandon the queued notice is
                        # withdrawn together with the summary, because the
                        # user never learned about the outage (v2.20.12).
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + max(15.0, remaining + 3.0)
                        # This recovery may only be posted if the summary
                        # that carries its alert actually reaches the chat.
                        st["recovery_awaits_summary"] = True
                        logger.info(
                            "监测项 %s 在聚合窗口内恢复，恢复通知延后 %.0f 秒发送",
                            name,
                            max(15.0, remaining + 3.0),
                        )
                    else:
                        st.pop("summary_key", None)
                        st.pop("summary_reason", None)
                        result = await self._send(text)
                        if not self._delivered(result):
                            st["pending_recovery"] = text
                            st["recovery_retry_count"] = 1
                            st["recovery_next_attempt"] = now + retry_delay(1)
                            # This notice does not depend on a buffered
                            # summary: an abandoned summary must not
                            # withdraw it.
                            st["recovery_awaits_summary"] = False
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
            "summary_key",
            "summary_reason",
            "send_exhausted",
            "stale",
            "silent_since",
            # A recovery notice that was queued before the config broke
            # describes an incident that is already over: delivering it after
            # the operator fixed the config sent a stale "已恢复" with the
            # old timestamps (and the event log kept two recovery rows).
            "pending_recovery",
            "recovery_attempt",
            "recovery_next_attempt",
            "recovery_retry_count",
            "recovery_awaits_summary",
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
                f"✅ 服务告警关闭 · {self._redactor.mask_name(name)}\n"
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

    def _peek_state(self, key: str) -> dict[str, Any]:
        """Return the persisted state of one monitor key (never raises)."""

        try:
            monitors = self.state.get("monitors")
            if isinstance(monitors, dict):
                items = monitors.get("items")
                if isinstance(items, dict):
                    state = items.get(key)
                    if isinstance(state, dict):
                        return state
        except Exception:
            pass
        return {}

    @staticmethod
    def _downgrade_config_error(detail: str, ever_ok: bool) -> str:
        """Turn a transient-looking config error back into a real outage.

        A target that worked before is not "misconfigured" just because its
        DNS record disappeared or the docker daemon went down. Done outside
        ``_probe`` so the probe stays a one-argument call that the existing
        test doubles can replace.
        """

        if not ever_ok or not detail.startswith(CONFIG_ERROR_PREFIX):
            return detail
        raw = detail[len(CONFIG_ERROR_PREFIX) :]
        return raw if _looks_like_transient_probe_error(raw) else detail

    async def _probe(self, item: dict[str, Any]) -> tuple[bool, str, str]:
        kind = str(item.get("__template_key") or "")
        timeout = max(1, min(60, cfg_int(item, "timeout_seconds", 10)))
        if kind == "http":
            url = str(item.get("url") or "").strip()
            if not url:
                return False, "URL 未配置", "-"
            expected = cfg_int(item, "expected_status", 0)
            keyword = str(item.get("keyword") or "").strip()
            # Set once the soft limit is crossed: a timeout after that point
            # means "we could not read enough to decide", not "the service is
            # down". The old short-circuit answered PROBE_ERROR immediately;
            # reading further must not turn a slow, large page into an outage
            # alert (2026-11-06 reverse audit).
            read_state: dict[str, Any] = {"soft_limit_hit": False, "soft_limit": 0}
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
                            # ``max_response_bytes`` is a soft limit: it keeps
                            # the common case small, but the probe keeps
                            # reading (up to a hard 10 MiB ceiling) when the
                            # keyword was not in the first slice. Cutting at
                            # the soft limit made a keyword in the page footer
                            # "unknown" forever; an item that had been alerting
                            # never closed, so the daily report kept an
                            # unresolved row (2026-11-06 audit).
                            soft_limit = max(
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
                            hard_limit = 10 * 1024 * 1024
                            read_state["soft_limit"] = soft_limit
                            encoding = resp.charset_encoding or "utf-8"

                            def _decode(raw: bytearray) -> str:
                                try:
                                    return bytes(raw).decode(encoding, errors="replace")
                                except LookupError:
                                    return bytes(raw).decode("utf-8", errors="replace")

                            body = bytearray()
                            truncated = False
                            found = False
                            next_check = soft_limit
                            async for chunk in resp.aiter_bytes():
                                body.extend(chunk)
                                if len(body) >= soft_limit:
                                    read_state["soft_limit_hit"] = True
                                if len(body) >= hard_limit:
                                    del body[hard_limit:]
                                    truncated = True
                                if len(body) >= next_check:
                                    if keyword in _decode(body):
                                        found = True
                                        break
                                    # Re-check once per extra MiB: decoding
                                    # the whole buffer on every 8 KiB chunk
                                    # would be quadratic.
                                    next_check = len(body) + 1024 * 1024
                                if truncated:
                                    break
                            if not found:
                                found = keyword in _decode(body)
                            if not found:
                                if truncated:
                                    return (
                                        False,
                                        f"{PROBE_ERROR_PREFIX}响应超过 {hard_limit} 字节，"
                                        + "未能读取完整正文，无法确认关键字",
                                        url,
                                    )
                                return False, "响应中未找到关键字", url
                        ms = (time.perf_counter() - started) * 1000
                        return True, f"HTTP {status_code}（{ms:.0f}ms）", url

                ok, detail, target_text = await asyncio.wait_for(
                    _do_probe(), timeout=timeout + 2
                )
                return ok, detail, target_text
            except TimeoutError:
                if read_state["soft_limit_hit"]:
                    # We already read past max_response_bytes and only the
                    # extended keyword search timed out: the service answered,
                    # the body was just too slow/large to finish. Treat it as
                    # inconclusive (no alert), matching the old short-circuit.
                    return (
                        False,
                        (
                            f"{PROBE_ERROR_PREFIX}读取超时（响应超过 "
                            f"{read_state['soft_limit']} 字节），无法确认关键字"
                        ),
                        url,
                    )
                return False, f"探测超时（超过 {timeout + 2} 秒）", url
            except httpx.TimeoutException:
                # httpx raises ReadTimeout/ConnectTimeout, which are NOT the
                # builtin TimeoutError. The generic branch stringified them as
                # "ReadTimeout: " (empty message), so the alert body had no
                # hint at all (v2.20.19). The same extended-read caveat as
                # above applies: a slow large body is inconclusive.
                if read_state["soft_limit_hit"]:
                    return (
                        False,
                        (
                            f"{PROBE_ERROR_PREFIX}读取超时（响应超过 "
                            f"{read_state['soft_limit']} 字节），无法确认关键字"
                        ),
                        url,
                    )
                return False, f"探测超时（超过 {timeout} 秒）", url
            except Exception as exc:
                detail = f"{exc.__class__.__name__}: {exc}"
                if _looks_like_config_error(detail):
                    # A missing scheme or a misspelled host is a configuration
                    # mistake, not an outage: the docker/systemd branches
                    # already classified it, but HTTP/TCP never called the
                    # helper, so a typo produced repeated "🔴 服务异常" alerts
                    # (v2.20.5; the v2.20.4 changelog claimed this fix while
                    # the classification was never wired here).
                    detail = CONFIG_ERROR_PREFIX + detail
                return False, detail, url

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
            except TimeoutError:
                # A black-holed port raises TimeoutError with no message;
                # the generic branch showed "TimeoutError: " (v2.20.19).
                return False, f"探测超时（超过 {timeout} 秒）", target
            except Exception as exc:
                detail = f"{exc.__class__.__name__}: {exc}"
                if _looks_like_config_error(detail):
                    detail = CONFIG_ERROR_PREFIX + detail
                return False, detail, target

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
                    # A localized docker/systemd may print bytes the current
                    # locale cannot decode. Without errors="ignore" the reader
                    # thread fails, stdout becomes None, and the item latched
                    # a fake outage with an unhelpful
                    # "'NoneType' object has no attribute 'strip'" reason
                    # (2026-11-06 audit).
                    errors="ignore",
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
                raw = (proc.stdout or "").strip()
                if not raw:
                    return (
                        False,
                        f"{PROBE_ERROR_PREFIX}docker inspect 无输出",
                        container,
                    )
                running = raw.lower() == "true"
                return running, f"容器 running={running}", container
            except subprocess.TimeoutExpired:
                return (
                    False,
                    f"{PROBE_ERROR_PREFIX}docker inspect 超过 {timeout}s 未返回",
                    container,
                )
            except Exception as exc:
                # A missing docker executable is a deployment/configuration
                # problem, not an outage. Without the classifier the item
                # stayed red forever and every report kept an "unresolved"
                # row; the hint list already contained "[winerror 2]" but
                # this branch never consulted it (v2.20.20).
                detail = f"{exc.__class__.__name__}: {exc}"
                if _looks_like_config_error(detail):
                    detail = CONFIG_ERROR_PREFIX + detail
                return False, detail, container

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
                detail = f"{exc.__class__.__name__}: {exc}"
                # A sandboxed/restricted account can fail the whole scan
                # (psutil.Error / PermissionError), not just one process.
                # The other four kinds classify that as a configuration
                # problem (warn once, never alert); this branch used to
                # alert forever on an environment the operator cannot fix.
                if _looks_like_config_error(detail):
                    detail = f"{CONFIG_ERROR_PREFIX}{detail}"
                return False, detail, pattern

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
                    # Same decode guard as the docker branch: a non-UTF-8
                    # locale turned stdout into None and the item alerted
                    # forever with "unit: unknown" (2026-11-06 audit).
                    errors="ignore",
                    timeout=timeout,
                )
                state_text = (proc.stdout or proc.stderr or "").strip()
                if not state_text:
                    return (
                        False,
                        f"{PROBE_ERROR_PREFIX}systemctl 无输出",
                        unit,
                    )
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
                            errors="ignore",
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
                # Same as the docker branch: a missing systemctl is a
                # configuration problem, not an outage (v2.20.20).
                detail = f"{exc.__class__.__name__}: {exc}"
                if _looks_like_config_error(detail):
                    detail = CONFIG_ERROR_PREFIX + detail
                return False, detail, unit

        return False, f"未知监测类型: {kind}", "-"

    @staticmethod
    def _probe_signature(item: dict[str, Any]) -> tuple[str, ...]:
        """Everything that can change what one monitor item actually checks.

        Used to drop exact duplicate entries and to tell apart two entries
        that share ``kind + name + target`` but probe differently (the same
        URL with a different keyword or expected status). ``show_advanced``
        and other display-only fields are deliberately excluded so a copied
        entry is still recognised as a duplicate.
        """

        kind = str(item.get("__template_key") or "")
        name = str(item.get("name") or "")
        # Normalise the values exactly like the probe does: an omitted
        # timeout and an explicit "10" behave identically, so they must be
        # the same signature (otherwise a copied entry gets a second state
        # slot and alerts twice).
        timeout = str(max(1, min(60, cfg_int(item, "timeout_seconds", 10))))
        if kind == "http":
            return (
                kind,
                name,
                str(item.get("url") or ""),
                str(item.get("keyword") or ""),
                str(cfg_int(item, "expected_status", 0)),
                str(cfg_bool(item, "verify_tls", True)),
                str(item.get("suppress_when_down") or "").strip(),
                timeout,
            )
        if kind == "tcp":
            # The probe defaults an omitted host to 127.0.0.1, so the
            # signature must do the same: a copied item that spells the
            # default out looked like a second check and alerted beside the
            # original for the same port (2026-11-06 audit).
            return (
                kind,
                name,
                str(item.get("host") or "127.0.0.1"),
                str(cfg_int(item, "port", 0)),
                timeout,
            )
        if kind == "docker":
            return (
                kind,
                name,
                str(item.get("container") or ""),
                timeout,
            )
        if kind == "process":
            return (kind, name, str(item.get("pattern") or ""))
        if kind == "systemd":
            return (
                kind,
                name,
                str(item.get("unit") or ""),
                timeout,
            )
        return (kind, name)

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
            base = f"{kind}:{name}:{key_target}"
        else:
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
            base = f"{kind}:{name}:{redact_url(target)}"
        # A same-kind/name/target entry with a different probe (keyword,
        # expected status, timeout) gets its own state slot. The suffix is
        # only present on those later duplicates, so existing state keys keep
        # working after an upgrade.
        suffix = str(item.get("__key_suffix") or "")
        if suffix:
            base += "#" + suffix
        return base

    def _targets_for(self, kind: str = "service") -> list[str]:
        picked = resolve_targets(self.config, kind)
        return self.targets if picked is None else picked

    async def _send(self, text: str) -> DeliveryResult:
        if self._is_silenced():
            # The maintenance window promises "probe only, never push". The
            # per-item paths already return before reaching a send, but a
            # summary whose aggregation window was opened just before the
            # silence began (or a retry queued earlier) used to slip through
            # here. Report failure, not success: a swallowed summary must not
            # mark its rules "notified", or their later recovery would claim
            # an alert the user never saw. The aggregator retries twice and
            # then abandons the batch, which withdraws ``alerted``.
            logger.info("服务监测维护静默中，已跳过一条推送")
            return DeliveryResult(ok=False)
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
        self,
        name: str,
        detail: str,
        text: str,
        target_text: str,
        item_key: str = "",
        display_name: str | None = None,
    ) -> Any | None:
        """Send the first alert of an episode, or buffer it in the window.

        Returns ``None`` when the aggregator buffered the item; the summary
        covers it and no per-item retry is needed.

        ``name`` stays the raw label because the summary callbacks match the
        owning state entry by it. ``display_name`` is what the summary text
        shows, so a name that contains an internal host/URL is masked without
        breaking that matching.
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
            on_delivered=self._on_summary_delivered,
            item_key=item_key,
            display_label=display_name,
        )

    def _on_summary_delivered(self, items: list[tuple[str, str]]) -> None:
        """Clear the "waiting for the summary" markers after it lands.

        A buffered monitor alert counts as announced once its summary reaches
        the chat. Keeping ``awaiting_summary``/``summary_buffered`` behind
        made the reload repair treat a delivered summary as lost: it
        withdrew ``alerted``, so the still-failing item alerted again and a
        recovered one lost its recovery notice (v2.20.17).
        """

        parts = [summary_item_parts(item) for item in items]
        labels = {label for label, _reason, _key in parts if label}
        pairs = {(label, reason) for label, reason, _key in parts}
        keys = {key for _label, _reason, key in parts if key}
        if not labels:
            return
        changed = False
        with self.state.section("monitors") as monitors:
            states = monitors.get("items")
            if not isinstance(states, dict):
                return
            for st in states.values():
                if not isinstance(st, dict):
                    continue
                label = str(st.get("name") or "")
                if label not in labels:
                    continue
                # Prefer the exact item key carried by the batch. Older
                # batches have no key; fall back to the buffered
                # (name, reason). A different same-named item's summary must
                # not clear this item's queued recovery (v2.20.19).
                key = str(st.get("summary_key") or "")
                if keys and key:
                    if key not in keys:
                        continue
                else:
                    reason = str(st.get("summary_reason") or "")
                    if reason and (label, reason) not in pairs:
                        continue
                st.pop("summary_key", None)
                st.pop("summary_reason", None)
                # A monitor that recovered inside the aggregation window is
                # no longer ``alerting``, but its queued recovery still waits
                # for this summary. Clear the marker here: the summary DID
                # reach the chat, so a later reload must not mistake the
                # recovery for an orphan and delete it (v2.20.19).
                if st.pop("recovery_awaits_summary", False):
                    changed = True
                if not st.get("alerting"):
                    continue
                # Only items whose alert was actually buffered; a same-named
                # item that delivered its own alert already cleared this.
                if not st.pop("summary_buffered", None):
                    continue
                st.pop("awaiting_summary", None)
                changed = True
        if changed:
            self.state.save()

    def _on_summary_abandoned(self, items: list[tuple[str, str]]) -> None:
        """Withdraw "alerted" for monitors whose summary never arrived.

        ``last_sent`` is KEPT: ``cooldown_retry`` needs it as the "this
        episode was announced" reference. Clearing it (the docstring used to
        claim it goes with the withdrawal) left the latched item with no path
        back to a push, so the whole outage stayed silent (v2.20.8).
        ``summary_abandoned`` marks the withdrawn state and lets the next
        tick retry immediately.
        """

        parts = [summary_item_parts(item) for item in items]
        labels = {label for label, _reason, _key in parts if label}
        pairs = {(label, reason) for label, reason, _key in parts}
        keys = {key for _label, _reason, key in parts if key}
        if not labels:
            return
        changed = False
        with self.state.section("monitors") as monitors:
            states = monitors.get("items")
            if not isinstance(states, dict):
                return
            for st in states.values():
                if not isinstance(st, dict):
                    continue
                label = str(st.get("name") or "")
                if label not in labels:
                    continue
                key = str(st.get("summary_key") or "")
                if keys and key:
                    if key not in keys:
                        continue
                else:
                    reason = str(st.get("summary_reason") or "")
                    if reason and (label, reason) not in pairs:
                        # Another monitor with the same name owns this summary.
                        continue
                st.pop("summary_key", None)
                st.pop("summary_reason", None)
                # A recovery that was queued because its alert sat in the
                # summary may only be posted when that summary reaches the
                # chat. The summary never arrived, so the recovery goes with
                # it: otherwise the user receives a "✅ 服务恢复" for an
                # outage they were never told about.
                #
                # This check comes BEFORE the ``summary_buffered`` gate: the
                # recovery path clears that marker when the episode closes,
                # so the gate below would skip the item and the queued
                # recovery would survive (v2.20.12).
                if st.get("recovery_awaits_summary"):
                    st.pop("pending_recovery", None)
                    st.pop("recovery_attempt", None)
                    st.pop("recovery_next_attempt", None)
                    st.pop("recovery_retry_count", None)
                    st.pop("recovery_awaits_summary", None)
                    changed = True
                if not st.get("alerting"):
                    continue
                # Only withdraw items that were actually buffered: a
                # same-named item whose alert went out on its own keeps
                # its "notified" state.
                if not st.pop("summary_buffered", None):
                    continue
                st["alerted"] = False
                # Keep ``last_sent``: ``cooldown_retry`` needs it as the
                # "this episode was announced" reference. A summary that
                # never arrived used to clear it, and the latched item then
                # had no path back to a push -- no alert during the whole
                # outage and no recovery notice either (v2.20.8). The
                # marker lets the next tick retry immediately.
                st["summary_abandoned"] = True
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
                    "kind_key": str(item.get("__template_key") or ""),
                    "target": "",
                    "ok": False,
                    "detail": f"配置不完整（{error}），未执行探测",
                }
            peek_state = self._peek_state(self._item_key(item))
            ever_ok = bool(peek_state.get("ever_ok")) or bool(peek_state.get("last_ok"))
            ok, detail, target = await self._probe(item)
            # The manual "check now" command must agree with the alert engine:
            # without the same downgrade it printed "配置错误" for a target the
            # loop was already treating as an outage (2026-11-06 reverse
            # audit).
            detail = self._downgrade_config_error(detail, ever_ok)
            return {
                "name": str(item.get("name") or ""),
                "kind": KIND_LABELS.get(str(item.get("__template_key")), "?"),
                "kind_key": str(item.get("__template_key") or ""),
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
                    # Stable key for the English dashboard: KIND_LABELS has
                    # a Chinese "进程" that leaked into en-US (v2.20.17).
                    "kind_key": str(item.get("__template_key") or ""),
                    "target": self._item_key(item),
                    "state": item_states.get(self._item_key(item), {}),
                    "config_error": item.get("config_error"),
                }
                for item in self.items
            ],
            "last_run": monitors.get("last_run"),
        }
