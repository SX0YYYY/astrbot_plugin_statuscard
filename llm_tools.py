"""Read-only data source behind the plugin's LLM tools (v2.24.0).

Three tools expose a summary of this plugin's data to the chat LLM. The
service layer keeps every heavy or blocking access bounded: psutil snapshots
and SQLite reads run in ``asyncio.to_thread``, each (tool, parameters) pair
caches its result for a few seconds, output is truncated to a few kilobytes,
and every line goes through the same Redactor as the card, the reports and
the dashboard. It never calls ``collect_all``, never probes, never sends and
never renders an image.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict
from typing import Any

from .collectors import cfg_get
from .redactor import Redactor, read_rules
from .utils import redact_url

# LLM tool output budget: large payloads burn context tokens, and the
# framework keeps a full copy of the result in its INFO log.
MAX_OUTPUT_CHARS = 6 * 1024
TRUNCATION_NOTE = "\n……（结果过长，已截断）"

# Per-tool cache lifetimes (seconds): a status snapshot changes slowly, the
# event log even slower; a chat turn usually asks once, but an agent that
# retries must not hammer SQLite on every attempt.
_TTL = {"status": 10.0, "events": 10.0, "uptime": 30.0}


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool):
        return default
    return max(low, min(high, number))


class StatusCardToolService:
    """Builds the tool answers; owns caching, redaction and truncation."""

    def __init__(
        self,
        config: Any,
        state: Any,
        report_store: Any = None,
        monitor: Any = None,
    ):
        self.config = config
        self.state = state
        self.report_store = report_store
        self.monitor = monitor
        self._redactor = Redactor(read_rules(config, "display.redact"))
        self._cache: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # cache plumbing
    # ------------------------------------------------------------------

    async def _cached(self, key: str, ttl: float, builder: Any) -> str:
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
            # Builders are synchronous (psutil / SQLite) and must not block
            # the event loop.
            value = str(await asyncio.to_thread(builder))
            self._cache[key] = (time.monotonic(), value)
            # Keep the cache small: a long chat can mint many key variants.
            if len(self._cache) > 64:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:-32]
                for stale_key, _ts in oldest:
                    self._cache.pop(stale_key, None)
            return value

    def _finish(self, text: str) -> str:
        if len(text) > MAX_OUTPUT_CHARS:
            return text[:MAX_OUTPUT_CHARS].rstrip() + TRUNCATION_NOTE
        return text

    # ------------------------------------------------------------------
    # tool 1: current server status
    # ------------------------------------------------------------------

    async def get_server_status(
        self,
        include_processes: bool = True,
        include_websites: bool = True,
        top_n: int = 5,
    ) -> str:
        top_n = _clamp(top_n, 1, 20, 5)
        return await self._cached(
            f"status:{bool(include_processes)}:{bool(include_websites)}:{top_n}",
            _TTL["status"],
            lambda: self._finish(
                self._status_text(include_processes, include_websites, top_n)
            ),
        )

    def _status_text(
        self, include_processes: bool, include_websites: bool, top_n: int
    ) -> str:
        import psutil

        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        try:
            cpu = psutil.cpu_percent(interval=None)
        except Exception:
            cpu = -1.0
        try:
            boot = psutil.boot_time()
            uptime_seconds = max(0, int(time.time() - boot))
        except Exception:
            uptime_seconds = 0
        lines = [
            f"CPU: {cpu:.1f}%",
            f"内存: {vm.percent:.1f}%（{vm.used / 1024**3:.1f}G / {vm.total / 1024**3:.1f}G）",
            f"SWAP: {swap.percent:.1f}%",
        ]
        if uptime_seconds:
            hours, _rest = divmod(uptime_seconds, 3600)
            days, hours = divmod(hours, 24)
            lines.append(f"运行时长: {days} 天 {hours} 小时")
        if os.name == "posix":
            try:
                load1, load5, load15 = os.getloadavg()
                lines.append(f"负载: {load1:.2f} {load5:.2f} {load15:.2f}")
            except Exception:
                pass
        disk_lines = self._disk_lines()
        if disk_lines:
            lines.append("磁盘:")
            lines.extend(disk_lines)
        if include_processes:
            lines.extend(self._process_lines(top_n))
        if include_websites:
            lines.extend(self._monitor_lines(top_n))
        return "\n".join(lines)

    def _disk_lines(self) -> list[str]:
        import psutil

        paths = cfg_get(self.config, "display.disk_paths", []) or []
        out: list[str] = []
        for path in paths[:8]:
            try:
                usage = psutil.disk_usage(str(path))
            except OSError:
                # A vanished mount / permission error: skip the line, the
                # status card does the same.
                continue
            out.append(
                f"- {self._redactor.mask_name(str(path))}: {usage.percent:.1f}%"
                f"（{usage.used / 1024**3:.1f}G / {usage.total / 1024**3:.1f}G）"
            )
        return out

    def _process_lines(self, top_n: int) -> list[str]:
        import psutil

        samples: list[tuple[float, float, str, int]] = []
        for proc in psutil.process_iter(
            ["name", "pid", "memory_percent", "cpu_percent"]
        ):
            try:
                info = proc.info
                samples.append(
                    (
                        float(info.get("memory_percent") or 0.0),
                        float(info.get("cpu_percent") or 0.0),
                        str(info.get("name") or ""),
                        int(info.get("pid") or 0),
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
                # A vanished process: skip, same as the status-card collector.
                continue
        samples.sort(reverse=True)
        if not samples:
            return []
        lines = [f"进程 Top {min(top_n, len(samples))}（按内存）:"]
        for mem, cpu, name, pid in samples[:top_n]:
            lines.append(
                f"- {self._redactor.mask_name(name)}（pid {pid}）内存 {mem:.1f}% CPU {cpu:.1f}%"
            )
        return lines

    def _monitor_lines(self, top_n: int) -> list[str]:
        monitor = self.monitor
        if monitor is None:
            return []
        try:
            status = monitor.status()
        except Exception:
            return []
        items = status.get("items") or []
        if not items:
            return []
        rows: list[tuple[bool, str]] = []
        for item in items:
            if item.get("config_error"):
                continue
            state = item.get("state") or {}
            ok = bool(state.get("last_ok"))
            error = self._redactor.mask_addresses(
                str(state.get("last_error") or "")[:80]
            )
            label = "正常" if ok else ("告警中: " + error if error else "异常")
            rows.append(
                (
                    ok,
                    f"- [{item.get('kind')}] {self._redactor.mask_name(str(item.get('name') or ''))}: {label}",
                )
            )
        # Abnormal first: the LLM (and the operator) cares about faults.
        rows.sort(key=lambda pair: pair[0])
        shown = rows[:top_n]
        faults = sum(1 for ok, _t in rows if not ok)
        head = f"服务监测（{len(rows)} 项，{faults} 项异常）:"
        return [head, *[text for _ok, text in shown]]

    # ------------------------------------------------------------------
    # tool 2: recent events
    # ------------------------------------------------------------------

    async def query_events(
        self, days: int = 7, limit: int = 50, status: str = "", kind: str = ""
    ) -> str:
        days = _clamp(days, 1, 90, 7)
        limit = _clamp(limit, 1, 200, 50)
        status = str(status or "").strip().lower()
        kind = str(kind or "").strip().lower()
        key = f"events:{days}:{limit}:{status}:{kind}"
        return await self._cached(
            key,
            _TTL["events"],
            lambda: self._finish(self._events_text(days, limit, status, kind)),
        )

    def _events_text(self, days: int, limit: int, status: str, kind: str) -> str:
        if self.report_store is None:
            return "日报数据库不可用，无法查询事件。"
        end = int(time.time())
        start = end - days * 86400
        rows = self.report_store.list_events_range(start, end, limit=limit)
        picked: list[dict[str, Any]] = []
        for row in rows:
            if status and str(row.get("status") or "").lower() != status:
                continue
            if kind and str(row.get("kind") or "").lower() != kind:
                continue
            picked.append(row)
            if len(picked) >= limit:
                break
        if not picked:
            return f"最近 {days} 天没有符合条件的事件（status={status or '任意'}，kind={kind or '任意'}）。"
        lines = [
            f"最近 {days} 天事件 {len(picked)} 条"
            + (f"（status={status}）" if status else "")
            + (f"（kind={kind}）" if kind else "")
            + ":"
        ]
        for row in picked:
            when = time.strftime("%m-%d %H:%M", time.localtime(int(row.get("ts") or 0)))
            name = self._redactor.mask_name(str(row.get("name") or ""))
            detail = self._redactor.mask_addresses(str(row.get("detail") or "")[:100])
            target = self._redactor.mask_addresses(
                redact_url(str(row.get("target") or ""))[:60]
            )
            resolved = "" if row.get("resolved_ts") else "（未恢复）"
            lines.append(
                f"{when} [{row.get('kind')}] {name}"
                + (f" @{target}" if target and target != "-" else "")
                + f": {row.get('status')}{resolved} - {detail}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # tool 3: availability summary
    # ------------------------------------------------------------------

    async def get_uptime_summary(self, days: int = 7, scope: str = "service") -> str:
        days = _clamp(days, 1, 90, 7)
        scope = str(scope or "service").strip().lower()
        if scope not in ("service", "all"):
            scope = "service"
        return await self._cached(
            f"uptime:{days}:{scope}",
            _TTL["uptime"],
            lambda: self._finish(self._uptime_text(days, scope)),
        )

    def _uptime_text(self, days: int, scope: str) -> str:
        if self.report_store is None:
            return "日报数据库不可用，无法汇总可用性。"
        end = int(time.time())
        start = end - days * 86400
        rows = self.report_store.list_events_range(start, end, limit=20000)
        if scope == "service":
            rows = [r for r in rows if str(r.get("kind") or "") == "service"]
        alerts = sum(1 for r in rows if r.get("status") == "alert")
        recoveries = sum(1 for r in rows if r.get("status") == "recovery")
        unresolved = [
            r for r in rows if r.get("status") == "alert" and not r.get("resolved_ts")
        ]
        if not rows:
            return f"最近 {days} 天没有任何{'服务监测' if scope == 'service' else ''}事件记录，一切正常或监测尚未启用。"
        by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row.get("status") in ("alert", "failed"):
                by_name[str(row.get("name") or "?")].append(row)
        ranked = sorted(by_name.items(), key=lambda kv: len(kv[1]), reverse=True)
        lines = [
            f"最近 {days} 天可用性汇总（{scope}）:",
            f"- 故障 {alerts} 次，恢复 {recoveries} 次，仍未恢复 {len(unresolved)} 项",
        ]
        for name, events in ranked[:10]:
            total = sum(
                max(
                    0,
                    int(r.get("resolved_ts") or end)
                    - int(r.get("started_ts") or r.get("ts") or end),
                )
                for r in events
            )
            lines.append(
                f"- {self._redactor.mask_name(name)}: 故障 {len(events)} 次，累计约 {total // 60} 分钟"
                + (
                    "（含未恢复）"
                    if any(not r.get("resolved_ts") for r in events)
                    else ""
                )
            )
        if unresolved:
            lines.append("仍未恢复:")
            for row in unresolved[:10]:
                lines.append(
                    f"- {self._redactor.mask_name(str(row.get('name') or ''))}"
                    f"（自 {time.strftime('%m-%d %H:%M', time.localtime(int(row.get('started_ts') or row.get('ts') or end)))}）"
                )
        return "\n".join(lines)
