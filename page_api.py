from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

import httpx
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
    cfg_bool,
    cfg_get,
    cfg_int,
    disk_usage,
    format_bytes,
    format_duration,
    get_timezone,
    memory_stat,
    now_in_timezone,
    process_info,
    resolve_targets,
    swap_stat,
)
from .provider_quality import today_overview
from .redactor import Redactor, read_rules
from .report_view import DailyReportBuilder, detect_resource_gaps
from .utils import normalize_targets
from .version import __version__, resolve_astrbot_version

PLUGIN_NAME = "astrbot_plugin_statuscard"
PLUGIN_VERSION = __version__


def jsonify(payload: Any) -> Any:
    """Return a JSON response for the current plugin Web handler."""

    if _json_response is None:
        raise RuntimeError("当前 AstrBot 不支持 astrbot.api.web，插件页面 API 不可用")
    return _json_response(payload)


def _timezone_name(config: Any) -> str:
    """The IANA zone the plugin actually uses, or "" when it falls back.

    ``get_timezone`` returns a fixed-offset tzinfo when the configured name is
    invalid; that has no ``key`` and browsers reject it in ``Intl`` options.
    An empty string lets the dashboard use the browser's local zone.
    """

    try:
        return str(getattr(get_timezone(config), "key", "") or "")
    except Exception:
        return ""


def _query_value(name: str, default: Any = None) -> Any:
    """Read a query parameter from the bound plugin request."""

    if _request is None:
        return default
    try:
        return _request.query.get(name, default)
    except Exception:
        return default


