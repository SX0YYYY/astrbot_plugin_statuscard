from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime
from typing import Any

import httpx
import psutil
from astrbot.api import logger

from .collectors import cfg_bool, cfg_get, cfg_int, format_duration, now_in_timezone
from .delivery import DeliveryResult
from .state import StateStore
from .utils import clamp_config, normalize_targets, redact_url, retry_delay

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


def _now_text(config: Any = None) -> str:
    if config is None:
        return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return now_in_timezone(config).strftime("%Y-%m-%d %H:%M:%S")


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
    ):
        self.plugin = plugin
        self.state = state
        self.config = config
        self.report_store = report_store
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
            if not item.get(required):
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

        items = (self.state.data.get("monitors") or {}).get("items") or {}
        if not isinstance(items, dict):
            return
        now = time.time()
        for key, st in items.items():
            if not isinstance(st, dict) or not st.get("alerting"):
                continue
            last_checked = float(st.get("last_checked") or st.get("since") or 0)
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
        elif not self.targets:
            logger.error(
                "服务监测已启用但没有推送目标（monitors.targets 与 alerts.targets 均为空），"
                "监测项不会探测也不会告警；请在配置页的“告警与每日定时状态图 → 推送目标”里填写"
            )
        self._prune_state()
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
        if not self.items:
            return
        monitors = self.state.data.setdefault("monitors", {})
        states = monitors.setdefault("items", {})
        valid_keys = {self._item_key(item) for item in self.items}
        stale_keys = [key for key in states if key not in valid_keys]
        if not stale_keys:
            return
        for key in stale_keys:
            states.pop(key, None)
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
        if not self.targets:
            # A missing push target means alerts could never reach anyone;
            # warn once at start-up (see audit) instead of every round.
            if not self._targets_warned:
                logger.error(
                    "服务监测已启用但没有推送目标（monitors.targets 与 alerts.targets 均为空），"
                    "不会探测也不会告警；请在配置页填写推送目标"
                )
                self._targets_warned = True
            return
        results = await asyncio.gather(
            *(
                self._check_item(item)
                for item in self.items
                if not item.get("config_error")
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("服务监测检查异常: %s", result)
        self.state.data.setdefault("monitors", {})["last_run"] = time.time()
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
        monitors = self.state.data.setdefault("monitors", {})
        states = monitors.setdefault("items", {})
        st = states.setdefault(key, {})
        fails = int(st.get("fails") or 0)
        alerting = bool(st.get("alerting"))
        pending = bool(st.get("pending"))
        last_sent = float(st.get("last_sent") or 0)
        name = str(item.get("name") or item.get("__template_key") or "服务")
        kind = KIND_LABELS.get(str(item.get("__template_key")), "服务")

        if self._is_silenced():
            # Keep the check history fresh but freeze alert state so the
            # maintenance window never emits alerts or recovery messages.
            st["last_checked"] = now
            if ok:
                st["last_ok"] = now
                st["last_error"] = ""
            else:
                st["last_error"] = detail
            states[key] = st
            return

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
            fails += 1
            will_alert = not alerting and fails >= self.fail_threshold
            st["fails"] = fails
            st["last_error"] = detail
            st["last_checked"] = now
            if will_alert:
                next_attempt = float(st.get("next_attempt") or 0)
                if now >= next_attempt and (
                    not last_sent or now - last_sent >= self.cooldown
                ):
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
                    result = await self._send(text)
                    delivered = self._delivered(result)
                    st["alerting"] = True
                    st["since"] = now
                    st["last_attempt"] = now
                    st["fails"] = 0
                    st["pending"] = not delivered
                    if delivered:
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        retry_count = int(st.get("retry_count") or 0) + 1
                        st["retry_count"] = retry_count
                        st["next_attempt"] = now + retry_delay(retry_count)
                        self._record_event(name, target_text, detail, "failed")
            elif alerting and pending:
                next_attempt = float(st.get("next_attempt") or 0)
                if now >= next_attempt:
                    text = (
                        f"🔴 服务异常 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"原因: {detail}\n"
                        f"时间: {_now_text(self.config)}"
                    )
                    result = await self._send(text)
                    st["last_attempt"] = now
                    if self._delivered(result):
                        st["pending"] = False
                        st["last_sent"] = now
                        st["retry_count"] = 0
                        st["next_attempt"] = 0
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        retry_count = int(st.get("retry_count") or 0) + 1
                        st["retry_count"] = retry_count
                        st["next_attempt"] = now + retry_delay(retry_count)
        else:
            st["fails"] = 0
            st["last_checked"] = now
            st["last_ok"] = now
            st["last_error"] = ""
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
                stale = bool(st.pop("stale", False))
                duration = now - float(st.get("since") or now)
                st["alerting"] = False
                st["since"] = 0
                st["retry_count"] = 0
                st["next_attempt"] = 0
                # The event log does not depend on the notification pref: the
                # daily report's interruption durations and unresolved list
                # rely on resolved_ts being written on every recovery.
                self._record_event(name, target_text, detail, "recovery")
                if self.recover_notify and not stale:
                    extra = "\n（异常告警此前未能送达）" if pending else ""
                    text = (
                        f"✅ 服务恢复 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"中断时长: {format_duration(duration)}\n"
                        f"时间: {_now_text(self.config)}{extra}"
                    )
                    result = await self._send(text)
                    if not self._delivered(result):
                        st["pending_recovery"] = text
                        st["recovery_retry_count"] = 1
                        st["recovery_next_attempt"] = now + retry_delay(1)
                st["pending"] = False
        states[key] = st

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
                    async with (
                        httpx.AsyncClient(
                            follow_redirects=True, timeout=timeout
                        ) as cli,
                        cli.stream("GET", url) as resp,
                    ):
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
                    return False, detail[:200], container
                running = proc.stdout.strip().lower() == "true"
                return running, f"容器 running={running}", container
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", container

        if kind == "process":
            pattern = str(item.get("pattern") or "").strip().lower()
            if not pattern:
                return False, "进程匹配串未配置", "-"
            try:
                for p in psutil.process_iter(["name", "cmdline"]):
                    try:
                        name = str(p.info.get("name") or "")
                        cmdline = " ".join(p.info.get("cmdline") or [])
                        if pattern in f"{name} {cmdline}".lower():
                            return True, f"匹配到进程 {name or p.pid}", pattern
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                return False, f"未找到匹配进程: {pattern}", pattern
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
                return state_text == "active", f"{unit}: {state_text}", unit
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", unit

        return False, f"未知监测类型: {kind}", "-"

    @staticmethod
    def _item_key(item: dict[str, Any]) -> str:
        kind = str(item.get("__template_key") or "")
        name = str(item.get("name") or "")
        if kind == "http":
            target = str(item.get("url") or "")
        elif kind == "tcp":
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

    async def _send(self, text: str) -> DeliveryResult:
        if not self.targets:
            logger.warning("服务监测推送目标为空")
            return DeliveryResult(ok=False)
        try:
            value = await self.plugin.send_text(self.targets, text, source="monitor")
            return (
                value
                if isinstance(value, DeliveryResult)
                else DeliveryResult(ok=bool(value))
            )
        except Exception:
            logger.exception("服务监测推送失败")
            return DeliveryResult(ok=False)

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

        return list(await asyncio.gather(*(one(item) for item in self.items)))

    def status(self) -> dict[str, Any]:
        monitors = self.state.get("monitors", {}) or {}
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
                    "state": monitors.get("items", {}).get(self._item_key(item), {}),
                    "config_error": item.get("config_error"),
                }
                for item in self.items
            ],
            "last_run": monitors.get("last_run"),
        }
