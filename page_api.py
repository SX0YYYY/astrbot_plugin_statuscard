from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import timedelta
from typing import Any

import psutil
from astrbot.api import logger

try:
    # The official helper works on every supported AstrBot version and does
    # not depend on the web framework the dashboard happens to use.
    from astrbot.api.web import json_response as _json_response
    from astrbot.api.web import request as _request
except Exception:  # pragma: no cover - very old AstrBot builds
    _json_response = None  # type: ignore[assignment]
    _request = None  # type: ignore[assignment]

from .collectors import (
    cached_cpu_percent,
    cfg_get,
    cfg_int,
    format_bytes,
    format_duration,
    memory_stat,
    now_in_timezone,
    process_info,
    swap_stat,
)
from .redactor import Redactor, read_rules
from .report_view import DailyReportBuilder
from .version import __version__, resolve_astrbot_version

PLUGIN_NAME = "astrbot_plugin_statuscard"
PLUGIN_VERSION = __version__


def jsonify(payload: Any) -> Any:
    """Return a JSON response for the current plugin Web handler."""

    if _json_response is None:
        raise RuntimeError("当前 AstrBot 不支持 astrbot.api.web，插件页面 API 不可用")
    return _json_response(payload)


def _query_value(name: str, default: Any = None) -> Any:
    """Read a query parameter from the bound plugin request."""

    if _request is None:
        return default
    try:
        return _request.query.get(name, default)
    except Exception:
        return default


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
        if _json_response is None or _request is None:
            logger.warning("当前 AstrBot 缺少 astrbot.api.web，插件页面 API 不可用")
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
            ("export", self.export, ["GET"], "导出历史数据"),
        )
        for name, handler, methods, desc in routes:
            register(f"/{PLUGIN_NAME}/{name}", handler, methods, desc)
        logger.info("插件页面 API 已注册：%d 个路由", len(routes))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _redactor(self) -> Redactor:
        """The redaction rules that apply to the Web dashboard.

        The dashboard has its own switch, completely independent of the QQ
        status card: an operator can hide details in the WebUI without also
        hiding them on the card, and the other way around. An empty list
        means the dashboard shows everything - it does not inherit the card
        rules, because silently reusing another switch would hide data the
        operator never asked to hide here.
        """

        return Redactor(read_rules(self.plugin.config, "display.page_redact"))

    def _redact(self) -> set[str]:
        """The rule names that apply to the Web dashboard."""

        return self._redactor().rules

    def _host(self) -> str:
        return self._redactor().dashboard_host(socket.gethostname())

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

    async def _latest_processes(self, top_n: int) -> list[dict[str, Any]]:
        """Return the same ten-second snapshot the status card renders.

        The dashboard used to read the five-minute process_samples table,
        which is a half-second instant sample and can disagree wildly with
        the card. Both surfaces now share the sampler's delta snapshot.
        """

        try:
            snapshot = await asyncio.to_thread(process_info, top_n)
        except Exception as exc:
            logger.debug("读取实时进程快照失败: %s", exc)
            return []
        redact = "process" in self._redact()
        items = []
        for index, row in enumerate(snapshot.get("top") or [], 1):
            items.append(
                {
                    "rank": index,
                    "name": "已隐藏" if redact else str(row.get("name") or ""),
                    "cpu": round(float(row.get("cpu_raw") or 0.0), 1),
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
        ok_count = fail_count = unknown_count = config_error_count = 0
        for item in monitor_status.get("items", []):
            state = item.get("state") or {}
            checked = state.get("last_checked")
            alerting = bool(state.get("alerting"))
            config_error = item.get("config_error")
            if config_error:
                status = "config_error"
                config_error_count += 1
            elif not checked:
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
                    "config_error": config_error,
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
                    "config_error": config_error_count,
                    "items": monitor_items,
                },
                "processes": await self._latest_processes(
                    max(
                        1,
                        min(
                            30,
                            cfg_int(plugin.config, "display.process_top_n", 8),
                        ),
                    )
                ),
                "daily_report": {
                    "enabled": daily_status.get("enabled"),
                    "time": daily_status.get("time"),
                    "sections": daily_status.get("sections"),
                    "last_run": daily_status.get("last_run"),
                },
                "audit": [issue.as_dict() for issue in plugin.audit_issues()],
                "store": plugin.report_store.stats(),
            }
        )

    # ------------------------------------------------------------------
    # GET /export
    # ------------------------------------------------------------------

    async def export(self):
        """Return raw history rows; the page turns them into a CSV file.

        The rows are aggregated numbers only (timestamps, percentages,
        counters, process names honouring the dashboard redaction) - never
        message text or user IDs.
        """

        kind = str(_query_value("kind", "resources")).lower()
        range_key = str(_query_value("range", "7d")).lower()
        seconds = RANGE_SECONDS.get(range_key, 86400 * 7)
        end = int(time.time())
        start = end - seconds
        store = self.plugin.report_store
        limit = 20000
        if kind == "resources":
            rows = [
                row
                for row in store.list_resources(start, limit=limit)
                if int(row.get("ts") or 0) <= end
            ]
        elif kind == "processes":
            rows = store.list_process_samples(start, end + 60, "cpu")
            rows += store.list_process_samples(start, end + 60, "mem")
            if "process" in self._redact():
                rows = [dict(row, name="已隐藏") for row in rows]
        elif kind == "websites":
            rows = store.list_website_samples(start, end + 60)
        elif kind == "events":
            now = now_in_timezone(self.plugin.config)
            days = int(seconds / 86400) + 2
            rows = []
            for offset in range(days):
                day = (now - timedelta(days=offset)).date().isoformat()
                rows += store.list_events(day, limit=5000)
            rows = [row for row in rows if int(row.get("ts") or 0) >= start]
        else:
            return jsonify({"ok": False, "error": f"unknown kind: {kind}"})
        return jsonify(
            {
                "ok": True,
                "kind": kind,
                "range": range_key,
                "generated_at": end,
                "rows": rows[:limit],
            }
        )

    # ------------------------------------------------------------------
    # GET /history
    # ------------------------------------------------------------------

    async def history(self):
        key = str(_query_value("range", "24h")).lower()
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
            days = int(_query_value("days", 7) or 7)
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
            builder = DailyReportBuilder(
                plugin.report_store, plugin.config, redact=self._redactor().rules
            )
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
