from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Final

import astrbot.api.message_components as Comp
import psutil
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain

from .aggregator import AlertAggregator
from .alerts import AlertEngine
from .audit import AuditIssue, run_audit
from .bg_provider import MAX_IMAGE_BYTES, resolve_background
from .collectors import (
    _call_action,
    _get_aiocqhttp_client,
    _normalize_object,
    cfg_bool,
    cfg_get,
    cfg_int,
    cgroup_memory_limit,
    collect_all,
    effective_media_source,
    fetch_avatar_bytes,
    notify_rules_active,
    uploaded_file_path,
)
from .delivery import DeliveryResult, DeliveryService
from .i18n import tr
from .local_card import render_fallback_card
from .monitor import ServiceMonitor
from .page_api import PageApi
from .report import DailyReporter, ReportCollector
from .report_store import ReportStore
from .sampler import Sampler
from .state import StateStore
from .t2i_renderer import build_default_html, build_report_html
from .utils import clamp_config, clean_report_sections, is_supported_platform
from .version import __version__

PLUGIN_NAME: Final[str] = "astrbot_plugin_statuscard"


@register(
    PLUGIN_NAME,
    "烬辞",
    "把服务器状态做成图片卡片发到 QQ，并提供 Web 统计面板、告警与日报",
    __version__,
)
class StatusCardPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = StateStore(self.data_dir / "state.json")
        self._migrate_media_sources()
        self._migrate_report_sections()
        self.report_store = ReportStore(self.data_dir / "report.db")
        interval = clamp_config(
            cfg_int(self.config, "display.sample_seconds", 60),
            15,
            600,
            "display.sample_seconds",
        )
        self.sampler = Sampler(
            self.state, interval_seconds=interval, config=self.config
        )
        self.report = ReportCollector(self, self.state, self.config, self.report_store)
        self.daily_report = DailyReporter(self, self.report_store, self.config)
        self._alert_aggregator = AlertAggregator(
            clamp_config(
                cfg_int(self.config, "alerts.aggregate_seconds", 120),
                0,
                600,
                "alerts.aggregate_seconds",
            ),
            clamp_config(
                cfg_int(self.config, "alerts.aggregate_max_items", 8),
                2,
                20,
                "alerts.aggregate_max_items",
            ),
        )
        self.alerts = AlertEngine(
            self,
            self.state,
            self.config,
            report_store=self.report_store,
            aggregator=self._alert_aggregator,
        )
        self.monitor = ServiceMonitor(
            self,
            self.state,
            self.config,
            report_store=self.report_store,
            aggregator=self._alert_aggregator,
        )
        self._status_cooldown: dict[str, float] = {}
        self._status_inflight: set[str] = set()
        self._privacy_notified: set[str] = set()
        self._delivery: DeliveryService | None = None
        self._diag: dict[str, Any] = self._load_diag()
        self.page_api = PageApi(self)
        self.page_api.register()
        self._audit_issues: list[AuditIssue] = run_audit(
            self.config, monitor=self.monitor
        )
        self._check_config_permissions()

    def audit_issues(self) -> list[AuditIssue]:
        """Configuration findings collected once at plugin start-up."""

        return list(self._audit_issues)

    def _load_diag(self) -> dict[str, Any]:
        """Restore the diagnostics snapshot kept in ``state.json``.

        The diagnostics used to live only in memory, so a plugin reload wiped
        them and ``状态卡诊断`` claimed nothing had been rendered yet even
        though a card had just been sent. Persisting them keeps the command
        useful across restarts.
        """

        raw = self.state.get("diag", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Any] = {}
        for key, value in raw.items():
            name = str(key)
            if isinstance(value, (bool, int, float)):
                out[name] = float(value)
            else:
                out[name] = str(value)
        return out

    def _save_diag(self) -> None:
        try:
            self.state.set("diag", dict(self._diag))
            self.state.save()
        except Exception as exc:
            logger.warning("保存诊断信息失败: %s", exc)

    def _check_config_permissions(self) -> None:
        if os.name != "posix":
            return
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            path = (
                Path(get_astrbot_data_path()) / "config" / f"{PLUGIN_NAME}_config.json"
            )
            if not path.is_file():
                return
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning(
                    "插件配置文件权限过宽：%s (%o)，建议 chmod 600",
                    path,
                    mode,
                )
        except Exception:
            pass

    def _migrate_media_sources(self) -> None:
        """Keep images uploaded before the source switch from being ignored.

        AstrBot fills new schema defaults into existing config files before the
        plugin sees them, so a legacy config would otherwise be treated as
        "default" even when it still has uploaded files. Run this once and
        persist the inferred source values.
        """

        with self.state.section("migration") as migration:
            if migration.get("media_source_v2"):
                return

            changed = False
            for section_name, source_name, upload_key in (
                ("avatar", "avatar_source", "avatar.avatar_upload_files"),
                ("background", "background_source", "background.bg_upload_files"),
            ):
                section = (
                    self.config.get(section_name)
                    if isinstance(self.config, dict)
                    else None
                )
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

    def _migrate_report_sections(self) -> None:
        """Remove the retired ``todo`` module from a legacy report config."""

        with self.state.section("migration") as migration:
            if migration.get("daily_report_sections_v1"):
                return
            section = (
                self.config.get("daily_report")
                if isinstance(self.config, dict)
                else None
            )
            if isinstance(section, dict):
                cleaned = clean_report_sections(section.get("sections"))
                if cleaned is not None:
                    section["sections"] = cleaned
                    if hasattr(self.config, "save_config"):
                        try:
                            self.config.save_config()
                            logger.info("已从日报内容中移除旧待办模块：%s", cleaned)
                        except Exception:
                            logger.exception("保存日报内容迁移失败")
                            return
            migration["daily_report_sections_v1"] = True
        self.state.save()

    async def initialize(self):
        self.sampler.start()
        self.report.start()
        self.daily_report.start()
        self.alerts.start()
        self.monitor.start()
        if self._audit_issues:
            for issue in self._audit_issues:
                log = logger.error if issue.level == "error" else logger.warning
                log("配置体检[%s] %s", issue.level, issue.message)
        else:
            logger.info("配置体检：未发现问题")
        logger.info(
            "StatusCard plugin initialized (data_dir=%s, alerts_enabled=%s, "
            "monitors_enabled=%s, daily_report_enabled=%s)",
            self.data_dir,
            cfg_bool(self.config, "alerts.enabled", False),
            cfg_bool(self.config, "monitors.enabled", False),
            cfg_bool(self.config, "daily_report.enabled", False),
        )

    async def terminate(self):
        await self.daily_report.stop()
        await self.report.stop()
        await self.monitor.stop()
        await self.alerts.stop()
        await self._alert_aggregator.stop()
        await self.sampler.stop()
        logger.info("StatusCard plugin terminated")

    # ------------------------------------------------------------------
    # status card
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查看状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """生成并发送当前设备的系统状态图片。"""
        event.should_call_llm(False)
        platform_name = ""
        try:
            platform_name = str(event.get_platform_name() or "")
        except Exception:
            pass
        if platform_name and not is_supported_platform(platform_name):
            yield event.plain_result(tr(self.config, "platform_unsupported"))
            return
        cooldown = max(
            0, min(3600, cfg_int(self.config, "display.cooldown_seconds", 10))
        )
        chat_key = str(getattr(event, "unified_msg_origin", "") or "")
        now = time.monotonic()
        if chat_key:
            if chat_key in self._status_inflight:
                yield event.plain_result(tr(self.config, "generating"))
                return
            if cooldown:
                last = self._status_cooldown.get(chat_key, 0.0)
                remaining = cooldown - (now - last)
                if remaining > 0:
                    yield event.plain_result(
                        tr(
                            self.config,
                            "cooldown",
                            seconds=max(1, int(remaining + 0.999)),
                        )
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
        if (
            cfg_bool(self.config, "display.privacy_notice", True)
            and chat_key
            and chat_key not in self._privacy_notified
        ):
            self._privacy_notified.add(chat_key)
            if len(self._privacy_notified) > 500:
                self._privacy_notified.clear()
                self._privacy_notified.add(chat_key)
            notices.append(tr(self.config, "privacy_notice"))
        if cfg_bool(self.config, "display.progress_notice", True):
            notices.append(tr(self.config, "progress_notice"))
        if notices:
            yield event.plain_result("\n".join(notices))
        try:
            image_url = await self._render_status_image(event)
            if chat_key:
                self._status_inflight.discard(chat_key)
            yield event.image_result(image_url)
        except Exception as exc:
            if chat_key:
                self._status_cooldown.pop(chat_key, None)
                self._status_inflight.discard(chat_key)
            self._diag.update(
                {
                    "last_error": exc.__class__.__name__,
                    "last_error_at": time.time(),
                }
            )
            self._save_diag()
            logger.exception("生成系统状态图片失败")
            # Prefer the local Pillow card over a plain text summary: the
            # T2I outage should degrade to a plain-looking image, not to no
            # image at all.
            try:
                collected = await collect_all(
                    context=self.context,
                    event=event,
                    state=self.state,
                    config=self.config,
                )
                png = await asyncio.to_thread(render_fallback_card, collected["view"])
                path = self._write_fallback_card(png)
                yield event.image_result(str(path))
                return
            except Exception:
                logger.exception("本地简化卡渲染失败，回退文字摘要")
            try:
                summary = await self.build_summary_text(event=event)
            except Exception:
                summary = tr(self.config, "summary_failed")
            yield event.plain_result(tr(self.config, "image_failed", summary=summary))
        finally:
            if chat_key:
                self._status_inflight.discard(chat_key)

    async def _render_status_image(self, event: AstrMessageEvent | None = None) -> str:
        collect_started = time.perf_counter()
        collected = await collect_all(
            context=self.context,
            event=event,
            state=self.state,
            config=self.config,
        )
        collect_ms = (time.perf_counter() - collect_started) * 1000
        background = await self._resolve_background()
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
        render_started = time.perf_counter()
        try:
            url = await asyncio.wait_for(
                self.html_render(html, {}, return_url=True, options=options),
                timeout=timeout,
            )
        except Exception:
            self._diag.update(
                {
                    "last_collect_ms": round(collect_ms, 1),
                    "last_render_ms": round(
                        (time.perf_counter() - render_started) * 1000, 1
                    ),
                    "last_render_ok": False,
                    "last_render_at": time.time(),
                }
            )
            if cfg_bool(self.config, "display.t2i_benchmark", False):
                logger.warning(
                    "T2I 状态卡渲染失败：%.1fms（采集 %.1fms）",
                    self._diag.get("last_render_ms", 0),
                    self._diag.get("last_collect_ms", 0),
                )
            self._save_diag()
            raise
        self._diag.update(
            {
                "last_collect_ms": round(collect_ms, 1),
                "last_render_ms": round(
                    (time.perf_counter() - render_started) * 1000, 1
                ),
                "last_render_ok": True,
                "last_render_at": time.time(),
            }
        )
        if cfg_bool(self.config, "display.t2i_benchmark", False):
            logger.info(
                "T2I 状态卡渲染完成：%.1fms（采集 %.1fms）",
                self._diag.get("last_render_ms", 0),
                self._diag.get("last_collect_ms", 0),
            )
        self._save_diag()
        return url

    async def build_status_image(self) -> str | None:
        """Render a status image without an incoming event (scheduled daily status image)."""
        try:
            return await self._render_status_image(event=None)
        except Exception:
            logger.exception("状态卡 T2I 渲染失败，改用本地简化卡")
            return await self._render_local_card_file()

    async def _render_local_card_file(self) -> str | None:
        """Draw the local Pillow fallback card and return its file path."""

        try:
            collected = await collect_all(
                context=self.context,
                event=None,
                state=self.state,
                config=self.config,
            )
            png = await asyncio.to_thread(render_fallback_card, collected["view"])
            return str(self._write_fallback_card(png))
        except Exception:
            logger.exception("本地简化卡渲染失败")
            return None

    def _write_fallback_card(self, png: bytes):
        """Write the fallback card under a unique name.

        The manual 查看状态 path and the scheduled daily image can hit the
        same T2I outage concurrently; a fixed file name made them overwrite
        each other's card. Old files are swept so the data dir does not grow.
        """

        path = self.data_dir / f"statuscard_fallback_{int(time.time() * 1000)}.png"
        path.write_bytes(png)
        try:
            for old in self.data_dir.glob("statuscard_fallback_*.png"):
                if old != path and old.stat().st_mtime < time.time() - 600:
                    old.unlink(missing_ok=True)
        except Exception:
            logger.debug("清理旧的本地简化卡文件失败")
        return path

    async def render_report_image(self, view: dict) -> str:
        """Render a daily-report view through the same T2I options as the card."""

        render_width = max(
            1170, min(2880, cfg_int(self.config, "display.render_width", 1170))
        )
        logical_width = max(650, min(1600, round(render_width / 1.8)))
        theme_raw = str(
            cfg_get(self.config, "daily_report.theme", "light") or "light"
        ).strip()
        theme = theme_raw.lower()
        if theme not in ("light", "dark"):
            if theme_raw:
                logger.warning(
                    "daily_report.theme 值 %r 不是 light/dark，已回退 light", theme_raw
                )
            theme = "light"
        html = build_report_html(view, render_width=logical_width, theme=theme)
        timeout = max(
            30,
            min(600, cfg_int(self.config, "daily_report.render_timeout", 180)),
        )
        options = {
            "type": "jpeg",
            "quality": 90,
            "full_page": True,
            "device_scale_factor_level": "ultra",
        }
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self.html_render(html, {}, return_url=True, options=options),
                timeout=timeout,
            )
        except Exception:
            self._diag.update(
                {
                    "last_report_render_ok": False,
                    "last_report_render_at": time.time(),
                    "last_report_render_ms": round(
                        (time.perf_counter() - started) * 1000, 1
                    ),
                }
            )
            if cfg_bool(self.config, "display.t2i_benchmark", False):
                logger.warning(
                    "T2I 日报渲染失败：%.1fms",
                    self._diag.get("last_report_render_ms", 0),
                )
            self._save_diag()
            raise
        self._diag.update(
            {
                "last_report_render_ok": True,
                "last_report_render_at": time.time(),
                "last_report_render_ms": round(
                    (time.perf_counter() - started) * 1000, 1
                ),
            }
        )
        if cfg_bool(self.config, "display.t2i_benchmark", False):
            logger.info(
                "T2I 日报渲染完成：%.1fms",
                self._diag.get("last_report_render_ms", 0),
            )
        self._save_diag()
        return result

    async def _resolve_background(self):
        source = effective_media_source(
            self.config,
            "background.background_source",
            "background.bg_upload_files",
            self.data_dir,
        )
        bg_bytes = None
        if source == "upload":
            uploaded = uploaded_file_path(
                self.config, "background.bg_upload_files", self.data_dir
            )
            if uploaded is not None:
                try:
                    if uploaded.stat().st_size > MAX_IMAGE_BYTES:
                        logger.warning("上传背景超过 10 MB，已跳过: %s", uploaded)
                    else:
                        bg_bytes = uploaded.read_bytes() or None
                except Exception as exc:
                    logger.warning("读取上传背景失败 %s: %s", uploaded, exc)
            if bg_bytes is None:
                logger.warning(
                    "背景来源为自定义上传，但没有可用的上传文件，回退内置背景"
                )

        return await resolve_background(prefer_bytes=bg_bytes)

    # ------------------------------------------------------------------
    # text / image helpers used by the alert engine
    # ------------------------------------------------------------------

    def _delivery_service(self) -> DeliveryService:
        """The service that pushes messages to a chat."""

        if self._delivery is None:
            self._delivery = DeliveryService(
                self.context,
                self.state,
                self._onebot_client,
                self._build_message_chain,
            )
        return self._delivery

    def _onebot_client(self) -> Any:
        """The OneBot client of the configured platform instance."""

        return _get_aiocqhttp_client(
            self.context,
            str(cfg_get(self.config, "alerts.platform_id", "default") or ""),
        )

    @staticmethod
    def _build_message_chain(payload: dict[str, Any]) -> MessageChain:
        """Build the AstrBot message chain for a delivery payload."""

        image = payload.get("image")
        if image:
            url = str(image)
            # Local fallback-card paths ride the same payload field as T2I
            # URLs; http(s) goes through the URL component, the rest is a
            # file on disk.
            if url.startswith(("http://", "https://")):
                return MessageChain([Comp.Image.fromURL(url)])
            return MessageChain([Comp.Image.fromFileSystem(url)])
        return MessageChain([Comp.Plain(str(payload.get("text") or ""))])

    async def send_text(
        self,
        targets: list[str],
        text: str,
        source: str = "unknown",
    ) -> DeliveryResult:
        return await self._delivery_service().send(
            targets, {"text": text}, kind="text", source=source
        )

    async def send_image(
        self,
        targets: list[str],
        image_url: str,
        source: str = "unknown",
    ) -> DeliveryResult:
        return await self._delivery_service().send(
            targets, {"image": image_url}, kind="image", source=source
        )

    async def build_summary_text(self, event: AstrMessageEvent | None = None) -> str:
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
            f"{tr(self.config, 'summary_host')}: {view['overview'][1]['value']}",
            f"{cpu['title']}: {cpu['percent']:.1f}%",
            f"RAM: {ram['percent']:.1f}% ({ram['desc'].splitlines()[0]})",
            f"SWAP: {swap['percent']:.1f}%",
        ]
        if disk:
            lines.append(
                f"{tr(self.config, 'summary_disk')} {disk['path']}: "
                f"{disk['percent_text']} ({disk['used']} / {disk['total']})"
            )
        lines.extend(
            [
                f"{tr(self.config, 'summary_network')}: ↑ {network['up']} ↓ {network['down']}",
                f"{tr(self.config, 'summary_month')}: ↑ {network['month_up']} ↓ {network['month_down']}",
                f"{tr(self.config, 'summary_process')}: 全部 {process['all']} / 运行 {process['running']} / 休眠 {process['sleeping']}",
                (
                    f"{tr(self.config, 'summary_friends')}: {bot.get('friends') if bot.get('friends') is not None else '--'}"
                    f" / {tr(self.config, 'summary_groups')}: {bot.get('groups') if bot.get('groups') is not None else '--'}"
                    f" / {tr(self.config, 'summary_members')}: {bot.get('members') if bot.get('members') is not None else '--'}"
                ),
                (
                    f"{tr(self.config, 'summary_recv')}: {bot.get('recv') if bot.get('recv') is not None else '--'}"
                    f" ({bot.get('recv_scope')})"
                    f" / {bot.get('sent') if bot.get('sent') is not None else '--'}"
                    f" ({bot.get('sent_scope')})"
                ),
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
        ok = await self.alerts.send_test(event.unified_msg_origin)
        yield event.plain_result(
            tr(self.config, "test_sent") if ok else tr(self.config, "test_failed")
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("立即检测")
    async def cmd_monitor_test(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        if not self.monitor.items:
            yield event.plain_result(tr(self.config, "monitor_no_targets"))
            return
        results = await self.monitor.test_all()
        failed = [item for item in results if not item["ok"]]
        lines = [
            tr(self.config, "monitor_result_title"),
            tr(
                self.config,
                "monitor_result_summary",
                ok=len(results) - len(failed),
                failed=len(failed),
            ),
        ]
        if failed:
            for item in failed:
                lines.append(
                    tr(
                        self.config,
                        "monitor_result_item",
                        name=item["name"],
                        kind=item["kind"],
                        detail=item["detail"],
                    )
                )
        else:
            lines.append(tr(self.config, "monitor_all_ok"))
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态卡诊断")
    async def cmd_diag(self, event: AstrMessageEvent):
        """Return a compact diagnostic summary without rendering an image."""
        event.should_call_llm(False)
        lines = ["🩺 状态卡诊断", "──────────────"]
        platform = ""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            pass
        lines.append(f"平台: {platform or '--'}")
        lines.append(f"插件: v{__version__}")
        if platform and platform != "aiocqhttp":
            lines.append("提示: 当前平台不是 aiocqhttp，查看状态会被拒绝。")

        platform_id = str(
            cfg_get(self.config, "alerts.platform_id", "default") or "default"
        )
        client = _get_aiocqhttp_client(self.context, platform_id)
        if client is None:
            lines.append("OneBot: 未找到 aiocqhttp 客户端")
            if cfg_bool(self.config, "alerts.enabled", False) and cfg_bool(
                self.config, "alerts.offline_notify", True
            ):
                lines.append(
                    "掉线告警: 已开启，但当前没有可用的 OneBot 连接，无法确认在线状态"
                )
        else:
            try:
                status = _normalize_object(
                    await _call_action(client, "get_status", timeout=3)
                )
                online = status.get("online")
                lines.append(
                    "OneBot: 客户端存在，online="
                    + ("unknown" if online is None else str(online))
                )
            except Exception as exc:
                lines.append(
                    "OneBot: 客户端存在，get_status 失败（"
                    + exc.__class__.__name__
                    + "）"
                )

        lines.append(
            "告警: "
            + ("开启" if cfg_bool(self.config, "alerts.enabled", False) else "关闭")
            + f"，目标 {len(self.alerts.targets)} 个"
        )
        daily_time = str(
            cfg_get(self.config, "alerts.daily_summary.time", "09:00") or "09:00"
        )
        lines.append(
            "每日定时状态图: "
            + (
                f"开启（{daily_time}）"
                if cfg_bool(self.config, "alerts.daily_summary.enabled", False)
                else "关闭"
            )
        )
        active_rules = notify_rules_active(self.config)
        lines.append(
            "通知中心: "
            + (
                f"启用（{active_rules} 条有效规则，三处旧推送目标不再生效）"
                if active_rules
                else "未启用（沿用三处推送目标）"
            )
        )
        monitor_status = self.monitor.status()
        silence = monitor_status.get("silence_until") or "无"
        lines.append(
            "服务监测: "
            + ("开启" if monitor_status.get("enabled") else "关闭")
            + f"，{len(self.monitor.items)} 项"
            + f"，静默: {silence}"
        )
        if monitor_status.get("silenced"):
            lines.append("服务监测当前处于维护静默，异常/恢复不会推送。")

        cgroup_limit = cgroup_memory_limit()
        if cgroup_limit:
            try:
                host_total = psutil.virtual_memory().total
            except Exception:
                host_total = 0
            if host_total and cgroup_limit < host_total * 0.95:
                lines.append(
                    "内存口径: 容器 cgroup 上限 "
                    f"{cgroup_limit / (1024**3):.1f} GB"
                    f"（宿主机 {host_total / (1024**3):.1f} GB；"
                    "内存使用率按宿主机计算，可能偏高）"
                )

        audit = self.audit_issues()
        if audit:
            lines.append(f"配置体检: {len(audit)} 项待处理")
            for issue in audit[:4]:
                tag = "错误" if issue.level == "error" else "提醒"
                lines.append(f"  [{tag}] {issue.message}")
        else:
            lines.append("配置体检: 未发现问题")

        if self._diag.get("last_render_at"):
            age = max(0, int(time.time() - float(self._diag["last_render_at"])))
            ok = "成功" if self._diag.get("last_render_ok") else "失败"
            lines.append(
                f"最近状态卡渲染: {ok}；采集 {self._diag.get('last_collect_ms', '--')}ms，"
                f"渲染 {self._diag.get('last_render_ms', '--')}ms，{age}s 前"
            )
        else:
            lines.append("最近状态卡渲染: 尚未渲染")
        if self._diag.get("last_report_render_at"):
            age = max(
                0,
                int(time.time() - float(self._diag["last_report_render_at"])),
            )
            ok = "成功" if self._diag.get("last_report_render_ok") else "失败"
            lines.append(
                f"最近日报渲染: {ok}；渲染 "
                f"{self._diag.get('last_report_render_ms', '--')}ms，{age}s 前"
            )
        else:
            lines.append("最近日报渲染: 尚未渲染")
        if self._diag.get("last_error"):
            lines.append(f"最近错误: {self._diag.get('last_error')}")

        delivery = self.state.get("delivery", {})
        if isinstance(delivery, dict) and delivery.get("ts"):
            age = max(0, int(time.time() - float(delivery["ts"])))
            uncertain_targets = delivery.get("uncertain_targets") or []
            if delivery.get("ok"):
                push_state = "部分成功" if delivery.get("partial") else "成功"
            elif uncertain_targets:
                push_state = "未确认（可能未送达）"
            else:
                push_state = "失败"
            lines.append(
                "最近主动推送: "
                + push_state
                + f"（{delivery.get('kind', '?')}，"
                + f"来源 {delivery.get('source', '未知')}，{age}s 前）"
            )
            if uncertain_targets:
                lines.append(
                    "未确认目标: "
                    + "、".join(str(item) for item in uncertain_targets[:3])
                )
            failed_targets = [
                item
                for item in (delivery.get("failed_targets") or [])
                if item not in uncertain_targets
            ]
            if failed_targets:
                lines.append(
                    "失败目标: " + "、".join(str(item) for item in failed_targets[:3])
                )
            receipts = delivery.get("receipts") or {}
            if isinstance(receipts, dict) and receipts:
                parts: list[str] = []
                for target, receipt in list(receipts.items())[:3]:
                    if not isinstance(receipt, dict):
                        continue
                    retcode = receipt.get("retcode")
                    message_id = receipt.get("message_id")
                    if receipt.get("ok"):
                        receipt_state = "成功"
                    elif target in uncertain_targets:
                        receipt_state = "未确认"
                    else:
                        receipt_state = "失败"
                    detail = f"retcode={retcode}"
                    if message_id is not None:
                        detail += f"，message_id={message_id}"
                    parts.append(f"{target}（{receipt_state}，{detail}）")
                if parts:
                    lines.append("OneBot 回执: " + "；".join(parts))
        else:
            lines.append("最近主动推送: 暂无记录")
        report_status = self.report.status()
        store_stats = report_status.get("store", {})
        if not self.report_store.available:
            lines.append(
                "日报数据库: 不可用（"
                + str(getattr(self.report_store, "init_error", None) or "初始化失败")
                + "，详见后台日志）"
            )
        lines.append(
            "日报数据: "
            + ("记录中" if report_status.get("enabled") else "未记录")
            + f"；资源 {store_stats.get('resources', 0)} 条"
            + f" / 快照 {store_stats.get('snapshots', 0)} 天"
            + f" / 事件 {store_stats.get('events', 0)} 条"
        )
        lines.append(
            "日报扩展: "
            + f"网站 {store_stats.get('websites', 0)} 条"
            + f" / 进程 {store_stats.get('processes', 0)} 条"
        )
        daily_status = self.daily_report.status()
        lines.append(
            "日报发送: "
            + ("开启" if daily_status.get("enabled") else "关闭")
            + f"，时间 {daily_status.get('time', '--')}"
            + f"，目标 {len(daily_status.get('targets') or [])} 个"
            + f"，内容 {','.join(daily_status.get('sections') or [])}"
        )
        if daily_status.get("next_run"):
            lines.append("下次日报: " + str(daily_status.get("next_run")))
        elif daily_status.get("enabled"):
            lines.append("下次日报: 待排程（任务可能未启动）")
        alarm_status = self.alerts.status()
        if alarm_status.get("daily_summary"):
            next_daily = alarm_status.get("daily_summary_next")
            running = alarm_status.get("daily_summary_running")
            lines.append(
                "下次每日状态图: "
                + (str(next_daily) if next_daily else "--")
                + ("（任务运行中）" if running else "（任务未运行）")
            )
        last_run = daily_status.get("last_run")
        if isinstance(last_run, dict):
            lines.append(
                "最近日报: "
                + ("成功" if last_run.get("ok") else "失败")
                + f"（{last_run.get('day') or '--'}，"
                + f"{str(last_run.get('detail') or '无详情')[:40]}）"
            )
        else:
            lines.append("最近日报: 暂无发送记录")
        lines.append("T2I: 由 AstrBot html_render 提供，最近一次结果见上。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("预览日报")
    async def cmd_preview_report(self, event: AstrMessageEvent):
        """Build and send yesterday's daily report to the current chat."""
        event.should_call_llm(False)
        # Rendering takes seconds; acknowledge immediately like 查看状态 does.
        yield event.plain_result(tr(self.config, "report_generating"))
        _ok, detail = await self.daily_report.preview(
            getattr(event, "unified_msg_origin", None)
        )
        yield event.plain_result(detail)
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("预览周报")
    async def cmd_preview_weekly(self, event: AstrMessageEvent):
        """Build and send the last-7-days weekly report to the current chat."""
        event.should_call_llm(False)
        yield event.plain_result(tr(self.config, "weekly_generating"))
        _ok, detail = await self.daily_report.preview(
            getattr(event, "unified_msg_origin", None), span_days=7
        )
        yield event.plain_result(detail)
        event.stop_event()
