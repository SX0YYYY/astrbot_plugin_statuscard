from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime
from typing import Any

import httpx
import psutil

from .collectors import cfg_bool, cfg_get, cfg_int, format_duration, now_in_timezone
from .state import StateStore

logger = logging.getLogger("astrbot_plugin_statuscard")


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


def _normalize_targets(raw: Any, platform_id: str) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value:
            continue
        target = value if ":" in value else f"{platform_id}:GroupMessage:{value}"
        if target not in out:
            out.append(target)
    return out


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
        self.interval = max(
            15, min(3600, cfg_int(config, "monitors.interval_seconds", 60))
        )
        self.fail_threshold = max(
            1, min(100, cfg_int(config, "monitors.fail_threshold", 2))
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
        self.targets = _normalize_targets(targets, self.platform_id)
        self.items = self._normalize_items(cfg_get(config, "monitors.items", []))
        self._task: asyncio.Task | None = None
        self._running = False
        self._silence_warned = False
        self._silence_active = False
        self._suppress_warned: set[str] = set()

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
            if not item.get(required):
                logger.warning(
                    "监测项 %r（%s）缺少 %s 配置，检查时会始终判定为异常",
                    str(item.get("name") or kind),
                    kind,
                    required,
                )
            normalized = dict(item)
            normalized["__template_key"] = kind
            normalized["name"] = str(item.get("name") or kind)
            out.append(normalized)
        return out

    def start(self) -> None:
        if not self.enabled:
            logger.info("服务监测未启用（monitors.enabled=false）")
            return
        if not self.items:
            logger.warning("服务监测已启用，但没有配置任何监测目标")
        self._prune_state()
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
                int(now), day, "service", name, detail, status, target
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
            logger.warning("服务监测目标为空，跳过本轮检查")
            return
        results = await asyncio.gather(
            *(self._check_item(item) for item in self.items),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("服务监测检查异常: %s", result)
        self.state.data.setdefault("monitors", {})["last_run"] = time.time()
        self.state.save()

    def _silence_until(self) -> datetime | None:
        """Parse ``monitors.silence_until`` as a wall-clock time in display.timezone."""

        raw = str(
            cfg_get(self.config, "monitors.silence_until", "") or ""
        ).strip()
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
        until = self._silence_until()
        silenced = bool(
            until is not None and now_in_timezone(self.config) < until
        )
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
        now = time.time()
        monitors = self.state.data.setdefault("monitors", {})
        states = monitors.setdefault("items", {})
        st = states.setdefault(key, {})
        fails = int(st.get("fails") or 0)
        alerting = bool(st.get("alerting"))
        pending = bool(st.get("pending"))
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
            st.pop("pending_recovery", None)
            st.pop("recovery_attempt", None)
            fails += 1
            st["fails"] = fails
            st["last_error"] = detail
            st["last_checked"] = now
            if not alerting and fails >= self.fail_threshold:
                last_attempt = float(st.get("last_attempt") or 0)
                if now - last_attempt >= self.cooldown:
                    text = (
                        f"🔴 服务异常 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"原因: {detail}\n"
                        f"时间: {_now_text(self.config)}\n"
                        f"已连续 {self.fail_threshold} 次检测失败。"
                    )
                    sent = await self._send(text)
                    st["alerting"] = True
                    st["since"] = now
                    st["last_attempt"] = now
                    st["fails"] = 0
                    st["pending"] = not sent
                    if sent:
                        st["last_sent"] = now
                        self._record_event(name, target_text, detail, "alert")
                    else:
                        self._record_event(name, target_text, detail, "failed")
            elif alerting and pending:
                last_attempt = float(st.get("last_attempt") or 0)
                if now - last_attempt >= self.cooldown:
                    text = (
                        f"🔴 服务异常 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"原因: {detail}\n"
                        f"时间: {_now_text(self.config)}"
                    )
                    sent = await self._send(text)
                    st["last_attempt"] = now
                    if sent:
                        st["pending"] = False
                        st["last_sent"] = now
                        self._record_event(
                            name, target_text, detail, "alert"
                        )
        else:
            st["fails"] = 0
            st["last_checked"] = now
            st["last_ok"] = now
            st["last_error"] = ""
            pending_text = st.get("pending_recovery")
            if pending_text:
                last_attempt = float(st.get("recovery_attempt") or 0)
                if now - last_attempt >= self.cooldown:
                    sent = await self._send(str(pending_text))
                    st["recovery_attempt"] = now
                    if sent:
                        st.pop("pending_recovery", None)
                        st.pop("recovery_attempt", None)
            if alerting:
                duration = now - float(st.get("since") or now)
                st["alerting"] = False
                st["since"] = 0
                if self.recover_notify:
                    extra = "\n（异常告警此前未能送达）" if pending else ""
                    text = (
                        f"✅ 服务恢复 · {name}\n"
                        "──────────────\n"
                        f"类型: {kind}\n"
                        f"目标: {target_text}\n"
                        f"中断时长: {format_duration(duration)}\n"
                        f"时间: {_now_text(self.config)}{extra}"
                    )
                    sent = await self._send(text)
                    if not sent:
                        st["pending_recovery"] = text
                        st["recovery_attempt"] = now
                    else:
                        self._record_event(
                            name, target_text, detail, "recovery"
                        )
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
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=timeout
                ) as cli:
                    async with cli.stream("GET", url) as resp:
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
            except Exception as exc:
                return False, f"{exc.__class__.__name__}: {exc}", url

        if kind == "tcp":
            host = str(item.get("host") or "127.0.0.1").strip()
            port = cfg_int(item, "port", 0)
            target = f"{host}:{port}"
            if not port:
                return False, "端口未配置", target
            try:
                reader, writer = await asyncio.wait_for(
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
                    detail = (proc.stderr or proc.stdout or "docker inspect 失败").strip()
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
        return f"{kind}:{name}:{target}"

    async def _send(self, text: str) -> bool:
        if not self.targets:
            logger.warning("服务监测推送目标为空")
            return False
        try:
            return await self.plugin.send_text(self.targets, text)
        except Exception:
            logger.exception("服务监测推送失败")
            return False

    async def test_all(self) -> list[dict[str, Any]]:
        async def one(item: dict[str, Any]) -> dict[str, Any]:
            ok, detail, target = await self._probe(item)
            return {
                "name": str(item.get("name") or ""),
                "kind": KIND_LABELS.get(str(item.get("__template_key")), "?"),
                "target": target,
                "ok": ok,
                "detail": detail,
            }

        return list(
            await asyncio.gather(*(one(item) for item in self.items))
        )

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
                }
                for item in self.items
            ],
            "last_run": monitors.get("last_run"),
        }
