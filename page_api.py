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
    disk_usage_bounded,
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
from .utils import normalize_targets, redact_url
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


def _mask_umo(target: Any) -> str:
    """Hide the middle of a chat id inside a UMO.

    ``default:GroupMessage:461961169`` becomes
    ``default:GroupMessage:*****1169``: the verification list still tells
    targets apart, but the browser never receives a full UMO (the class
    docstring promises no UMO is exposed).
    """

    text = str(target or "")
    head, _, tail = text.rpartition(":")
    digits = "".join(ch for ch in tail if ch.isdigit())
    # Short IDs used to pass through untouched, which leaked the UMO the
    # class docstring promises never to expose; mask anything digit-bearing.
    if not digits:
        return text
    keep = min(4, max(1, len(digits) - 4)) if len(digits) > 4 else 1
    masked = "*" * (len(digits) - keep) + digits[len(digits) - keep :]
    return f"{head}:{masked}" if head else masked


class PageApi:
    """Dashboard page API for the statuscard plugin page.

    The page is served by AstrBot from ``pages/status`` and calls these routes
    through ``window.AstrBotPluginPage``. Only aggregated numeric data is
    returned; no message text, user IDs or UMO are exposed.
    """

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._reports_cache: tuple[float, dict[str, Any], tuple] | None = None
        self._reports_cache_seconds = 60.0

    # ------------------------------------------------------------------
    # degraded store gate
    # ------------------------------------------------------------------

    def _db_unavailable_payload(self) -> dict[str, Any]:
        """Shared marker + empty payload for a broken report store.

        v2.18.9 taught the send path to retry via ``ensure_ready``; the
        dashboard routes never checked, so a broken store looked like "no
        data was ever recorded" -- the most misleading empty state there is.
        The page gets an explicit ``db_unavailable`` marker instead, and one
        recovery attempt is kicked off in the background so the next refresh
        can already succeed.
        """

        store = self.plugin.report_store
        if store.available:
            return None
        try:
            asyncio.get_running_loop().create_task(
                asyncio.to_thread(store.ensure_ready)
            )
        except Exception:
            logger.debug("报表库后台恢复任务创建失败", exc_info=True)
        return {
            "ok": True,
            "db_unavailable": True,
            "reason_code": "report_db_unavailable",
        }

    # ------------------------------------------------------------------
    # routes
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

    def _mask_host_text(self, text: str) -> str:
        """Hide probe errors that embed an internal host or address.

        Delegates to the shared redactor: only address-shaped text is
        replaced, so "HTTP 500 (expected 200)" and "probe timed out" stay
        readable while ``10.0.0.12:9000`` and URLs do not reach the browser.
        """

        return self._redactor().mask_addresses(text)

    def _disk(self) -> dict[str, Any]:
        paths = cfg_get(self.plugin.config, "display.disk_paths", None)
        if isinstance(paths, list):
            configured = [str(item) for item in paths if str(item).strip()]
        else:
            configured = []
        if not configured:
            configured = [os.path.abspath(os.sep)]
        try:
            # Keep the dashboard on the same used/total scale as the card.
            usages = disk_usage_bounded(configured)
        except Exception:
            usages = []
        mounts: list[dict[str, Any]] = []
        for usage in usages:
            if usage.percent is None or usage.used is None or usage.total is None:
                continue
            mounts.append(
                {
                    "path": str(usage.name or ""),
                    "percent": round(float(usage.percent), 1),
                    "used": int(usage.used),
                    "total": int(usage.total),
                    "used_text": format_bytes(usage.used),
                    "total_text": format_bytes(usage.total),
                }
            )
        if not mounts:
            return {
                "path": configured[0],
                "percent": None,
                "used": None,
                "total": None,
                "used_text": "--",
                "total_text": "--",
                "disks": [],
            }
        # The disk alert watches every mount, so the card shows the worst one
        # instead of silently reporting the first (a filling second disk used
        # to stay invisible here). ``worst`` is an element of ``mounts``, so
        # the list must be attached to a copy -- putting it on the element
        # itself created a cycle and the JSON response blew the stack.
        worst = max(mounts, key=lambda item: item["percent"])
        return {**worst, "disks": mounts}

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
        degraded_count = 0
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
            elif state.get("fails"):
                # Failed the latest probe but has not reached the alert
                # threshold (or is suppressed by a dependency). Showing a
                # green dot and hiding last_error here told the operator
                # "everything is fine" while the service was already down.
                status = "degraded"
                degraded_count += 1
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
                    "last_error": self._mask_host_text(
                        str(state.get("last_error") or "")[:160]
                    ),
                    "config_error": config_error,
                }
            )

        # status() runs a SQLite read (latest report run); like the disk and
        # provider queries below it must not run on the event loop -- a busy
        # store lock stalled every message for up to 5 seconds.
        daily_status = await asyncio.to_thread(plugin.daily_report.status)
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
                "disk": await asyncio.to_thread(self._disk),
                "network": network_view,
                "network_redacted": "network" in redact,
                "monitors": {
                    "enabled": bool(monitor_status.get("enabled")),
                    "total": len(monitor_items),
                    "ok": ok_count,
                    "fail": fail_count,
                    "degraded": degraded_count,
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

        kind = str(await _request_value("kind", "monitors")).lower()
        plugin = self.plugin
        if kind == "targets":
            platform_id = str(
                cfg_get(plugin.config, "alerts.platform_id", "default") or "default"
            )
            merged: list[str] = []
            # Verify exactly what real notifications would use: resolve each
            # kind through the notification center (which already filters
            # disabled rules and empty targets) and fall back to that kind's
            # legacy list only when no rule covers it. A single global gate
            # ("any rule exists") skipped the legacy targets of every kind the
            # rules did not cover, so a channel that really receives alerts
            # was never tested.
            legacy_by_kind = (
                ("resource", "alerts.targets"),
                ("service", "monitors.targets"),
                ("offline", "alerts.targets"),
                ("provider", "alerts.targets"),
                ("daily", "daily_report.targets"),
                ("weekly", "daily_report.targets"),
                ("daily_image", "alerts.targets"),
            )
            for notify_kind, legacy_key in legacy_by_kind:
                picked = resolve_targets(plugin.config, notify_kind)
                if picked is None:
                    raw = cfg_get(plugin.config, legacy_key, [])
                    if isinstance(raw, str):
                        raw = [raw]
                    picked = normalize_targets(raw or [], platform_id)
                for one in picked:
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
                        # Keep enough to tell targets apart without echoing
                        # full UMOs (the class contract promises no UMO).
                        "target": _mask_umo(target),
                        "state": state,
                        "retcode": receipt.get("retcode"),
                        "message_id": receipt.get("message_id"),
                        "error": receipt.get("error") or "",
                    }
                )
            return jsonify({"ok": True, "kind": kind, "results": per_target})
        if kind == "monitors":
            monitor = getattr(plugin, "monitor", None)
            all_items = list(getattr(monitor, "items", None) or [])
            items = [item for item in all_items if not item.get("config_error")]
            if not items:
                # "没有已启用的监测项" used to be the answer even when every
                # item was enabled but incomplete -- it pointed the operator
                # at the wrong fix. Distinguish the two cases.
                broken = sum(1 for item in all_items if item.get("config_error"))
                note = (
                    f"{broken} 个监测项配置不完整（缺少 URL/端口等），请在配置页补全后重试"
                    if broken
                    else "没有已启用的监测项"
                )
                return jsonify(
                    {
                        "ok": True,
                        "kind": kind,
                        "results": [],
                        "note": note,
                    }
                )

            # Dozens of monitor targets used to be probed at once on a
            # single click, which spikes the sites being watched and keeps the
            # request open for the slowest probe.
            semaphore = asyncio.Semaphore(6)

            async def probe_one(item: dict[str, Any]) -> dict[str, Any]:
                started = time.perf_counter()
                try:
                    async with semaphore:
                        # The clock restarts inside the semaphore: with more
                        # items than slots, queueing time used to be billed
                        # to the probe itself and the last items showed
                        # seconds of "waiting" as their latency.
                        started = time.perf_counter()
                        ok, detail, _target = await monitor._probe(item)
                except Exception as exc:
                    ok, detail = False, f"探测异常: {exc.__class__.__name__}"
                return {
                    "name": item.get("name"),
                    "ok": bool(ok),
                    "detail": self._mask_host_text(str(detail or "")),
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
            # Probe the address the listener actually binds. A wildcard bind
            # is reachable through loopback, but a listener pinned to one NIC
            # used to fail this check even though the chain was healthy.
            host = str(cfg_get(cfg, "host", "127.0.0.1") or "127.0.0.1").strip()
            if host in {"0.0.0.0", "::", ""}:
                host = "127.0.0.1"
            elif ":" in host and not host.startswith("["):
                host = f"[{host}]"
            token = str(cfg_get(cfg, "token", "") or "")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"http://{host}:{port}{path}",
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
        if range_key not in RANGE_SECONDS:
            # /history already normalises; the export echoed the raw value
            # while silently using the 7-day window.
            range_key = "7d"
        gate = self._db_unavailable_payload()
        if gate:
            return jsonify({**gate, "kind": kind, "range": range_key, "rows": []})
        seconds = RANGE_SECONDS[range_key]
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
            # list_resources returns ascending rows, so the newest ones are
            # at the end; cutting [:limit] dropped the latest samples while
            # the toast promised "the most recent records".
            rows = [row for row in fetched[-limit:] if int(row.get("ts") or 0) <= end]
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
            rows = [dict(row) for row in cpu_rows[-half:]]
            rows += [dict(row) for row in mem_rows[-half:]]
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
            # Rows written before v2.18.7 may carry the full URL (with a
            # token in the query) as the name; the CSV is exactly what gets
            # forwarded to other people, so strip query and credentials here
            # like the daily report already does.
            rows = [
                {
                    **row,
                    "name": redact_url(str(row.get("name") or "")),
                }
                for row in fetched[-limit:]
            ]
        elif kind == "events":
            # One range query instead of one connection per day.
            fetched = await asyncio.to_thread(
                store.list_events_range, start, end, limit=limit + 1
            )
            truncated = len(fetched) > limit
            # Same whitelist as /events: the CSV is forwarded to other people
            # and ``target`` carries probe URLs.
            rows = [
                {
                    "ts": row.get("ts"),
                    "day": row.get("day"),
                    "kind": row.get("kind"),
                    "name": row.get("name"),
                    "detail": self._mask_host_text(str(row.get("detail") or "")),
                    "status": row.get("status"),
                    "resolved_ts": row.get("resolved_ts"),
                }
                for row in fetched[-limit:]
            ]
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
        gate = self._db_unavailable_payload()
        if gate:
            return jsonify(
                {
                    **gate,
                    "range": key,
                    "window_start": None,
                    "window_end": None,
                    "points": [],
                    "gaps": [],
                    "network": [],
                    "network_note": "",
                }
            )
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
        # list_resources returns ascending rows, so the newest sample is at
        # the end: slicing from the front dropped "now" from the chart. Keep
        # the tail, matching the export endpoint.
        rows = [row for row in fetched[-20000:] if int(row.get("ts") or 0) <= end]
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
                    for item in history[-60:]
                    if isinstance(item, (list, tuple)) and len(item) >= 3
                ]
                network_note = (
                    "网络折线来自插件 state.json（默认 60 个采样点，约 1 小时）；"
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
        gate = self._db_unavailable_payload()
        if gate:
            return jsonify(
                {
                    **gate,
                    "days": days,
                    "counts_limit": 2000,
                    "counts_truncated": False,
                    "items": [],
                    "counts": {},
                    "truncated": False,
                }
            )
        tz = now_in_timezone(self.plugin.config).tzinfo
        start_dt = datetime.combine(
            (now_in_timezone(self.plugin.config).date() - timedelta(days=days - 1)),
            dtime.min,
            tzinfo=tz,
        )
        # One range query instead of one connection per day.
        events = await asyncio.to_thread(
            self.plugin.report_store.list_events_range,
            int(start_dt.timestamp()),
            int(time.time()) + 60,
            limit=2000,
        )
        items: list[dict[str, Any]] = [
            {
                "id": event.get("id"),
                "ts": event.get("ts"),
                "day": event.get("day"),
                "kind": event.get("kind"),
                "name": event.get("name"),
                "detail": self._mask_host_text(str(event.get("detail") or "")[:200]),
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
                "counts_limit": 2000,
                # The counters are computed from the same capped query, so the
                # frontend can say "statistics cover the latest 2000 rows"
                # instead of showing a silently low total.
                "counts_truncated": len(events) >= 2000,
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
        gate = self._db_unavailable_payload()
        if gate:
            # An explicit degraded marker instead of "empty sections": the
            # page keeps whatever it already rendered and shows a notice.
            return jsonify({**gate, "day": None})
        now_ts = time.time()
        fresh = str(_query_value("fresh", "0")) == "1"
        # The cache key carries the date, the redaction rules and the report
        # content selection: a single slot used to serve yesterday's report
        # right after midnight, an unredacted payload for a minute after
        # page_redact was turned on, or old sections after the operator
        # changed daily_report.sections.
        sections_cfg = cfg_get(plugin.config, "daily_report.sections", None)
        cache_key = (
            now_in_timezone(plugin.config).date().isoformat(),
            tuple(sorted(self._redact())),
            repr(sections_cfg),
        )
        cached = self._reports_cache
        if (
            cached
            and not fresh
            and now_ts - cached[0] < self._reports_cache_seconds
            and cached[2] == cache_key
        ):
            return jsonify(cached[1])
        view: dict[str, Any] = {}
        try:
            builder = DailyReportBuilder(
                plugin.report_store, plugin.config, redact=self._redactor().rules
            )
            sections = sections_cfg
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
            "runs": await asyncio.to_thread(
                plugin.report_store.list_report_runs, limit=30
            ),
        }
        self._reports_cache = (now_ts, payload, cache_key)
        return jsonify(payload)
