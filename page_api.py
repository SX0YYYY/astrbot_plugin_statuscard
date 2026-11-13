from __future__ import annotations

import asyncio
import copy
import os
import re
import socket
import time
from datetime import date, datetime, timedelta
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
from .i18n import tr
from .platform_support import adapter_platform_name, find_adapter
from .provider_quality import today_overview
from .redactor import Redactor, read_rules
from .report_view import DailyReportBuilder, detect_resource_gaps
from .utils import (
    mask_chat_ids,
    mask_umo,
    normalize_targets,
    redact_url,
    safe_float,
    safe_int,
)
from .version import __version__, resolve_astrbot_version

PLUGIN_NAME = "astrbot_plugin_statuscard"
PLUGIN_VERSION = __version__


def jsonify(payload: Any) -> Any:
    """Return a JSON response for the current plugin Web handler."""

    if _json_response is None:
        raise RuntimeError("当前 AstrBot 不支持 astrbot.api.web，插件页面 API 不可用")
    return _json_response(_sanitize_json(payload))


def _sanitize_json(value: Any) -> Any:
    """Replace non-finite floats so the payload is valid JSON.

    SQLite happily stores ``Infinity`` in a REAL column and Python writes it
    as a bare ``Infinity`` literal -- which is *not* valid JSON. The browser
    then rejected the whole response and the tab went blank instead of
    showing a single field as ``--``.
    """

    import math

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {key: _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(item) for item in value]
    return value


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


def _normalize_range(raw: Any, default: str) -> str:
    """Map a requested range to a supported key.

    ``/history`` mapped "90d" to the closest window while ``/export`` fell
    back to the 7-day default: the same link produced different windows on
    the chart and in the CSV (v2.20.20).
    """

    key = str(raw or "").strip().lower()
    if key in RANGE_SECONDS:
        return key
    match = re.match(r"^(\d+)\s*([hd])$", key)
    if match:
        seconds = int(match.group(1)) * (86400 if match.group(2) == "d" else 3600)
        return min(
            RANGE_SECONDS,
            key=lambda item: abs(RANGE_SECONDS[item] - seconds),
        )
    return default


def _mask_umo(target: Any) -> str:
    """Thin wrapper over the shared :func:."""

    return mask_umo(target)