async def _request_value(name: str, default: Any = None) -> Any:
    """Read a value from the query string or a JSON request body.

    ``verify`` is a POST route because it really sends messages; the bridge
    sends its parameters as JSON, while the older GET handlers used query
    parameters. Accept both so a cached page keeps working.
    """

    value = _query_value(name, None)
    if value is not None:
        return value
    if _request is None:
        return default
    try:
        body = await _request.json()
    except Exception:
        return default
    if isinstance(body, dict) and name in body:
        return body[name]
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
            ("verify", self.verify, ["POST"], "立即验证"),
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
            # Keep the dashboard on the same used/total scale as the card.
            usage = disk_usage([path])[0]
            if usage.percent is None or usage.used is None or usage.total is None:
                raise RuntimeError(usage.exception or "磁盘信息不可用")
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
                    "pid": None if redact else int(row.get("pid") or 0),
                    "cpu": round(float(row.get("cpu_raw") or 0.0), 1),
                    "mem_mb": round(float(row.get("mem_mb") or 0.0), 1),
                }
            )
        return items

    def _snapshots_for_page(self, plugin: Any) -> list[dict[str, Any]]:
        """Raw snapshots, with the QQ counts removed under qq_stats redaction."""

        snapshots = plugin.report_store.list_snapshots(30)
        if "qq_stats" not in self._redact():
            return snapshots
        return [
            {
                "day": row.get("day"),
                "complete": row.get("complete"),
                "captured_at": row.get("captured_at"),
            }
            for row in snapshots
        ]

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
        try:
            provider_quality = await asyncio.to_thread(today_overview, plugin.config)
        except Exception:
            logger.exception("插件页面 LLM 服务质量概览失败")
            provider_quality = {
                "available": False,
                "empty_reason": "查询失败",
                "reason_code": "query_failed",
                "calls": 0,
                "completed": 0,
                "errors": 0,
                "aborted": 0,
                "success_rate": None,
                "success_text": "--",
                "error_streak": 0,
            }
        return jsonify(
            {
                "ok": True,
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": _timezone_name(plugin.config),
                "host": self._host(),
                "platform": self._platform(),
                "astrbot_version": self._astrbot_version(),
                "plugin_version": PLUGIN_VERSION,
                "uptime": format_duration(time.time() - psutil.boot_time()),
                "uptime_seconds": int(max(0, time.time() - psutil.boot_time())),
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
                "provider_quality": provider_quality,
                "audit": [issue.as_dict() for issue in plugin.audit_issues()],
            }
        )

    async def verify(self):
        """One-click checks triggered from the dashboard.

        ``kind=targets`` really sends a short message to every configured
        push target and reports the per-target receipt; ``kind=monitors``
        probes every monitor once without touching the alerting state
        machine; ``kind=webhook`` posts a simulated Komari event to the
        plugin's own listener so the receive -> parse -> push chain is
        exercised end to end.
        """

        kind = str(await _request_value("kind", "targets")).lower()
        plugin = self.plugin
        if kind == "targets":
            platform_id = str(
                cfg_get(plugin.config, "alerts.platform_id", "default") or "default"
            )
            merged: list[str] = []
            center_active = False
            # Verify exactly what real notifications would use: resolve each
            # kind through the notification center (which already filters
            # disabled rules and empty targets), and only fall back to the
            # legacy lists for kinds no rule covers.
            for notify_kind in (
                "resource",
                "service",
                "offline",
                "provider",
                "daily",
                "weekly",
                "daily_image",
            ):
                picked = resolve_targets(plugin.config, notify_kind)
                if picked is None:
                    continue
                center_active = True
                for one in picked:
                    if one not in merged:
                        merged.append(one)
            if not center_active:
                for key in (
                    "alerts.targets",
                    "monitors.targets",
                    "daily_report.targets",
                ):
                    raw = cfg_get(plugin.config, key, [])
                    if isinstance(raw, str):
                        raw = [raw]
                    for item in raw or []:
                        for one in normalize_targets([item], platform_id):
                            if one not in merged:
                                merged.append(one)
            if not merged:
                return jsonify({"ok": False, "error": "未配置任何推送目标"})
            text = (
                "🔔 状态卡验证消息：这是从看板触发的推送目标验证，"
                "收到即说明该目标配置正确。"
            )
            result = await plugin.send_text(merged, text, source="verify_targets")
            per_target = []
            for target in merged:
                receipt = (result.receipts or {}).get(target) or {}
                if target in (result.ok_targets or []):
                    state = "ok"
                elif target in (result.uncertain_targets or []):
                    state = "uncertain"
                else:
                    state = "failed"
                per_target.append(
                    {
                        "target": target,
                        "state": state,
                        "retcode": receipt.get("retcode"),
                        "message_id": receipt.get("message_id"),
                        "error": receipt.get("error") or "",
                    }
                )
            return jsonify({"ok": True, "kind": kind, "results": per_target})
        if kind == "monitors":
            monitor = getattr(plugin, "monitor", None)
            items = [
                item
                for item in (getattr(monitor, "items", None) or [])
                if not item.get("config_error")
            ]
            if not items:
                return jsonify(
                    {
                        "ok": True,
                        "kind": kind,
                        "results": [],
                        "note": "没有已启用的监测项",
                    }
                )

            async def probe_one(item: dict[str, Any]) -> dict[str, Any]:
                started = time.perf_counter()
                try:
                    ok, detail, _target = await monitor._probe(item)
                except Exception as exc:
                    ok, detail = False, f"探测异常: {exc.__class__.__name__}"
                return {
                    "name": item.get("name"),
                    "ok": bool(ok),
                    "detail": detail,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }

            results = await asyncio.gather(*(probe_one(item) for item in items))
            return jsonify({"ok": True, "kind": kind, "results": list(results)})
        if kind == "webhook":
            cfg = cfg_get(plugin.config, "alerts.komari_webhook", {}) or {}
            if not cfg_bool(cfg, "enabled", False):
                return jsonify(
                    {
                        "ok": True,
                        "kind": kind,
                        "listening": False,
                        "message": "Komari Webhook 未启用",
                    }
                )
            running = getattr(plugin.alerts, "_webhook_runner", None) is not None
            if not running:
                return jsonify(
                    {
                        "ok": True,
                        "kind": kind,
                        "listening": False,
                        "message": "Webhook 未在监听（可能启动失败，详见日志）",
                    }
                )
            port = max(1, min(65535, cfg_int(cfg, "port", 9968)))
            path = str(cfg_get(cfg, "path", "/webhook") or "/webhook")
            if not path.startswith("/"):
                path = "/" + path
            token = str(cfg_get(cfg, "token", "") or "")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"http://127.0.0.1:{port}{path}",
                        json={"message": "🔔 Komari Webhook 链路验证（看板模拟事件）"},
                        headers=headers,
                    )
                status = resp.status_code
            except Exception as exc:
                return jsonify(
                    {
                        "ok": True,
                        "kind": kind,
                        "listening": True,
                        "http_status": 0,
                        "message": f"请求失败: {exc.__class__.__name__}",
                    }
                )
            return jsonify(
                {
                    "ok": True,
                    "kind": kind,
                    "listening": True,
                    "http_status": status,
                }
            )
        return jsonify({"ok": False, "error": f"unknown kind: {kind}"})

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
        truncated = False
        if kind == "resources":
            fetched = await asyncio.to_thread(
                store.list_resources, start, limit=limit + 1
            )
            truncated = len(fetched) > limit
            rows = [row for row in fetched[:limit] if int(row.get("ts") or 0) <= end]
            if "network" in self._redact():
                # The overview and history honour the network rule; the CSV
                # export is a file people forward, so it must not leak the
                # exact up/down rates around it.
                rows = [
                    {
                        key: (None if key in ("net_up", "net_down") else value)
                        for key, value in row.items()
                    }
                    for row in rows
                ]
        elif kind == "processes":
            # cpu and mem are two independent tables; truncating after the
            # merge used to drop the whole mem half once cpu filled the cap.
            half = limit // 2
            cpu_rows = await asyncio.to_thread(
                store.list_process_samples, start, end + 60, "cpu", limit=half + 1
            )
            mem_rows = await asyncio.to_thread(
                store.list_process_samples, start, end + 60, "mem", limit=half + 1
            )
            truncated = len(cpu_rows) > half or len(mem_rows) > half
            rows = [dict(row) for row in cpu_rows[:half]]
            rows += [dict(row) for row in mem_rows[:half]]
            # process_samples stores the per-core percentage; the dashboard
            # shows whole-machine percentages. Export both so a CSV consumer
            # can tell which one it is looking at.
            cores = psutil.cpu_count(logical=True) or 1
            for row in rows:
                cpu = row.get("cpu")
                row["cpu_pct_total"] = (
                    round(float(cpu) / cores, 1) if cpu is not None else None
                )
            if "process" in self._redact():
                rows = [dict(row, name="已隐藏", pid=None) for row in rows]
        elif kind == "websites":
            fetched = await asyncio.to_thread(
                store.list_website_samples, start, end + 60, limit=limit + 1
            )
            truncated = len(fetched) > limit
            rows = fetched[-limit:]
        elif kind == "events":
            # One range query instead of one connection per day.
            fetched = await asyncio.to_thread(
                store.list_events_range, start, end, limit=limit + 1
            )
            truncated = len(fetched) > limit
            rows = fetched[:limit]
        else:
            return jsonify({"ok": False, "error": f"unknown kind: {kind}"})
        return jsonify(
            {
                "ok": True,
                "kind": kind,
                "range": range_key,
                "generated_at": end,
                "truncated": truncated,
                "rows": rows[:limit],
            }
        )

    # ------------------------------------------------------------------
    # GET /history
    # ------------------------------------------------------------------

    async def history(self):
        key = str(_query_value("range", "24h")).lower()
        # Echo the range that was actually used, never an unvalidated input.
        if key not in RANGE_SECONDS:
            key = "24h"
        seconds = RANGE_SECONDS[key]
        end = int(time.time())
        start = end - seconds
        # A 30-day window can materialise thousands of rows; keep the
        # synchronous SQLite scan off the event loop so the WebUI (and the
        # plugin's own background loops) stay responsive.
        fetched = await asyncio.to_thread(
            self.plugin.report_store.list_resources, start, 20001
        )
        truncated = len(fetched) > 20000
        rows = [row for row in fetched[:20000] if int(row.get("ts") or 0) <= end]
        sampled = self._downsample(rows)
        points = [
            {
                "ts": int(row["ts"]),
                "cpu": row.get("cpu"),
                "ram": row.get("ram"),
                "swap": row.get("swap"),
                "disk": row.get("disk"),
                "load1": row.get("load1"),
            }
            for row in sampled
        ]
        network_redacted = "network" in self._redact()
        if network_redacted:
            net_points: list[dict[str, Any]] = []
            network_note = "已按脱敏配置隐藏网络数据。"
            network_note_kind = "redacted"
        else:
            # Resource samples already carry the sampler's net rates, so reuse
            # the same rows: the network chart then follows the selected
            # 24h/7d/30d window instead of only the last ~2h in state.json.
            from_db = [
                {
                    "ts": int(row["ts"]),
                    "up": row.get("net_up"),
                    "down": row.get("net_down"),
                }
                for row in sampled
                if row.get("net_up") is not None or row.get("net_down") is not None
            ]
            if from_db:
                net_points = from_db
                network_note = (
                    "网络折线来自日报采样（默认 5 分钟一条），与资源趋势同一时间窗口。"
                )
                network_note_kind = "db"
            else:
                net = self.plugin.state.get("net", {}) or {}
                history = net.get("history") or []
                net_points = [
                    {
                        "ts": int(item[0]),
                        "up": float(item[1]),
                        "down": float(item[2]),
                    }
                    for item in history[-120:]
                    if isinstance(item, (list, tuple)) and len(item) >= 3
                ]
                network_note = (
                    "网络折线来自插件 state.json（仅最近约 2 小时）；"
                    "开启「记录日报历史数据」后可按所选范围显示。"
                )
                network_note_kind = "state"
        sample_interval = max(
            60,
            min(
                1800,
                cfg_int(
                    self.plugin.config,
                    "daily_report.resource_sample_seconds",
                    300,
                ),
            ),
        )
        # Detect on the raw rows, not the downsampled points: downsampling
        # stretches the spacing between samples far beyond the sampling
        # interval and would flag every normal pair as a gap.
        gaps = [
            gap
            for gap in detect_resource_gaps(
                [int(row.get("ts") or 0) for row in rows], 2 * sample_interval
            )
            if gap["end"] > start and gap["start"] < end
        ]
        return jsonify(
            {
                "ok": True,
                "range": key,
                "window_start": start,
                "window_end": end,
                "points": points,
                "gaps": gaps,
                "network": net_points,
                "network_redacted": network_redacted,
                "network_note": network_note,
                "network_note_kind": network_note_kind,
                "truncated": truncated,
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
        tz = now_in_timezone(self.plugin.config).tzinfo
        start_dt = datetime.combine(
            (now_in_timezone(self.plugin.config).date() - timedelta(days=days - 1)),
            dtime.min,
            tzinfo=tz,
        )
        # One range query instead of one connection per day.
        events = self.plugin.report_store.list_events_range(
            int(start_dt.timestamp()), int(time.time()) + 60, limit=2000
        )
        items: list[dict[str, Any]] = [
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
            for event in events
        ]
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
        page_size = 200
        return jsonify(
            {
                "ok": True,
                "days": days,
                "items": items[:page_size],
                "counts": counts,
                "truncated": len(items) > page_size,
            }
        )

    # ------------------------------------------------------------------
    # GET /reports
    # ------------------------------------------------------------------

    async def reports(self):
        plugin = self.plugin
        now_ts = time.time()
        fresh = str(_query_value("fresh", "0")) == "1"
        cached = self._reports_cache
        if cached and not fresh and now_ts - cached[0] < self._reports_cache_seconds:
            return jsonify(cached[1])
        view: dict[str, Any] = {}
        try:
            builder = DailyReportBuilder(
                plugin.report_store, plugin.config, redact=self._redactor().rules
            )
            sections = cfg_get(plugin.config, "daily_report.sections", None)
            view = await asyncio.to_thread(
                builder.build,
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
            # An empty payload used to render as a blank report page with no
            # explanation. Tell the page explicitly so it can offer a retry.
            return jsonify(
                {
                    "ok": False,
                    "error_code": "report_build_failed",
                    "error": "日报数据构建失败，请稍后重试",
                }
            )
        payload = {
            "ok": True,
            "day": view.get("day"),
            "sections": view.get("sections"),
            "summary": view.get("summary"),
            "resources": view.get("resources"),
            "anomalies": view.get("anomalies"),
            "qq": view.get("qq"),
            "token": view.get("token"),
            "provider_quality": view.get("provider_quality"),
            "website": view.get("website"),
            "process": view.get("process"),
            "runs": plugin.report_store.list_report_runs(limit=30),
        }
        self._reports_cache = (now_ts, payload)
        return jsonify(payload)
