from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .alerts import AlertEngine
from .bg_provider import BackgroundRequest, resolve_background
from .collectors import (
    collect_all,
    cfg_bool,
    cfg_get,
    cfg_int,
    fetch_avatar_bytes,
)
from .monitor import ServiceMonitor
from .sampler import Sampler
from .state import StateStore
from .t2i_renderer import build_default_html


PLUGIN_NAME: Final[str] = "astrbot_plugin_statuscard"
ALIASES: Final[set[str]] = {
    "/状态卡",
    "状态卡片",
    "sysstatus",
    "系统状态",
}


@register(
    PLUGIN_NAME,
    "烬辞",
    "以华丽的图片卡片显示系统运行状态，并支持主动告警推送",
    "1.0.0",
)
class StatusCardPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(self.data_dir / "state.json")
        interval = cfg_int(self.config, "alerts.interval_seconds", 60)
        self.sampler = Sampler(self.state, interval_seconds=interval)
        self.alerts = AlertEngine(self, self.state, self.config)
        self.monitor = ServiceMonitor(self, self.state, self.config)

    async def initialize(self):
        self.sampler.start()
        self.alerts.start()
        self.monitor.start()
        logger.info(
            "StatusCard plugin initialized (data_dir=%s, alerts_enabled=%s, monitors_enabled=%s)",
            self.data_dir,
            cfg_bool(self.config, "alerts.enabled", False),
            cfg_bool(self.config, "monitors.enabled", False),
        )

    async def terminate(self):
        await self.monitor.stop()
        await self.alerts.stop()
        await self.sampler.stop()
        logger.info("StatusCard plugin terminated")

    # ------------------------------------------------------------------
    # status card
    # ------------------------------------------------------------------

    @filter.command("状态卡", alias=ALIASES)
    async def cmd_status(self, event: AstrMessageEvent):
        """生成并发送当前设备的系统状态图片。"""
        event.should_call_llm(False)
        try:
            image_url = await self._render_status_image(event)
            yield event.image_result(image_url)
        except Exception:
            logger.exception("生成系统状态图片失败")
            try:
                summary = await self.build_summary_text(event=event)
            except Exception:
                summary = "获取系统状态失败，请检查 AstrBot 后台日志。"
            yield event.plain_result("生成状态图失败，以下为文本摘要：\n" + summary)

    async def _render_status_image(self, event: AstrMessageEvent | None = None) -> str:
        collected = await collect_all(
            context=self.context,
            event=event,
            state=self.state,
            config=self.config,
        )
        background = await self._resolve_background(event)
        avatar = await fetch_avatar_bytes(collected["view"]["bot"], self.config)
        html = build_default_html(
            collected,
            background.data,
            background.mime,
            avatar_bytes=avatar,
        )
        options = {
            "type": "jpeg",
            "quality": 90,
            "full_page": True,
            "device_scale_factor_level": "ultra",
        }
        return await self.html_render(html, {}, return_url=True, options=options)

    async def build_status_image(self) -> str | None:
        """Render a status image without an incoming event (daily summary)."""
        return await self._render_status_image(event=None)

    async def _resolve_background(self, event: AstrMessageEvent | None):
        bg_bytes = None
        if event is not None:
            try:
                for seg in event.get_messages():
                    if isinstance(seg, Comp.Image):
                        file = getattr(seg, "file", None) or ""
                        if isinstance(file, str) and file.startswith(
                            ("http://", "https://")
                        ):
                            async with httpx.AsyncClient(
                                follow_redirects=True, timeout=5
                            ) as cli:
                                resp = await cli.get(file)
                                resp.raise_for_status()
                                bg_bytes = resp.content
                                break
            except Exception:
                bg_bytes = None

        provider = str(
            cfg_get(self.config, "background.bg_provider", "none") or "none"
        )
        fallback = str(
            cfg_get(self.config, "background.bg_fallback_chain", "none") or "none"
        )
        chain: list[str] = []
        for item in [provider, *fallback.split(",")]:
            name = item.strip().lower()
            if name and name not in chain:
                chain.append(name)
        if not chain:
            chain = ["none"]

        local_path = cfg_get(self.config, "background.bg_local_path", "")
        local_path = local_path.strip() if isinstance(local_path, str) else ""
        proxy = cfg_get(self.config, "background.bg_proxy", "")
        proxy = proxy.strip() if isinstance(proxy, str) else ""

        request = BackgroundRequest(
            provider_chain=tuple(chain),
            local_path=Path(local_path) if local_path else None,
            timeout=cfg_int(self.config, "background.bg_req_timeout", 10),
            proxy=proxy or None,
            preload_count=cfg_int(self.config, "background.bg_preload_count", 1),
            lolicon_r18_type=cfg_int(
                self.config, "background.bg_lolicon_r18_type", 0
            ),
        )
        return await resolve_background(prefer_bytes=bg_bytes, request=request)

    # ------------------------------------------------------------------
    # text / image helpers used by the alert engine
    # ------------------------------------------------------------------

    async def send_text(self, targets: list[str], text: str) -> bool:
        chain = MessageChain([Comp.Plain(text)])
        any_ok = False
        for target in targets:
            try:
                sent = await self.context.send_message(target, chain)
                if sent:
                    any_ok = True
                else:
                    logger.warning("主动消息未找到匹配平台: %s", target)
            except Exception as exc:
                logger.warning("主动发送到 %s 失败: %s", target, exc)
        return any_ok

    async def send_image(self, targets: list[str], image_url: str) -> bool:
        chain = MessageChain([Comp.Image.fromURL(image_url)])
        any_ok = False
        for target in targets:
            try:
                sent = await self.context.send_message(target, chain)
                if sent:
                    any_ok = True
                else:
                    logger.warning("主动图片未找到匹配平台: %s", target)
            except Exception as exc:
                logger.warning("主动发送图片到 %s 失败: %s", target, exc)
        return any_ok

    async def build_summary_text(
        self, event: AstrMessageEvent | None = None
    ) -> str:
        collected = await collect_all(
            context=self.context,
            event=event,
            state=self.state,
            config=self.config,
        )
        view = collected["view"]
        bot = view["bot"]
        resources = view["resources"]
        cpu = resources[0]
        ram = resources[1]
        swap = resources[2]
        disk = view["disks"][0] if view["disks"] else None
        network = view["network"]
        process = view["process"]["counts"]
        lines = [
            f"主机: {view['overview'][1]['value']}",
            f"{cpu['title']}: {cpu['percent']:.1f}%",
            f"RAM: {ram['percent']:.1f}% ({ram['desc'].splitlines()[0]})",
            f"SWAP: {swap['percent']:.1f}%",
        ]
        if disk:
            lines.append(
                f"磁盘 {disk['path']}: {disk['percent_text']} ({disk['used']} / {disk['total']})"
            )
        lines.extend(
            [
                f"网络: ↑ {network['up']} ↓ {network['down']}",
                f"本月流量: ↑ {network['month_up']} ↓ {network['month_down']}",
                f"进程: 全部 {process['all']} / 运行 {process['running']} / 休眠 {process['sleeping']}",
                f"好友: {bot.get('friends') if bot.get('friends') is not None else '--'}"
                f" / 群聊: {bot.get('groups') if bot.get('groups') is not None else '--'}"
                f" / 群员: {bot.get('members') if bot.get('members') is not None else '--'}",
                f"收发: {bot.get('recv') if bot.get('recv') is not None else '--'}"
                f" ({bot.get('recv_scope')}) / {bot.get('sent') if bot.get('sent') is not None else '--'}"
                f" ({bot.get('sent_scope')})",
            ]
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # admin diagnostics
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态卡告警测试", alias={"/状态卡告警测试"})
    async def cmd_alert_test(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not cfg_bool(self.config, "alerts.enabled", False):
            yield event.plain_result(
                "告警当前未启用（alerts.enabled=false）。启用后再试，或先在 WebUI 中打开。"
            )
            return
        ok = await self.alerts.send_test(event.unified_msg_origin)
        yield event.plain_result(
            "测试告警已发送到当前会话。" if ok else "测试告警发送失败，请查看后台日志。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态卡告警状态", alias={"/状态卡告警状态"})
    async def cmd_alert_status(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        status = self.alerts.status()
        yield event.plain_result(
            "状态卡告警配置：\n"
            + json.dumps(status, ensure_ascii=False, indent=2)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态卡监测状态", alias={"/状态卡监测状态"})
    async def cmd_monitor_status(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        status = self.monitor.status()
        yield event.plain_result(
            "服务监测状态：\n"
            + json.dumps(status, ensure_ascii=False, indent=2)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态卡监测测试", alias={"/状态卡监测测试"})
    async def cmd_monitor_test(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not self.monitor.items:
            yield event.plain_result(
                "没有配置任何监测目标。请在 WebUI 的 monitors.items 中添加。"
            )
            return
        results = await self.monitor.test_all()
        lines = ["服务监测测试结果："]
        for result in results:
            icon = "✅" if result["ok"] else "❌"
            lines.append(
                f"{icon} {result['name']} [{result['kind']}] {result['target']}\n"
                f"   {result['detail']}"
            )
        yield event.plain_result("\n".join(lines))