def _monitor_status(state: dict[str, Any], config_error: Any) -> str:
    """Derive one monitor card's status.

    Kept as a module-level function so the precedence (and the
    "probe never concluded" branch) is directly testable. A PROBE_ERROR
    round leaves ``fails`` untouched by design; without the
    ``probe_error_at`` branch the card stayed green even though the latest
    probe never produced a conclusion (v2.20.22).
    """

    checked = state.get("last_checked")
    if config_error:
        return "config_error"
    if not checked:
        return "unknown"
    if bool(state.get("alerting")):
        return "fail"
    if state.get("fails"):
        # Failed the latest probe but has not reached the alert threshold
        # (or is suppressed by a dependency).
        return "degraded"
    if safe_float(state.get("probe_error_at") or 0) > safe_float(
        state.get("last_ok") or 0
    ):
        return "unknown"
    return "ok"


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
        # One entry per cache key currently being built: a plain boolean was
        # cleared by whichever concurrent build finished first, so two
        # different days being opened at once each built again (v2.20.20).
        # The value is an Event the late callers wait on, so a COLD cache
        # (no stale copy to serve) builds once instead of once per caller
        # (v2.20.24).
        self._reports_build_inflight: dict[tuple, asyncio.Event] = {}
        # The payload of a finished build, keyed by cache key. The shared
        # cache slot holds only ONE payload, so a waiter that reads it may
        # find another day's payload and rebuild (v2.20.24 audit).
        self._reports_build_results: dict[tuple, Any] = {}
        # Per-kind verify cooldown: the dashboard buttons used to be able to
        # send a real message every click, so a stuck retry loop in the page
        # could spam the chat (v2.20.5).
        self._verify_last: dict[str, float] = {}
        # Strong references for the fire-and-forget recovery tasks: asyncio
        # may garbage-collect an unreferenced task mid-flight.
        self._recovery_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # degraded store gate
    # ------------------------------------------------------------------

    async def _db_unavailable_payload(self) -> dict[str, Any] | None:
        """Shared marker + empty payload for a broken report store.

        v2.18.9 taught the send path to retry via ``ensure_ready``; the
        dashboard routes never checked, so a broken store looked like "no
        data was ever recorded" -- the most misleading empty state there is.
        The page gets an explicit ``db_unavailable`` marker instead, and one
        recovery attempt is kicked off in the background so the next refresh
        can already succeed.

        The probe runs in a worker thread: it opens a connection, issues
        PRAGMAs and takes ``BEGIN IMMEDIATE`` (a WRITE lock) under the
        store's lock. Called from the event loop it could block every other
        task (including QQ message handling) for up to the 5-second SQLite
        timeout, on every history/events/reports refresh (v2.20.21).
        """

        store = self.plugin.report_store
        # ``available`` is a cached flag: when the database file is deleted
        # at runtime it stays True until something touches a real table. The
        # dashboard then showed "no data was ever recorded" instead of the
        # explicit database error. ``probe()`` is a cheap SELECT MAX(ts) and
        # also flips the flag when the file is gone (v2.20.17).
        try:
            probe_ok = await asyncio.to_thread(store.probe)
        except Exception:
            # A raising probe used to escape the gate and turn every
            # history/events/reports/export request into a 500 -- the exact
            # opposite of what a degraded-mode gate is for. Treat it like an
            # unavailable database and let the recovery below run.
            logger.exception("报表库探测失败，按数据库不可用处理")
            probe_ok = False
        if store.available and probe_ok:
            return None
        if any(not task.done() for task in self._recovery_tasks):
            # A recovery attempt is already running; spawning another per
            # refresh used to run concurrent init/rename passes on the same
            # SQLite file (v2.20.0).
            return {
                "ok": True,
                "db_unavailable": True,
                "reason_code": "report_db_unavailable",
            }
        try:
            task = asyncio.get_running_loop().create_task(
                asyncio.to_thread(store.ensure_ready)
            )

            def _consume(task: asyncio.Task) -> None:
                # Discard still runs first (the set is the caller's bookkeeping),
                # but the exception must be retrieved here: an unretrieved task
                # exception logs a framework-level "Task exception was never
                # retrieved" line for every refresh while the database is down,
                # and the actual failure reason never reached the log (v2.20.24
                # audit).
                self._recovery_tasks.discard(task)
                if not task.cancelled() and task.exception() is not None:
                    logger.error(
                        "报表库后台恢复失败: %s",
                        task.exception(),
                        exc_info=task.exception(),
                    )

            task.add_done_callback(_consume)
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

        rules = read_rules(self.plugin.config, "display.page_redact")
        # Constructing a Redactor calls socket.gethostname(); the export path
        # built one per row (twice per row for events) and spent ~100ms of
        # CPU on 20k rows for no reason. Reuse it while the rule set is
        # unchanged; a config reload creates a new PageApi anyway.
        key = tuple(sorted(rules))
        cached = getattr(self, "_redactor_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        redactor = Redactor(rules)
        self._redactor_cache = (key, redactor)
        return redactor

    def _redact(self) -> set[str]:
        """The rule names that apply to the Web dashboard."""

        return self._redactor().rules

    def _mask_audit(self, issues: list[Any]) -> list[dict[str, Any]]:
        """Mask host-shaped tokens inside the config-audit messages.

        ``audit.py`` already strips URL credentials and chat ids, but with
        the hostname rule on the dashboard must not print the host either:
        the monitor names in ``monitors`` are hidden, while the audit banner
        next to them showed the same address verbatim (v2.20.17).
        """

        redactor = self._redactor()

        def mask_value(value: Any) -> Any:
            # The English dashboard renders ``params`` instead of parsing the
            # Chinese message, so the same credentials must be stripped there
            # (v2.20.30): a monitor name or target in params would otherwise
            # bypass every mask the message goes through.
            if isinstance(value, dict):
                return {key: mask_value(inner) for key, inner in value.items()}
            if isinstance(value, (list, tuple)):
                return [mask_value(inner) for inner in value]
            if isinstance(value, str):
                return redactor.mask_address_tokens(redact_url(value))
            return value

        masked: list[dict[str, Any]] = []
        for issue in issues:
            item = issue.as_dict()
            # Strip URL credentials and query tokens FIRST: the token replacer
            # stops at the first non-URL character, so "?token=…" after an
            # already-masked host used to survive in the banner text.
            item["message"] = redactor.mask_address_tokens(
                redact_url(str(item.get("message") or ""))
            )
            item["params"] = mask_value(item.get("params") or {})
            masked.append(item)
        return masked

    def _host(self) -> str:
        # ``socket.gethostname()`` itself can raise in restricted containers
        # (and did take the whole /overview route down with it). The Redactor
        # already guards the same call; mirror that here (v2.20.22).
        try:
            hostname = socket.gethostname()
        except Exception:
            # ``OSError`` is the common case; a restricted interpreter can
            # raise other exceptions from the same call. The overview route
            # must degrade to "--", not 500 (v2.20.22).
            hostname = ""
        return self._redactor().dashboard_host(hostname)

    @staticmethod
    def _network_view(redacted: bool, net: dict[str, Any]) -> dict[str, Any]:
        """Return the overview network payload, honouring ``network`` redaction."""

        if redacted:
            return {"redacted": True}
        return {
            "up_rate": round(safe_float(net.get("up_rate")), 1),
            "down_rate": round(safe_float(net.get("down_rate")), 1),
            "month_up_text": format_bytes(net.get("month_up")),
            "month_down_text": format_bytes(net.get("month_down")),
            "month": str(net.get("month") or ""),
        }

    def _platform(self) -> str:
        instance_id = str(
            cfg_get(self.plugin.config, "alerts.platform_id", "default") or "default"
        )
        # Show the adapter type (qq_official / aiocqhttp) instead of the
        # instance id: after switching bots the dashboard otherwise still
        # said "default" while every push went through the official adapter.
        adapter = find_adapter(self.plugin.context, None, instance_id)
        name = adapter_platform_name(adapter) if adapter is not None else ""
        return name or instance_id

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
            # Deduplicate like the card does (v2.20.0).
            configured = list(
                dict.fromkeys(str(item).strip() for item in paths if str(item).strip())
            )
        else:
            configured = []
        if not configured:
            configured = [os.path.abspath(os.sep)]
        try:
            # Keep the dashboard on the same used/total scale as the card.
            usages = disk_usage_bounded(configured)
        except Exception:
            usages = []
        redactor = self._redactor()
        mounts: list[dict[str, Any]] = []
        for usage in usages:
            # A configured mount that cannot be read stays visible with "--":
            # dropping it made a pulled disk disappear from the dashboard while
            # the status card showed the failed row (v2.20.2).
            mounts.append(
                {
                    "path": redactor.mask_address_tokens(str(usage.name or "")),
                    "percent": (
                        round(float(usage.percent), 1)
                        if usage.percent is not None
                        else None
                    ),
                    "used": int(usage.used) if usage.used is not None else None,
                    "total": int(usage.total) if usage.total is not None else None,
                    "used_text": format_bytes(usage.used),
                    "total_text": format_bytes(usage.total),
                }
            )
        if not mounts:
            return {
                "path": redactor.mask_address_tokens(configured[0]),
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
        usable = [item for item in mounts if item["percent"] is not None]
        worst = max(usable, key=lambda item: item["percent"]) if usable else mounts[0]
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
        import math

        redact = "process" in self._redact()
        items = []
        for index, row in enumerate(snapshot.get("top") or [], 1):
            # A hand-edited/external snapshot row used to raise on the bare
            # int()/float() and take the whole /overview route down; every
            # other state/db value on this page goes through safe_*.
            pid = safe_int(row.get("pid"), 0)
            cpu_raw = safe_float(row.get("cpu_raw"), 0.0)
            mem_mb = safe_float(row.get("mem_mb"), 0.0)
            # Same 1e9 ceiling as the report's process aggregation: a huge
            # finite value renders a 300-digit peak, no real per-process
            # sample is anywhere near it.
            if not math.isfinite(cpu_raw) or abs(cpu_raw) > 1e9:
                cpu_raw = 0.0
            if not math.isfinite(mem_mb) or abs(mem_mb) > 1e9:
                mem_mb = 0.0
            items.append(
                {
                    "rank": index,
                    "name": "已隐藏" if redact else str(row.get("name") or ""),
                    "pid": None if redact else pid,
                    "cpu": round(cpu_raw, 1),
                    "mem_mb": round(mem_mb, 1),
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
        if not isinstance(net, dict):
            # A hand-edited state.json used to turn the whole overview into a
            # 500 ("'str' object has no attribute 'get'"); the collected
            # layers already treat a broken shape as empty (v2.20.5).
            net = {}
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
            # A hand-edited state.json can hold a scalar here; ``or {}`` keeps
            # a non-empty string and the first ``.get`` raised, taking the
            # whole overview route down (v2.20.8).
            state = item.get("state")
            if not isinstance(state, dict):
                state = {}
            checked = state.get("last_checked")
            config_error = item.get("config_error")
            status = _monitor_status(state, config_error)
            if status == "config_error":
                config_error_count += 1
            elif status == "unknown":
                unknown_count += 1
            elif status == "fail":
                fail_count += 1
            elif status == "degraded":
                degraded_count += 1
            else:
                ok_count += 1
            monitor_items.append(
                {
                    "name": self._redactor().mask_name(
                        redact_url(str(item.get("name") or ""))
                    ),
                    "kind": item.get("kind"),
                    # The English page needs the stable type key; without it
                    # the Chinese KIND_LABELS value ("进程") leaked through
                    # (v2.20.17). Monitor.status() already provides it.
                    "kind_key": item.get("kind_key"),
                    "status": status,
                    "last_checked": checked,
                    "last_ok": state.get("last_ok"),
                    "last_error": self._mask_host_text(
                        str(state.get("last_error") or "")[:160]
                    ),
                    # ``config_error`` quotes the raw operator input (for a
                    # bad TCP port it is ``repr(item["port"])``), so a pasted
                    # URL with credentials leaked on a dashboard that had
                    # the hostname rule on. Mask it exactly like last_error
                    # (v2.20.19).
                    "config_error": self._mask_host_text(str(config_error or "")[:160]),
                    # Structured twin for the English page (v2.20.30); the
                    # values are field names / a raw port, not host-shaped.
                    "config_error_code": item.get("config_error_code"),
                    "config_error_params": item.get("config_error_params") or {},
                }
            )

        # The overview used to run one more SQLite read (daily_report.status)
        # and ship a field the page never rendered; both are gone (v2.20.20).
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
                "aborted_long": 0,
                "success_rate": None,
                "success_text": "--",
                "error_streak": 0,
                "truncated": False,
            }
        # ``psutil.boot_time()`` raises in a sandboxed container that hides
        # /proc. The collectors module already guards its copy; the overview
        # is the FIRST screen of the dashboard, and an exception here also
        # withheld ``timezone`` (sent only by this route), so history/events
        # fell back to the browser timezone and the whole panel looked dead
        # while the QQ card still worked (v2.20.21).
        try:
            boot_time = psutil.boot_time()
        except Exception:
            boot_time = None
        uptime_seconds = (
            int(max(0, time.time() - boot_time)) if boot_time is not None else None
        )
        return jsonify(
            {
                "ok": True,
                "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": _timezone_name(plugin.config),
                "host": self._host(),
                "platform": self._platform(),
                "astrbot_version": self._astrbot_version(),
                "plugin_version": PLUGIN_VERSION,
                "uptime": (
                    format_duration(uptime_seconds)
                    if uptime_seconds is not None
                    else "--"
                ),
                "uptime_seconds": uptime_seconds,
                "cpu": {
                    "percent": round(cached_cpu_percent(), 1),
                    "count": psutil.cpu_count(logical=True) or 1,
                    "load1": load1,
                },
                "memory": {
                    "percent": round(float(mem.percent), 1),
                    # "available / total" matches the percentage above it
                    # (psutil's RAM percent is (total - available) / total).
                    "used_text": format_bytes(
                        mem.available if mem.available is not None else mem.used
                    ),
                    "total_text": format_bytes(mem.total),
                    "available": mem.available is not None,
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
                "provider_quality": provider_quality,
                "audit": self._mask_audit(plugin.audit_issues()),
                # An empty list means either "no problems" or "the audit did
                # not run". The dashboard used to render both as a hidden
                # banner, i.e. the misleading all-green result the diagnostic
                # command already avoids (v2.20.21).
                "audit_status": (
                    "failed" if getattr(plugin, "_audit_failed", False) else "ok"
                ),
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

        raw_kind = await _request_value("kind", None)
        kind = str(raw_kind or "").strip().lower()
        if kind not in ("targets", "monitors", "webhook"):
            # A bridge that loses the JSON body used to fall back to
            # "monitors" silently: the operator clicked "verify targets", the
            # page rendered undefined rows and no message was sent. Reject
            # the request instead of guessing (v2.20.30).
            return jsonify(
                {
                    "ok": False,
                    "error": "missing or unknown kind",
                    "error_code": "verify_kind_missing",
                }
            )
        now = time.time()
        if now - self._verify_last.get(kind, 0.0) < 30:
            return jsonify(
                {
                    "ok": False,
                    "error": "验证过于频繁，请 30 秒后再试（避免重复发送消息）",
                    # The dashboard renders ``error`` verbatim, so the English
                    # page showed the Chinese sentence. The code lets the
                    # frontend pick the translation (v2.20.14).
                    "error_code": "verify_too_frequent",
                }
            )
        # The cooldown is charged only once the request will really run: an
        # early "未配置任何推送目标" answer used to start the 30-second timer,
        # so the operator fixed the target and was told "验证过于频繁"
        # (v2.20.8).
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
                    if not raw and legacy_key != "alerts.targets":
                        # The real senders fall back to alerts.targets when
                        # the module-specific list is empty:
                        # ServiceMonitor for ``service`` and DailyReporter
                        # for ``daily``/``weekly``. Verify only read the
                        # module list, so a correct alerts.targets channel
                        # was never exercised (v2.20.19).
                        raw = cfg_get(plugin.config, "alerts.targets", [])
                        if isinstance(raw, str):
                            raw = [raw]
                    picked = normalize_targets(raw or [], platform_id)
                for one in picked:
                    if one not in merged:
                        merged.append(one)
            if not merged:
                return jsonify(
                    {
                        "ok": False,
                        "error": "未配置任何推送目标",
                        "error_code": "verify_no_targets",
                    }
                )
            # The message lands in QQ, so it follows display.language like
            # every other chat text; it used to be hard-coded Chinese
            # (v2.20.20).
            text = tr(plugin.config, "verify_targets_message")
            try:
                result = await plugin.send_text(merged, text, source="verify_targets")
            except Exception as exc:
                # A raising send used to escape the route as a 500 AND keep
                # the cooldown it had already charged, locking the operator
                # out for 30 seconds right after fixing the environment.
                # Charge the cooldown only after an actual send attempt.
                logger.exception("验证推送目标发送失败")
                return jsonify(
                    {"ok": False, "kind": kind, "error": exc.__class__.__name__}
                )
            self._verify_last[kind] = now
            per_target = []
            for target in merged:
                receipt = (result.receipts or {}).get(target) or {}
                if target in (result.ok_targets or []):
                    state = "ok"
                elif target in (result.uncertain_targets or []):
                    state = "uncertain"
                else:
                    state = "failed"
                # The receipt error can quote the full target (for example
                # "无法解析目标: default:GroupMessage:123"), which defeated the
                # masked "target" field right above it. It can also quote
                # only the bare session id ("会话 ID 不是数字: 461961169") or
                # embed it in an exception message, so the full-UMO
                # replacement alone was not enough.
                error_text = str(receipt.get("error") or "")
                if target:
                    error_text = error_text.replace(target, _mask_umo(target))
                error_text = mask_chat_ids(error_text)
                per_target.append(
                    {
                        # Keep enough to tell targets apart without echoing
                        # full UMOs (the class contract promises no UMO).
                        "target": _mask_umo(target),
                        "state": state,
                        "retcode": receipt.get("retcode"),
                        "message_id": receipt.get("message_id"),
                        "error": self._mask_host_text(error_text)[:160],
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
                        # Structured variant so the English UI can translate
                        # instead of showing the Chinese sentence (v2.20.0).
                        "note_code": (
                            {"code": "monitor_items_incomplete", "count": broken}
                            if broken
                            else {"code": "monitor_items_empty"}
                        ),
                    }
                )
            self._verify_last[kind] = now

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
                    "name": self._redactor().mask_name(
                        redact_url(str(item.get("name") or ""))
                    ),
                    "ok": bool(ok),
                    "detail": self._mask_host_text(str(detail or "")),
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                }

            # A verify click with dozens of unreachable targets can take
            # minutes (6 concurrent × up to 60s each); the browser call then
            # timed out first and the operator saw "request timed out" while
            # the backend kept probing. Bound the whole batch and report the
            # items that did not finish (v2.20.21).
            tasks = [asyncio.create_task(probe_one(item)) for item in items]
            _done, pending = await asyncio.wait(tasks, timeout=80)
            for task in pending:
                task.cancel()
            # ``asyncio.wait`` returns sets, so iterating ``done`` made the
            # result order depend on Task object hashes: every click showed
            # the items in a different order, unlike the overview list and the
            # configured order (v2.20.22). Walk the original task list.
            results = [
                task.result()
                for task in tasks
                if task.done() and not task.cancelled() and task.exception() is None
            ]
            timed_out = len(pending)
            # The button is a manual probe, but with the monitor disabled
            # (the default) or without a push target the background loop
            # never runs at all. Showing a wall of green checks without that
            # context told the operator everything was being watched while
            # the overview said "服务监测已关闭" (v2.20.21).
            monitor_on = cfg_bool(plugin.config, "monitors.enabled", False)
            targets: list[str] = []
            get_targets = getattr(monitor, "_targets_for", None)
            if callable(get_targets):
                try:
                    targets = list(get_targets("service") or [])
                except Exception:
                    targets = []
            payload: dict[str, Any] = {
                "ok": True,
                "kind": kind,
                "results": list(results),
            }
            if not monitor_on:
                payload["note"] = (
                    "服务监测已关闭：本次只是手动探测，后台不会自动检查或告警"
                )
                payload["note_code"] = {"code": "monitor_disabled"}
            elif not targets:
                payload["note"] = "没有可用的服务监测推送目标：后台不会自动检查或告警"
                payload["note_code"] = {"code": "monitor_no_targets"}
            if timed_out and not payload.get("note"):
                payload["note"] = f"另有 {timed_out} 个监测项在 80 秒内未完成，已取消"
                payload["note_code"] = {
                    "code": "monitor_verify_timeout",
                    "count": timed_out,
                }
            return jsonify(payload)
        if kind == "webhook":
            cfg = cfg_get(plugin.config, "alerts.komari_webhook", {}) or {}
            if not cfg_bool(cfg, "enabled", False):
                return jsonify(
                    {
                        "ok": True,
                        "kind": kind,
                        "listening": False,
                        "message": "Komari Webhook 未启用",
                        "message_code": "webhook_disabled",
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
                        "message_code": "webhook_not_running",
                    }
                )
            self._verify_last[kind] = now
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
                # httpx's read timeout is counted PER CHUNK, not for the
                # whole response: a server that sends the headers and then
                # drips the body one byte at a time keeps the read alive
                # forever, and ``client.post`` reads the entire body. The
                # client timeout therefore does not bound the request --
                # only an outer wait_for does (the same reasoning as the
                # avatar download in collectors.py).
                async def _post() -> int:
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.post(
                            f"http://{host}:{port}{path}",
                            json={
                                "message": "🔔 Komari Webhook 链路验证（看板模拟事件）"
                            },
                            headers=headers,
                        )
                        return resp.status_code

                status = await asyncio.wait_for(_post(), timeout=15)
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
        return jsonify(
            {
                "ok": False,
                "error": f"unknown kind: {kind} (supported: monitors, targets, webhook)",
            }
        )

    # ------------------------------------------------------------------
    # GET /export
    # ------------------------------------------------------------------

    @staticmethod
    def _process_cpu_pct_total(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add the whole-machine CPU percentage next to the per-core one.

        ``process_samples.cpu`` is a REAL column in a dynamically typed
        SQLite database, so a text value is possible (older writers, a
        hand-edited db). A bare ``float()`` aborted the whole /export request
        and the page lost every CSV after the first one.
        """

        cores = psutil.cpu_count(logical=True) or 1
        out: list[dict[str, Any]] = []
        for row in rows:
            cpu = row.get("cpu")
            item = dict(row)
            item["cpu_pct_total"] = (
                round(safe_float(cpu) / cores, 1) if cpu is not None else None
            )
            out.append(item)
        return out

    async def export(self):
        """Return raw history rows; the page turns them into a CSV file.

        The rows are aggregated numbers only (timestamps, percentages,
        counters, process names honouring the dashboard redaction) - never
        message text or user IDs.
        """

        kind = str(_query_value("kind", "resources")).lower()
        # Same normalisation as /history: the export used to fall back to the
        # 7-day default, so "90d" meant one window on the chart and another
        # in the CSV (v2.20.20).
        range_key = _normalize_range(_query_value("range", "7d"), "7d")
        gate = await self._db_unavailable_payload()
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
            rows = [
                row for row in fetched[-limit:] if safe_int(row.get("ts") or 0) <= end
            ]
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
            rows = self._process_cpu_pct_total(rows)
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
                    "name": self._redactor().mask_name(
                        redact_url(str(row.get("name") or ""))
                    ),
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
                    "name": self._redactor().mask_name(
                        redact_url(str(row.get("name") or ""))
                    ),
                    "detail": self._mask_host_text(str(row.get("detail") or "")),
                    "status": row.get("status"),
                    "resolved_ts": row.get("resolved_ts"),
                    # The first failed probe; NULL on rows written by
                    # <=2.20.29 and on recovery rows (the reader falls back
                    # to ts).
                    "started_ts": row.get("started_ts"),
                }
                for row in fetched[-limit:]
            ]
        else:
            return jsonify(
                {
                    "ok": False,
                    "error": f"unknown kind: {kind} (supported: resources, processes, websites, events)",
                }
            )
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
        # Echo the range that was actually used, never an unvalidated input.
        # An unsupported but meaningful value ("90d") maps to the closest
        # supported window so a hand-written link shows roughly what it asked
        # for (v2.20.5); /export shares the same helper (v2.20.20).
        key = _normalize_range(_query_value("range", "24h"), "24h")
        gate = await self._db_unavailable_payload()
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
        rows = [row for row in fetched[-20000:] if safe_int(row.get("ts") or 0) <= end]
        sampled = self._downsample(rows)
        points = [
            {
                # safe_int: a text ts in the db used to raise here and turn
                # the whole history route into a 500.
                "ts": safe_int(row.get("ts")),
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
                    "ts": safe_int(row.get("ts")),
                    "up": row.get("net_up"),
                    "down": row.get("net_down"),
                }
                for row in sampled
                if row.get("net_up") is not None or row.get("net_down") is not None
            ]
            if from_db:
                net_points = from_db
                # The interval is sent separately (``network_sample_seconds``)
                # so the UI can render the configured value instead of a
                # hardcoded "5 minutes".
                network_note = "网络折线来自日报采样，与资源趋势同一时间窗口。"
                network_note_kind = "db"
            else:
                net = self.plugin.state.get("net", {}) or {}
                if not isinstance(net, dict):
                    net = {}
                history = net.get("history") or []
                if not isinstance(history, list):
                    history = []
                net_points = [
                    {
                        "ts": safe_int(item[0]),
                        "up": safe_float(item[1]),
                        "down": safe_float(item[2]),
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
                [safe_int(row.get("ts") or 0) for row in rows], 2 * sample_interval
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
                # The dashboard sentence used to hardcode "5 minutes" while
                # the slider allows 60–1800 seconds: the note disagreed with
                # the data it described (v2.20.21).
                "network_sample_seconds": sample_interval,
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
        gate = await self._db_unavailable_payload()
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
        #
        # One row more than the counter limit: that extra row is what tells
        # "exactly 2000 events" apart from "more than 2000" (v2.20.14).
        events = await asyncio.to_thread(
            self.plugin.report_store.list_events_range,
            int(start_dt.timestamp()),
            int(time.time()) + 60,
            limit=2001,
        )
        counts_truncated = len(events) > 2000
        if counts_truncated:
            # ``list_events_range`` returns oldest-first (DESC + reverse), so
            # slicing from the front dropped the NEWEST event. Every other
            # route uses ``fetched[-limit:]`` for exactly this reason
            # (v2.20.17).
            events = events[-2000:]
        items: list[dict[str, Any]] = [
            {
                "id": event.get("id"),
                "ts": event.get("ts"),
                "day": event.get("day"),
                "kind": event.get("kind"),
                # Event names can be a URL the operator pasted as the monitor
                # name; strip query/credentials before the page or a forwarded
                # CSV can see them, and hide the host entirely when the
                # hostname rule is on (the daily report already does).
                "name": self._redactor().mask_name(
                    redact_url(str(event.get("name") or ""))
                ),
                # Mask first, then cut: truncating first can leave a partial
                # token that no longer matches the redaction pattern.
                "detail": self._mask_host_text(str(event.get("detail") or ""))[:200],
                "status": event.get("status"),
                "resolved_ts": event.get("resolved_ts"),
                # The outage start (first failed probe). The dashboard shows
                # this instead of the push moment so it matches the alert
                # text; rows from <=2.20.29 carry null.
                "started_ts": event.get("started_ts"),
            }
            for event in events
        ]
        items.sort(
            key=lambda item: (
                safe_int(item.get("ts") or 0),
                safe_int(item.get("id") or 0),
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
                #
                # ">= 2000" claimed a truncation even when the range held
                # exactly 2000 events and nothing was cut off (v2.20.14): the
                # query asks for one row more than the limit, so a full page
                # means there really was more.
                "counts_truncated": counts_truncated,
                "days": days,
                "items": items[:page_size],
                "counts": counts,
                "truncated": len(items) > page_size,
            }
        )

    # ------------------------------------------------------------------
    # GET /reports
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_report_day(raw: str, today: date | None = None) -> bool:
        """Whether ``?day=`` names a report worth building.

        The upper bound was already checked; the lower bound matters because
        ``date.fromisoformat("0001-01-01")`` is valid, and subtracting the
        previous window from it overflowed inside the report builder: the
        whole page became "构建失败" (v2.20.17).
        """

        try:
            parsed = date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False
        if today is None:
            today = date.today()
        return today - timedelta(days=365 * 30) <= parsed <= today

    async def reports(self):
        plugin = self.plugin
        now_ts = time.time()
        fresh = str(_query_value("fresh", "0")) == "1"
        # ``day`` lets the dashboard look at an older report (for example one
        # that was never delivered because the bot was offline). Invalid or
        # future dates fall back to the default "latest available" report.
        raw_day = str(_query_value("day", "") or "").strip()
        day_arg: str | None = None
        if raw_day:
            try:
                parsed_day = date.fromisoformat(raw_day)
            except ValueError:
                # Silently building "yesterday" for an unparseable date made
                # the page look like the jump worked: the header showed the
                # default day and the user thought they had jumped to it
                # (v2.20.21).
                return jsonify(
                    {
                        "ok": False,
                        "error_code": "report_day_invalid",
                        "message": f"无法识别的日期: {raw_day[:32]}",
                    }
                )
            today = now_in_timezone(plugin.config).date()
            if not self._valid_report_day(parsed_day.isoformat(), today):
                return jsonify(
                    {
                        "ok": False,
                        "error_code": "report_day_out_of_range",
                        "message": "日期超出可查询范围（今天往前 30 年以内）",
                    }
                )
            day_arg = parsed_day.isoformat()
        # The cache key carries the date, the redaction rules and the report
        # content selection: a single slot used to serve yesterday's report
        # right after midnight, an unredacted payload for a minute after
        # page_redact was turned on, or old sections after the operator
        # changed daily_report.sections.
        sections_cfg = cfg_get(plugin.config, "daily_report.sections", None)
        # The page used to hardcode 7 days while the pushed report honours
        # daily_report.history_days (7-30); the chart and the pushed image
        # then disagreed after the operator changed the setting.
        history_days = max(
            7, min(30, cfg_int(plugin.config, "daily_report.history_days", 7))
        )
        cache_key = (
            now_in_timezone(plugin.config).date().isoformat(),
            tuple(sorted(self._redact())),
            repr(sections_cfg),
            history_days,
            day_arg or "",
        )
        # Claim the build BEFORE the first await. The database probe below
        # awaits, so a claim made after it lets every concurrent caller pass
        # this point first and build the whole report on its own (v2.20.24
        # audit).
        event = self._reports_build_inflight.get(cache_key)
        # A forced refresh (``fresh=1``) always builds its own payload: the
        # operator clicked "刷新" and must not receive the build that was
        # already running (v2.20.24 audit).
        is_builder = event is None or fresh
        if is_builder:
            event = asyncio.Event()
            self._reports_build_inflight[cache_key] = event
        try:
            return await self._reports_dispatch(
                now_ts,
                cache_key,
                sections_cfg,
                fresh,
                day_arg,
                event,
                is_builder,
            )
        finally:
            if is_builder:
                if self._reports_build_inflight.get(cache_key) is event:
                    self._reports_build_inflight.pop(cache_key, None)
                # The waiters wake up here and read the payload. The slot
                # must survive until then, so it is NOT cleaned here; a
                # bounded size keeps a build nobody waited for from piling
                # up.
                self._prune_build_results(keep=cache_key)
                event.set()

    def _prune_build_results(self, keep: tuple | None = None) -> None:
        """Drop the oldest handoff payloads, never the one just written.

        A plain "keep the last N keys" eviction removed the key that the
        builder had just stored whenever the dictionary was already full:
        re-assigning an existing key keeps its original position, so the
        key the builder wrote could be the first one evicted -- and the
        waiting caller then rebuilt the whole report (v2.20.24 audit).
        """

        results = self._reports_build_results
        if len(results) <= 8:
            return
        for stale_key in list(results):
            if len(results) <= 8:
                break
            if stale_key == keep:
                continue
            results.pop(stale_key, None)

    async def _reports_dispatch(
        self,
        now_ts: float,
        cache_key: tuple,
        sections_cfg: Any,
        fresh: bool,
        day_arg: str | None,
        event: asyncio.Event,
        is_builder: bool,
    ):
        """Serve one /reports request, sharing one build per cache key."""
        if not is_builder:
            # Another caller is already building this payload. Wait for it
            # and reuse its result. The warm-cache shortcut below is NOT
            # used here: the builder runs the database probe first, and a
            # cached payload must not mask a broken database (v2.20.24
            # audit).
            try:
                await asyncio.wait_for(event.wait(), timeout=90)
            except Exception:
                # TimeoutError (or a cancellation of the wait itself). The
                # build is taking longer than 90 s -- rebuilding here would
                # run a SECOND full aggregation next to the running one
                # (v2.20.24 audit). Report the timeout instead; the page
                # offers a retry.
                logger.warning("看板日报构建超过 90 秒未完成，本次不再重复构建")
                return jsonify(
                    {
                        "ok": False,
                        "error_code": "report_build_timeout",
                        "error": "日报构建超时，请稍后重试",
                    }
                )
            result = self._reports_build_results.get(cache_key)
            if result is not None:
                # The handoff payload CAN be older than the 60 s cache
                # window, so it is marked as stale: otherwise the page
                # would claim the data was just updated (v2.20.24 audit).
                # But "older" is not automatic: two tabs opening the same
                # day at the same second handed out a payload built one
                # second ago and the page then told the user it was
                # showing last-time data. Judge by the payload's own
                # generated_at instead (v2.20.32).
                handed = copy.deepcopy(result)
                if isinstance(handed, dict):
                    built_txt = str(handed.get("generated_at") or "")
                    built_ts = 0.0
                    if built_txt:
                        try:
                            # generated_at is the DISPLAY timezone's wall
                            # clock (report_view formats an aware datetime);
                            # interpret it with the same zone, not the host
                            # zone, or a mismatched deployment shifted the
                            # freshness verdict by hours (v2.20.32).
                            built_ts = (
                                datetime.strptime(built_txt, "%Y-%m-%d %H:%M:%S")
                                .replace(
                                    tzinfo=now_in_timezone(self.plugin.config).tzinfo
                                )
                                .timestamp()
                            )
                        except (ValueError, TypeError):
                            built_ts = 0.0
                    fresh_enough = (
                        built_ts > 0
                        and time.time() - built_ts <= self._reports_cache_seconds
                    )
                    handed["stale"] = not fresh_enough
                return jsonify(handed)
            # The running build failed (no result): fall through and build
            # here, so the caller still gets an error payload.
        # The database gate comes BEFORE the cache: a warm cache must not
        # mask a broken database, or the page would show stale data as if
        # it were fresh and the background repair would never start
        # (v2.20.24 audit).
        gate = await self._db_unavailable_payload()
        if gate:
            # An explicit degraded marker instead of "empty sections": the
            # page keeps whatever it already rendered and shows a notice.
            degraded = {**gate, "day": None}
            # The waiting callers get the same marker, so a database outage
            # is probed once per cache key instead of once per caller.
            self._reports_build_results[cache_key] = degraded
            return jsonify(degraded)
        cached = self._reports_cache
        if (
            cached
            and not fresh
            and now_ts - cached[0] < self._reports_cache_seconds
            and cached[2] == cache_key
        ):
            # Hand out a copy: the cached payload is the very object every
            # later request would receive, so a caller mutating it in place
            # would corrupt all subsequent responses.
            payload = copy.deepcopy(cached[1])
            self._reports_build_results[cache_key] = payload
            return jsonify(payload)
        payload = await self._build_reports_payload(
            now_ts, cache_key, sections_cfg, fresh, day_arg
        )
        # Hand the payload to the waiting callers. The shared cache slot is
        # NOT a substitute: it only holds one key at a time.
        self._reports_build_results[cache_key] = payload
        return jsonify(payload)

    async def _build_reports_payload(
        self,
        now_ts: float,
        cache_key: tuple,
        sections_cfg: Any,
        fresh: bool,
        day_arg: str | None = None,
    ):
        view: dict[str, Any] = {}
        try:
            history_days = max(
                7, min(30, cfg_int(self.plugin.config, "daily_report.history_days", 7))
            )
            builder = DailyReportBuilder(
                self.plugin.report_store,
                self.plugin.config,
                redact=self._redactor().rules,
            )
            sections = sections_cfg
            view = await asyncio.to_thread(
                builder.build,
                day=day_arg,
                history_days=history_days,
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
            return {
                "ok": False,
                "error_code": "report_build_failed",
                "error": "日报数据构建失败，请稍后重试",
            }
        payload = {
            "ok": True,
            "day": view.get("day"),
            # The page uses this to bound the "next day" arrow; deriving it
            # from the browser (UTC) hid the arrow for 8 hours every night in
            # Asia/Shanghai (v2.20.8).
            "today": now_in_timezone(self.plugin.config).date().isoformat(),
            "sections": view.get("sections"),
            # Whether the CONFIG listed any sections. ``sections`` above is
            # filtered to valid names, so an all-invalid list looks like an
            # empty selection and the page told the operator to "pick some"
            # when the real problem was a typo (v2.20.17).
            "sections_configured": (
                len(sections_cfg)
                if isinstance(sections_cfg, (list, tuple))
                else (1 if sections_cfg else 0)
            ),
            "summary": view.get("summary"),
            "resources": view.get("resources"),
            "anomalies": view.get("anomalies"),
            "qq": view.get("qq"),
            "token": view.get("token"),
            "provider_quality": view.get("provider_quality"),
            "website": view.get("website"),
            "process": view.get("process"),
            "runs": await asyncio.to_thread(
                self.plugin.report_store.list_report_runs, limit=30
            ),
        }
        self._reports_cache = (now_ts, payload, cache_key)
        return payload
