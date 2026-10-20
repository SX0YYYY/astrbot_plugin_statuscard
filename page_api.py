from __future__ import annotations

import os
import socket
import time
from datetime import timedelta
from typing import Any

import psutil
from astrbot.api import logger

try:
    from quart import jsonify, request
except Exception:  # pragma: no cover - AstrBot normally provides Quart
    jsonify = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]

from .collectors import (
    cached_cpu_percent,
    cfg_get,
    format_bytes,
    format_duration,
    memory_stat,
    now_in_timezone,
    swap_stat,
)
from .report_view import DailyReportBuilder
from .version import __version__
from .version_resolver import resolve_astrbot_version

PLUGIN_NAME = "astrbot_plugin_statuscard"
PLUGIN_VERSION = __version__

RANGE_SECONDS = {
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}


class PageApi:
    """Dashboard page API for the statuscard plugin page.

    The page is served by AstrBot from ``pages/status`` and calls these routes
    through ``window.AstrBotPluginPage``. Only aggregated numeric data is
    returned; no message text, user IDs or UMO are exposed.
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._reports_cache: tuple[float, dict[str, Any]] | None = None
        self._reports_cache_seconds = 60.0

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def register(self) -> None:
        if jsonify is None or request is None:
            logger.warning("当前环境缺少 Quart，插件页面 API 不可用")
            return
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            logger.warning("当前 AstrBot 不支持 register_web_api，插件页面 API 不可用")
            return
        routes = (
            ("overview", self.overview, ["GET"], "状态概览"),
            ("history", self.history, ["GET"], "历史趋势"),
            ("events", self.events, ["GET"], "异常事件"),
            ("reports", self.reports, ["GET"], "日报摘要"),
        )
        for name, handler, methods, desc in routes:
            register(f"/{PLUGIN_NAME}/{name}", handler, methods, desc)
        logger.info("插件页面 API 已注册：%d 个路由", len(routes))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _redact(self) -> set[str]:
        raw = cfg_get(self.plugin.config, "display.redact", []) or []
        if isinstance(raw, str):
            raw = [raw]
        return {str(item).strip().lower() for item in raw if str(item).strip()}

    def _host(self) -> str:
        if "hostname" in self._redact():
            return "已隐藏"
        return socket.gethostname()

    @staticmethod
    def _network_view(redacted: bool, net: dict[str, Any]) -> dict[str, Any]:
        """Return the overview network payload, honouring ``network`` redaction."""

        if redacted:
            return {"redacted": True}
        return {
            "up_rate": round(float(net.get("up_rate") or 0.0), 1),
            "down_rate": round(float(net.get("down_rate") or 0.0), 1),
            "month_up_text": format_bytes(net.get("month_up")),
            "month_down_text": format_bytes(net.get("month_down")),
            "month": net.get("month") or "",
        }

    def _platform(self) -> str:
        return str(
            cfg_get(self.plugin.config, "alerts.platform_id", "default") or "default"
        )

    def _astrbot_version(self) -> str:
        try:
            return resolve_astrbot_version(self.plugin.context)
        except Exception:
            return ""

    def _disk(self) -> dict[str, Any]:
        paths = cfg_get(self.plugin.config, "display.disk_paths", None)
        if isinstance(paths, list) and paths:
            path = str(paths[0])
        else:
            path = os.path.abspath(os.sep)
        try:
            usage = psutil.disk_usage(path)
            return {
                "path": path,
                "percent": round(float(usage.percent), 1),
                "used": int(usage.used),
                "total": int(usage.total),
                "used_text": format_bytes(usage.used),
                "total_text": format_bytes(usage.total),
            }
        except Exception:
            return {
                "path": path,
                "percent": None,
                "used": None,
                "total": None,
                "used_text": "--",
                "total_text": "--",
            }

    def _latest_processes(self, ts: int) -> list[dict[str, Any]]:
        store = self.plugin.report_store
        rows = store.list_process_samples(ts - 900, ts + 60, "cpu")
        if not rows:
            return []
        latest = max(int(row.get("ts") or 0) for row in rows)
        rows = [row for row in rows if int(row.get("ts") or 0) == latest]
        redact = "process" in self._redact()
        items = []
        for row in sorted(rows, key=lambda item: int(item.get("rank") or 99)):
            items.append(
                {
                    "rank": int(row.get("rank") or 0),
                    "name": "已隐藏" if redact else str(row.get("name") or ""),
                    "cpu": round(float(row.get("cpu") or 0.0), 1),
                    "mem_mb": round(float(row.get("mem_mb") or 0.0), 1),
                }
            )
        return items

    @staticmethod
    def _downsample(
        rows: list[dict[str, Any]], limit: int = 120
    ) -> list[dict[str, Any]]:
        if len(rows) <= limit:
            return rows
        step = len(rows) / limit
        indexes = [max(0, int(index * step)) for index in range(limit)]
        indexes[-1] = len(rows) - 1
        return [rows[index] for index in indexes]

    # ------------------------------------------------------------------
    # GET /overview
    # ------------------------------------------------------------------

    async def overview(self):
        plugin = self.plugin
        now = now_in_timezone(plugin.config)
        ts = int(now.timestamp())
        mem = memory_stat()
        swap = swap_stat()
        net = plugin.state.get("net", {}) or {}
        redact = self._redact()
        network_view = self._network_view("network" in redact, net)
        load1: float | None = None
        if hasattr(os, "getloadavg"):
            try:
                load1 = round(float(os.getloadavg()[0]), 2)
            except Exception:
                load1 = None

        monitor_status = plugin.monitor.status()
        monitor_items: list[dict[str, Any]] = []
        ok_count = fail_count = unknown_count = 0
        for item in monitor_status.get("items", []):
            state = item.get("state") or {}
            checked = state.get("last_checked")
            alerting = bool(state.get("alerting"))
            if not checked:
                status = "unknown"
                unknown_count += 1
            elif alerting:
                status = "fail"
                fail_count += 1
            else:
                status = "ok"
                ok_count += 1
            monitor_items.append(
                {
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "status": status,
                    "last_checked": checked,
                    "last_ok": state.get("last_ok"),
                    "last_error": str(state.get("last_error") or "")[:160],
                }
            )

        daily_status = plugin.daily_report.status()
        return jsonify(
            {
                "ok": True,
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "host": self._host(),
                "platform": self._platform(),
                "astrbot_version": self._astrbot_version(),
                "plugin_version": PLUGIN_VERSION,
                "uptime": format_duration(time.time() - psutil.boot_time()),
                "cpu": {
                    "percent": round(cached_cpu_percent(), 1),
                    "count": psutil.cpu_count(logical=True) or 1,
                    "load1": load1,
                },
                "memory": {
                    "percent": round(float(mem.percent), 1),
                    "used_text": format_bytes(mem.used),
                    "total_text": format_bytes(mem.total),
                },
                "swap": {
                    "percent": round(float(swap.percent), 1),
                    "used_text": format_bytes(swap.used),
                    "total_text": format_bytes(swap.total),
                },
                "disk": self._disk(),
                "network": network_view,
                "network_redacted": "network" in redact,
                "monitors": {
                    "enabled": bool(monitor_status.get("enabled")),
                    "total": len(monitor_items),
                    "ok": ok_count,
                    "fail": fail_count,
                    "unknown": unknown_count,
                    "items": monitor_items,
                },
                "processes": self._latest_processes(ts),
                "daily_report": {
                    "enabled": daily_status.get("enabled"),
                    "time": daily_status.get("time"),
                    "sections": daily_status.get("sections"),
                    "last_run": daily_status.get("last_run"),
                },
                "store": plugin.report_store.stats(),
            }
        )

    # ------------------------------------------------------------------
    # GET /history
    # ------------------------------------------------------------------

    async def history(self):
        key = str(request.args.get("range", "24h")).lower()
        seconds = RANGE_SECONDS.get(key, 86400)
        end = int(time.time())
        start = end - seconds
        rows = [
            row
            for row in self.plugin.report_store.list_resources(start)
            if int(row.get("ts") or 0) <= end
        ]
        points = [
            {
                "ts": int(row["ts"]),
                "cpu": row.get("cpu"),
                "ram": row.get("ram"),
                "swap": row.get("swap"),
                "disk": row.get("disk"),
                "load1": row.get("load1"),
            }
            for row in self._downsample(rows)
        ]
        net = self.plugin.state.get("net", {}) or {}
        history = net.get("history") or []
        network_redacted = "network" in self._redact()
        if network_redacted:
            net_points: list[dict[str, Any]] = []
            network_note = "已按脱敏配置隐藏网络数据。"
        else:
            net_points = [
                {
                    "ts": int(item[0]),
                    "up": float(item[1]),
                    "down": float(item[2]),
                }
                for item in history[-120:]
                if isinstance(item, (list, tuple)) and len(item) >= 3
            ]
            network_note = "网络折线来自插件 state.json，最多保留最近 180 个采样点。"
        return jsonify(
            {
                "ok": True,
                "range": key,
                "points": points,
                "network": net_points,
                "network_redacted": network_redacted,
                "network_note": network_note,
            }
        )

    # ------------------------------------------------------------------
    # GET /events
    # ------------------------------------------------------------------

    async def events(self):
        try:
            days = int(request.args.get("days", 7))
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(30, days))
        today = now_in_timezone(self.plugin.config).date()
        items: list[dict[str, Any]] = []
        for offset in range(days):
            day = (today - timedelta(days=offset)).isoformat()
            for event in self.plugin.report_store.list_events(day, limit=200):
                items.append(
                    {
                        "id": event.get("id"),
                        "ts": event.get("ts"),
                        "day": event.get("day"),
                        "kind": event.get("kind"),
                        "name": event.get("name"),
                        "detail": str(event.get("detail") or "")[:200],
                        "status": event.get("status"),
                        "resolved_ts": event.get("resolved_ts"),
                    }
                )
        items.sort(
            key=lambda item: (
                int(item.get("ts") or 0),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("status") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return jsonify(
            {
                "ok": True,
                "days": days,
                "items": items[:200],
                "counts": counts,
            }
        )

    # ------------------------------------------------------------------
    # GET /reports
    # ------------------------------------------------------------------

    async def reports(self):
        plugin = self.plugin
        now_ts = time.time()
        cached = self._reports_cache
        if cached and now_ts - cached[0] < self._reports_cache_seconds:
            return jsonify(cached[1])
        view: dict[str, Any] = {}
        try:
            builder = DailyReportBuilder(plugin.report_store, plugin.config)
            sections = cfg_get(plugin.config, "daily_report.sections", None)
            view = builder.build(
                day=None,
                history_days=7,
                sections=sections,
                meta={
                    "host": self._host(),
                    "astrbot_version": self._astrbot_version(),
                },
            )
        except Exception:
            logger.exception("插件页面聚合日报失败")
        payload = {
            "ok": True,
            "day": view.get("day"),
            "sections": view.get("sections"),
            "summary": view.get("summary"),
            "resources": view.get("resources"),
            "anomalies": view.get("anomalies"),
            "qq": view.get("qq"),
            "token": view.get("token"),
            "website": view.get("website"),
            "process": view.get("process"),
            "runs": plugin.report_store.list_report_runs(limit=30),
            "snapshots": plugin.report_store.list_snapshots(30),
        }
        self._reports_cache = (now_ts, payload)
        return jsonify(payload)
