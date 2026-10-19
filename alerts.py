from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psutil

from .collectors import (
    _call_action,
    _get_aiocqhttp_client,
    _normalize_object,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_get,
    format_bytes,
    format_duration,
    memory_stat,
    swap_stat,
)
from .state import StateStore

logger = logging.getLogger("astrbot_plugin_statuscard")


def _now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


class AlertEngine:
    """Threshold, offline, daily-summary and Komari webhook alerts.

    All alerts are opt-in. The plugin ships with ``alerts.enabled = false``.
    """

    def __init__(self, plugin: Any, state: StateStore, config: Any):
        self.plugin = plugin
        self.state = state
        self.config = config
        self.interval = max(15, cfg_int(config, "alerts.interval_seconds", 60))
        self.sustain = max(1, cfg_int(config, "alerts.sustain_samples", 3))
        self.cooldown = max(0, cfg_int(config, "alerts.cooldown_seconds", 1800))
        self.recover_notify = cfg_bool(config, "alerts.recover_notify", True)
        self.offline_notify = cfg_bool(config, "alerts.offline_notify", True)
        self.cpu_threshold = cfg_float(config, "alerts.cpu_threshold", 85.0)
        self.mem_threshold = cfg_float(config, "alerts.mem_threshold", 90.0)
        self.swap_threshold = cfg_float(config, "alerts.swap_threshold", 90.0)
        self.disk_threshold = cfg_float(config, "alerts.disk_threshold", 85.0)
        self.load_threshold = cfg_float(
            config, "alerts.load_per_core_threshold", 2.0
        )
        self.platform_id = str(
            cfg_get(config, "alerts.platform_id", "default") or "default"
        )
        self.targets = self._normalize_targets(
            cfg_get(config, "alerts.targets", []), self.platform_id
        )
        self._monitor_task: asyncio.Task | None = None
        self._daily_task: asyncio.Task | None = None
        self._running = False
        self._webhook_runner: Any = None
        self._webhook_site: Any = None
        self._primed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if not cfg_bool(self.config, "alerts.enabled", False):
            logger.info("状态卡告警未启用（alerts.enabled=false）")
            self._start_webhook_task()
            return
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self._daily_task = asyncio.create_task(self._daily_loop())
        self._start_webhook_task()
        logger.info(
            "状态卡告警已启动：目标=%s，间隔=%ss，阈值 CPU %.0f%% / RAM %.0f%% / SWAP %.0f%% / DISK %.0f%%",
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
        asyncio.create_task(self.start_webhook())

    async def stop(self) -> None:
        self._running = False
        for task in (self._monitor_task, self._daily_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._monitor_task = None
        self._daily_task = None
        await self.stop_webhook()

    # ------------------------------------------------------------------
    # targets / sending
    # ------------------------------------------------------------------

    @staticmethod
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
            if ":" in value:
                target = value
            else:
                target = f"{platform_id}:GroupMessage:{value}"
            if target not in out:
                out.append(target)
        return out

    async def _send(self, text: str) -> bool:
        if not self.targets:
            logger.warning("告警目标为空，跳过发送")
            return False
        if not await self.plugin.send_text(self.targets, text):
            logger.warning("告警发送失败：%s", text.splitlines()[0] if text else "")
            return False
        return True

    async def send_test(self, target_umo: str | None = None) -> bool:
        targets = [target_umo] if target_umo else self.targets
        if not targets:
            return False
        text = (
            "🔔 状态卡告警测试\n"
            "──────────────\n"
            f"主机: {os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', '--')}\n"
            f"时间: {_now_text()}\n"
            "如果你收到这条消息，说明主动推送链路正常。"
        )
        return await self.plugin.send_text(targets, text)

    # ------------------------------------------------------------------
    # monitor loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        # Prime psutil's non-blocking CPU counter.
        psutil.cpu_percent(interval=None)
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
        cpu = psutil.cpu_percent(interval=None)
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
        self.state.data["alerts_last_sample"] = now
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

        if value >= threshold:
            count += 1
            st["count"] = count
            if not alerting and count >= self.sustain:
                last_attempt = float(st.get("last_attempt") or 0)
                if now - max(last_attempt, last_sent) >= self.cooldown:
                    text = (
                        f"⚠️ 系统告警 · {name}\n"
                        "──────────────\n"
                        f"当前: {value_text}（阈值 {threshold:.1f}{unit}）\n"
                        f"主机: {self._hostname()}\n"
                        f"时间: {_now_text()}\n"
                        f"已连续 {self.sustain} 个采样周期超过阈值。"
                    )
                    ok = await self._send(text)
                    st["last_attempt"] = now
                    st["last_error"] = "" if ok else "send_failed"
                    if ok:
                        st["alerting"] = True
                        st["alert_since"] = now
                        st["count"] = 0
                        st["last_sent"] = now
                    else:
                        # Retry after the cooldown instead of on every sample.
                        st["count"] = self.sustain
        else:
            st["count"] = 0
            if alerting:
                st["alerting"] = False
                duration = now - float(st.get("alert_since") or now)
                st["alert_since"] = 0
                if self.recover_notify:
                    text = (
                        f"✅ 已恢复 · {name}\n"
                        "──────────────\n"
                        f"当前: {value_text}\n"
                        f"告警持续: {format_duration(duration)}\n"
                        f"时间: {_now_text()}"
                    )
                    await self._send(text)

    async def _check_offline(self) -> None:
        client = _get_aiocqhttp_client(self.plugin.context)
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
        except Exception:
            online = False

        alerts = self.state.setdefault("alerts", {})
        st = alerts.setdefault("offline", {})
        now = time.time()
        count = int(st.get("count") or 0)

        if online:
            st["count"] = 0
            if st.get("alerting"):
                duration = now - float(st.get("since") or now)
                st["alerting"] = False
                st["since"] = 0
                text = (
                    "✅ Bot 连接已恢复\n"
                    "──────────────\n"
                    f"离线时长: {format_duration(duration)}\n"
                    f"时间: {_now_text()}"
                )
                if st.get("pending_alert"):
                    text += "\n（离线期间有一条告警未能送达，现已恢复）"
                    st["pending_alert"] = False
                await self._send(text)
            return

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
                f"时间: {_now_text()}\n"
                "离线告警会先排队，连接恢复后补发。"
            )
            ok = await self._send(text)
            st["pending_alert"] = not ok

    @staticmethod
    def _hostname() -> str:
        try:
            return os.uname().nodename
        except Exception:
            return os.environ.get("COMPUTERNAME", "unknown")

    # ------------------------------------------------------------------
    # daily summary
    # ------------------------------------------------------------------

    async def _daily_loop(self) -> None:
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
                except Exception:
                    tz = datetime.now().astimezone().tzinfo
                time_text = str(
                    cfg_get(self.config, "alerts.daily_summary.time", "09:00")
                    or "09:00"
                )
                try:
                    hour, minute = [int(x) for x in time_text.split(":", 1)]
                except Exception:
                    hour, minute = 9, 0
                now = datetime.now(tz)
                target = now.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                )
                if target <= now:
                    target += timedelta(days=1)
                wait = max(1.0, (target - now).total_seconds())
                logger.info(
                    "状态卡每日摘要将在 %s 发送（%.0f 秒后）",
                    target.strftime("%Y-%m-%d %H:%M"),
                    wait,
                )
                await asyncio.sleep(wait)
                if not cfg_bool(
                    self.config, "alerts.daily_summary.enabled", False
                ):
                    continue
                await self._send_daily_summary()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("状态卡每日摘要任务失败")
                await asyncio.sleep(60)

    async def _send_daily_summary(self) -> None:
        if not self.targets:
            return
        with_image = cfg_bool(
            self.config, "alerts.daily_summary.with_image", True
        )
        if with_image:
            try:
                image_url = await self.plugin.build_status_image()
            except Exception:
                logger.exception("每日摘要渲染状态图失败")
                image_url = None
            if image_url:
                ok = await self.plugin.send_image(self.targets, image_url)
                if ok:
                    return
        summary = await self.plugin.build_summary_text()
        await self._send("📅 每日状态摘要\n──────────────\n" + summary)

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
        port = cfg_int(self.config, "alerts.komari_webhook.port", 9968)
        path = str(
            cfg_get(self.config, "alerts.komari_webhook.path", "/webhook")
            or "/webhook"
        )
        if not path.startswith("/"):
            path = "/" + path
        token = str(cfg_get(self.config, "alerts.komari_webhook.token", "") or "")

        if host == "0.0.0.0" and not token:
            logger.warning(
                "Komari Webhook 绑定 0.0.0.0 且未设置 token，存在安全风险；建议改为 127.0.0.1 或设置 token"
            )

        app = web.Application()

        async def handle(request: "web.Request") -> "web.Response":
            if token:
                auth = request.headers.get("Authorization", "")
                if auth != f"Bearer {token}":
                    return web.json_response(
                        {"status": "error", "message": "unauthorized"},
                        status=403,
                    )
            try:
                data = await request.json()
            except Exception:
                text = (await request.text())[:4000]
                data = {"message": text}
            text = self._format_komari(data)
            await self._send(text)
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
            return "🔔 Komari 监控通知\n──────────────\n" + json.dumps(
                data, ensure_ascii=False, indent=2
            )[:4000]

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
            lines.append(
                json.dumps(data, ensure_ascii=False, indent=2)[:4000]
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        alerts = self.state.get("alerts", {}) or {}
        return {
            "enabled": cfg_bool(self.config, "alerts.enabled", False),
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
            "komari_webhook": bool(
                cfg_bool(self.config, "alerts.komari_webhook.enabled", False)
            ),
        }
