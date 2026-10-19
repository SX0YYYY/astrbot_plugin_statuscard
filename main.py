from __future__ import annotations

import asyncio
import time
from typing import Final

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .alerts import AlertEngine
from .bg_provider import MAX_IMAGE_BYTES, resolve_background
from .collectors import (
    collect_all,
    cfg_bool,
    cfg_get,
    cfg_int,
    effective_media_source,
    fetch_avatar_bytes,
    uploaded_file_path,
)
from .monitor import ServiceMonitor
from .sampler import Sampler
from .state import StateStore
from .t2i_renderer import build_default_html


PLUGIN_NAME: Final[str] = "astrbot_plugin_statuscard"


@register(
    PLUGIN_NAME,
    "烬辞",
    "以图片卡片显示系统运行状态，并支持主动告警推送",
    "2.1.2",
)
class StatusCardPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(self.data_dir / "state.json")
        self._migrate_media_sources()
        interval = cfg_int(self.config, "alerts.interval_seconds", 60)
        self.sampler = Sampler(
            self.state, interval_seconds=interval, config=self.config
        )
        self.alerts = AlertEngine(self, self.state, self.config)
        self.monitor = ServiceMonitor(self, self.state, self.config)
        self._status_cooldown: dict[str, float] = {}
        self._status_inflight: set[str] = set()
        self._privacy_notified: set[str] = set()

    def _migrate_media_sources(self) -> None:
        """Keep images uploaded before the source switch from being ignored.

        AstrBot fills new schema defaults into existing config files before the
        plugin sees them, so a legacy config would otherwise be treated as
        "default" even when it still has uploaded files. Run this once and
        persist the inferred source values.
        """

        migration = self.state.setdefault("migration", {})
        if not isinstance(migration, dict):
            migration = {}
            self.state.data["migration"] = migration
        if migration.get("media_source_v2"):
            return

        changed = False
        for section_name, source_name, upload_key in (
            ("avatar", "avatar_source", "avatar.avatar_upload_files"),
            ("background", "background_source", "background.bg_upload_files"),
        ):
            section = self.config.get(section_name) if isinstance(self.config, dict) else None
            if not isinstance(section, dict):
                continue
            desired = (
                "upload"
                if uploaded_file_path(self.config, upload_key, self.data_dir)
                is not None
                else "default"
            )
            if section.get(source_name) != desired:
                section[source_name] = desired
                changed = True

        migration["media_source_v2"] = True
        self.state.save()
        if changed and hasattr(self.config, "save_config"):
            try:
                self.config.save_config()
                logger.info("已将旧版上传头像/背景迁移为“自定义上传”来源")
            except Exception:
                logger.exception("写入媒体来源迁移配置失败")

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

    def _status_access_allowed(self, event: AstrMessageEvent) -> bool:
        mode = str(
            cfg_get(self.config, "display.access", "all") or "all"
        ).strip().lower()
        if mode == "all":
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        if mode == "admin":
            return False
        if mode != "allowlist":
            return True

        raw = cfg_get(self.config, "display.allowlist", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return False
        allowed = {
            str(item).strip()
            for item in raw
            if str(item).strip()
        }
        values: set[str] = set()
        try:
            if event.unified_msg_origin:
                values.add(str(event.unified_msg_origin))
        except Exception:
            pass
        for getter in ("get_sender_id", "get_group_id"):
            try:
                value = getattr(event, getter)()
                if value:
                    values.add(str(value))
            except Exception:
                pass
        return bool(allowed & values)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """生成并发送当前设备的系统状态图片。"""
        event.should_call_llm(False)
        if not self._status_access_allowed(event):
            yield event.plain_result(
                "查看状态仅管理员可用。"
            )
            return
        cooldown = max(
            0, min(3600, cfg_int(self.config, "display.cooldown_seconds", 10))
        )
        chat_key = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.monotonic()
        if chat_key:
            if chat_key in self._status_inflight:
                yield event.plain_result("状态卡正在生成，请稍候。")
                return
            if cooldown:
                last = self._status_cooldown.get(chat_key, 0.0)
                remaining = cooldown - (now - last)
                if remaining > 0:
                    yield event.plain_result(
                        f"状态卡冷却中，请 {max(1, int(remaining + 0.999))} 秒后再试。"
                    )
                    return
                self._status_cooldown[chat_key] = now
                if len(self._status_cooldown) > 200:
                    cutoff = now - cooldown * 2
                    self._status_cooldown = {
                        key: ts
                        for key, ts in self._status_cooldown.items()
                        if ts >= cutoff
                    }
            self._status_inflight.add(chat_key)
        notices: list[str] = []
        if cfg_bool(self.config, "display.privacy_notice", True):
            if chat_key and chat_key not in self._privacy_notified:
                self._privacy_notified.add(chat_key)
                if len(self._privacy_notified) > 500:
                    self._privacy_notified.clear()
                    self._privacy_notified.add(chat_key)
                notices.append(
                    "提示：状态图包含主机名/进程等信息，并会发送到 T2I 服务渲染。"
                )
        if cfg_bool(self.config, "display.progress_notice", True):
            notices.append("状态卡生成中，通常需要 1–3 分钟，请稍候…")
        if notices:
            yield event.plain_result("\n".join(notices))
        try:
            image_url = await self._render_status_image(event)
            if chat_key:
                self._status_inflight.discard(chat_key)
            yield event.image_result(image_url)
        except Exception:
            if chat_key:
                self._status_cooldown.pop(chat_key, None)
                self._status_inflight.discard(chat_key)
            logger.exception("生成系统状态图片失败")
            try:
                summary = await self.build_summary_text(event=event)
            except Exception:
                summary = "获取系统状态失败，请检查 AstrBot 后台日志。"
            yield event.plain_result("生成状态图失败，以下为文本摘要：\n" + summary)
        finally:
            if chat_key:
                self._status_inflight.discard(chat_key)

    async def _render_status_image(self, event: AstrMessageEvent | None = None) -> str:
        collected = await collect_all(
            context=self.context,
            event=event,
            state=self.state,
            config=self.config,
        )
        background = await self._resolve_background(event)
        avatar = await fetch_avatar_bytes(
            collected["view"]["bot"], self.config, data_dir=self.data_dir
        )
        render_width = max(
            1170, min(2880, cfg_int(self.config, "display.render_width", 1170))
        )
        # The T2I service supports three fixed scale levels. "ultra" is 1.8x,
        # so a logical width of 650 CSS px produces a 1170 px wide image.
        logical_width = max(650, min(1600, round(render_width / 1.8)))
        html = build_default_html(
            collected,
            background.data,
            background.mime,
            avatar_bytes=avatar,
            render_width=logical_width,
        )
        timeout = max(
            30,
            min(600, cfg_int(self.config, "display.render_timeout", 180)),
        )
        options = {
            "type": "jpeg",
            "quality": 90,
            "full_page": True,
            "device_scale_factor_level": "ultra",
        }
        return await asyncio.wait_for(
            self.html_render(html, {}, return_url=True, options=options),
            timeout=timeout,
        )

    async def build_status_image(self) -> str | None:
        """Render a status image without an incoming event (scheduled daily status image)."""
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

        if bg_bytes is None:
            source = effective_media_source(
                self.config,
                "background.background_source",
                "background.bg_upload_files",
                self.data_dir,
            )
            if source == "upload":
                uploaded = uploaded_file_path(
                    self.config, "background.bg_upload_files", self.data_dir
                )
                if uploaded is not None:
                    try:
                        if uploaded.stat().st_size > MAX_IMAGE_BYTES:
                            logger.warning(
                                "上传背景超过 10 MB，已跳过: %s", uploaded
                            )
                        else:
                            bg_bytes = uploaded.read_bytes() or None
                    except Exception as exc:
                        logger.warning(
                            "读取上传背景失败 %s: %s", uploaded, exc
                        )
                if bg_bytes is None:
                    logger.warning(
                        "背景来源为自定义上传，但没有可用的上传文件，回退内置背景"
                    )

        return await resolve_background(prefer_bytes=bg_bytes)

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
                logger.warning(
                    "主动发送到 %s 失败: %s",
                    target,
                    str(exc) or exc.__class__.__name__,
                )
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
                logger.warning(
                    "主动发送图片到 %s 失败: %s",
                    target,
                    str(exc) or exc.__class__.__name__,
                )
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
    @filter.command("测试通知")
    async def cmd_alert_test(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not cfg_bool(self.config, "alerts.enabled", False):
            yield event.plain_result(
                "告警推送未启用。请先在配置页打开“启用资源与掉线告警”。"
            )
            return
        ok = await self.alerts.send_test(event.unified_msg_origin)
        yield event.plain_result(
            "测试通知已发送到当前会话。" if ok else "测试通知发送失败，请查看后台日志。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("立即检测")
    async def cmd_monitor_test(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not self.monitor.items:
            yield event.plain_result(
                "没有配置监测目标。请在配置页的“服务可用性监测”中添加。"
            )
            return
        results = await self.monitor.test_all()
        failed = [item for item in results if not item["ok"]]
        lines = [
            "🩺 服务检测结果",
            f"✅ 正常 {len(results) - len(failed)} 项 · ❌ 异常 {len(failed)} 项",
        ]
        if failed:
            for item in failed:
                lines.append(
                    f"❌ {item['name']}（{item['kind']}）\n   {item['detail']}"
                )
        else:
            lines.append("✅ 全部正常。")
        yield event.plain_result("\n".join(lines))
