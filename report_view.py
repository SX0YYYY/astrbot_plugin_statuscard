from __future__ import annotations

import math
import socket
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import psutil

from .collectors import (
    cfg_bool,
    cfg_get,
    cfg_int,
    format_duration,
    get_timezone,
    now_in_timezone,
)
from .redactor import Redactor, read_rules
from .report_store import ReportStore

RESOURCE_KEYS = ("cpu", "ram", "swap", "disk", "load1")
RESOURCE_LABELS = {
    "cpu": "CPU",
    "ram": "RAM",
    "swap": "SWAP",
    "disk": "磁盘",
    "load1": "每核负载",
}
RRULE_COUNT = len(RESOURCE_KEYS)
VALID_SECTIONS = (
    "resource",
    "anomaly",
    "qq",
    "token",
    "website",
    "process",
)
DEFAULT_SECTIONS = ("resource", "anomaly", "qq", "token")


def _fmt_num(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"{round(float(value)):,}"
    except (TypeError, ValueError, OverflowError):
        return "--"


def _fmt_compact(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    units = ("", "K", "M", "G", "T")
    index = 0
    while abs(number) >= 1000 and index < len(units) - 1:
        number /= 1000
        index += 1
    if index == 0:
        return f"{number:.0f}"
    return f"{number:.1f}{units[index]}"


def _signed_pct(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return "--"


def _delta_pct(current: Any, previous: Any) -> float | None:
    if current is None or previous in (None, 0):
        return None
    try:
        return (float(current) - float(previous)) / float(previous) * 100.0
    except (TypeError, ValueError):
        return None


def _nice_ceil(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = value / (10**exp)
    for step in (1, 2, 5, 10):
        if base <= step:
            return step * (10**exp)
    return 10 ** (exp + 1)


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _line_chart(
    labels: list[str],
    series: list[dict[str, Any]],
    y_max: float | None = None,
    width: int = 600,
    height: int = 170,
    y_formatter=_fmt_num,
) -> dict[str, Any]:
    left = 44
    right = 12
    top = 12
    bottom = 22
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [
        value
        for item in series
        for value in item.get("values", [])
        if value is not None
    ]
    if not values:
        return {
            "width": width,
            "height": height,
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "plot_w": plot_w,
            "plot_h": plot_h,
            "series": [],
            "y_labels": [],
            "x_labels": [],
            "peaks": [],
            "empty": True,
        }

    maximum = float(y_max) if y_max is not None else _nice_ceil(max(values) * 1.15)
    n = len(labels)
    denom = max(1, n - 1)

    def xy(index: int, value: float) -> tuple[float, float]:
        x = left + plot_w * (index / denom)
        y = top + plot_h * (1 - min(1.0, max(0.0, value / maximum)))
        return round(x, 1), round(y, 1)

    rendered: list[dict[str, Any]] = []
    peaks: list[dict[str, Any]] = []
    for item in series:
        item_values = list(item.get("values", []))
        segments: list[str] = []
        current: list[str] = []
        for index, value in enumerate(item_values):
            if value is None:
                if current:
                    segments.append(" ".join(current))
                    current = []
                continue
            x, y = xy(index, float(value))
            current.append(f"{x},{y}")
        if current:
            segments.append(" ".join(current))
        rendered.append(
            {
                "name": str(item.get("name") or ""),
                "color": str(item.get("color") or "#4f8cff"),
                "segments": segments,
            }
        )
        numeric = [
            (index, float(value))
            for index, value in enumerate(item_values)
            if value is not None
        ]
        if numeric:
            peak_index, peak_value = max(numeric, key=lambda pair: pair[1])
            if peak_value > 0:
                x, y = xy(peak_index, peak_value)
                peaks.append(
                    {
                        "x": x,
                        "y": y,
                        "text": y_formatter(peak_value),
                        "color": str(item.get("color") or "#4f8cff"),
                    }
                )

    y_labels = []
    for i in range(5):
        value = maximum * i / 4
        y = top + plot_h * (1 - i / 4)
        y_labels.append({"y": round(y, 1), "text": y_formatter(value)})

    # Ceiling keeps tick marks on the "nice" slots: for a 24-point hour axis
    # this yields 0/6/12/18/23 instead of 0/6/12/17/23.
    indices = sorted({math.ceil((n - 1) * i / 4) for i in range(5)}) if n else []
    x_labels = []
    for index in indices:
        x = left + plot_w * (index / denom)
        x_labels.append({"x": round(x, 1), "text": labels[index]})

    return {
        "width": width,
        "height": height,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "plot_w": plot_w,
        "plot_h": plot_h,
        "series": rendered,
        "y_labels": y_labels,
        "x_labels": x_labels,
        "peaks": peaks,
        "empty": False,
    }


class DailyReportBuilder:
    """Build the daily-report view from the plugin store and AstrBot history.

    The builder only reads data. Rendering and sending are handled by
    ``t2i_renderer.build_report_html`` and the future scheduled task.
    """

    def __init__(
        self,
        store: ReportStore,
        config: Any,
        db_path: Path | str | None = None,
        redact: set[str] | list[str] | None = None,
    ):
        self.store = store
        self.config = config
        self.db_path = Path(db_path) if db_path is not None else None
        self.tz = get_timezone(config)
        self.tz_name = str(
            cfg_get(config, "display.timezone", "Asia/Shanghai") or "Asia/Shanghai"
        )
        # The Web dashboard passes its own rules so that the dashboard can be
        # more or less strict than the QQ status card. Accept a Redactor
        # instance defensively: wrapping it into a second Redactor would
        # normalize its rules down to an empty set and silently disable
        # redaction for this surface.
        if isinstance(redact, Redactor):
            redact = redact.rules
        if redact is not None:
            self.redactor = Redactor(redact)
        else:
            self.redactor = Redactor(read_rules(config, "display.redact"))
        self.redact = self.redactor.rules

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------

    def build(
        self,
        day: str | date | datetime | None = None,
        history_days: int = 7,
        sections: list[str] | None = None,
        meta: dict[str, Any] | None = None,
        span_days: int = 1,
    ) -> dict[str, Any]:
        target = self._resolve_day(day)
        span = max(1, min(30, int(span_days or 1)))
        start_day = target - timedelta(days=span - 1)
        previous = target - timedelta(days=span)
        history_days = max(7, min(30, int(history_days or 7)))
        requested = list(DEFAULT_SECTIONS) if sections is None else sections
        if isinstance(requested, str):
            requested = [requested]
        selected = [item for item in requested if item in VALID_SECTIONS]
        selected = [item for item in VALID_SECTIONS if item in selected]
        meta = meta or {}
        generated_at = now_in_timezone(self.config).strftime("%Y-%m-%d %H:%M:%S")
        host = str(meta.get("host") or socket.gethostname())
        if self.redactor.hostname:
            host = "已隐藏"

        if span == 1:
            title = "系统日报"
            window = f"{target.isoformat()} 00:00–24:00"
            day_label = "昨日"
            window_label = "昨日"
            token_label = f"昨日 · 趋势最近 {history_days} 天"
        else:
            title = "系统周报"
            window = f"{start_day.isoformat()} 00:00 – {target.isoformat()} 24:00"
            day_label = "最后一天"
            window_label = f"近 {span} 天"
            token_label = f"近 {span} 天合计 · 趋势最近 {history_days} 天"

        view: dict[str, Any] = {
            "title": title,
            "day": target.isoformat(),
            "start_day": start_day.isoformat(),
            "span_days": span,
            "day_label": day_label,
            "window_label": window_label,
            "token_label": token_label,
            "previous_day": previous.isoformat(),
            "window": window,
            "timezone": self.tz_name,
            "generated_at": generated_at,
            "host": host,
            "astrbot_version": str(meta.get("astrbot_version") or ""),
            "sections": selected,
            "history_days": history_days,
            "footer_note": (
                "数据来源：report.db、AstrBot platform_stats/provider_stats、"
                "message_stats；不包含消息内容、用户 ID 或 UMO。"
            ),
        }
        if "resource" in selected:
            view["resources"] = self._resource_section(target, previous, history_days)
        if "anomaly" in selected:
            view["anomalies"] = self._anomaly_section(target, previous, span)
        if "qq" in selected:
            view["qq"] = self._qq_section(target, history_days)
        if "token" in selected:
            view["token"] = self._token_section(target, history_days, span)
        if "website" in selected:
            view["website"] = self._website_section(
                target, previous, history_days, span
            )
        if "process" in selected:
            view["process"] = self._process_section(
                target, previous, history_days, span
            )
        view["summary"] = self._summary(view)
        return view

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _resolve_day(self, value: Any) -> date:
        if value is None:
            return (now_in_timezone(self.config) - timedelta(days=1)).date()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    def _summary(self, view: dict[str, Any]) -> dict[str, Any]:
        sections = view.get("sections") or []
        anomalies = view.get("anomalies") or {}
        token = view.get("token") or {}
        qq = view.get("qq") or {}
        yesterday = qq.get("yesterday") or {}
        # A figure for a section the operator did not select would read as a
        # real "0" or "no data"; label it as not enabled instead.
        return {
            "anomaly_count": (
                anomalies.get("count", 0) if "anomaly" in sections else "未开启"
            ),
            "anomaly_enabled": "anomaly" in sections,
            "unresolved_count": (
                anomalies.get("unresolved", 0) if "anomaly" in sections else "未开启"
            ),
            "token_total_text": (
                token.get("total_text", "暂无数据") if "token" in sections else "未开启"
            ),
            "token_enabled": "token" in sections,
            "sent_text": (
                yesterday.get("sent_text", "--") if "qq" in sections else "未开启"
            ),
            "recv_text": (
                yesterday.get("recv_text", "--") if "qq" in sections else "未开启"
            ),
            "qq_enabled": "qq" in sections,
        }

    def _astrbot_db_path(self) -> Path | None:
        if self.db_path is not None:
            return self.db_path
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "data_v4.db"
        except Exception:
            return None

    def _readonly_db(self) -> sqlite3.Connection | None:
        path = self._astrbot_db_path()
        if path is None or not path.is_file():
            return None
        try:
            return sqlite3.connect(
                f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # resources
    # ------------------------------------------------------------------

    def _resource_section(
        self, day: date, previous: date, history_days: int
    ) -> dict[str, Any]:
        start_day = min(day - timedelta(days=history_days - 1), previous)
        start_ts = int(
            datetime.combine(start_day, time.min, tzinfo=self.tz).timestamp()
        )
        end_ts = int(
            datetime.combine(
                day + timedelta(days=1), time.min, tzinfo=self.tz
            ).timestamp()
        )
        rows = [
            row
            for row in self.store.list_resources(start_ts, limit=100000)
            if int(row.get("ts") or 0) < end_ts
        ]
        by_day: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            local = datetime.fromtimestamp(int(row["ts"]), self.tz)
            by_day.setdefault(local.strftime("%Y-%m-%d"), []).append(row)

        first_day = min(by_day) if by_day else None
        yesterday_rows = by_day.get(day.isoformat(), [])
        prev_rows = by_day.get(previous.isoformat(), [])
        available = len(yesterday_rows) >= 2
        if not rows:
            first_ts = self.store.first_resource_ts()
            if first_ts is not None:
                first_day = datetime.fromtimestamp(first_ts, self.tz).strftime(
                    "%Y-%m-%d"
                )
                empty_reason = (
                    f"资源采样从 {first_day} 开始，暂无 {day.isoformat()} 数据"
                )
            else:
                empty_reason = "暂无资源采样数据"
        elif not available:
            empty_reason = f"资源采样从 {first_day or '--'} 开始，暂无完整昨日数据"
        else:
            empty_reason = None

        yesterday = {
            key: self._metric_stats(
                yesterday_rows, key, unit="%" if key != "load1" else ""
            )
            for key in RESOURCE_KEYS
        }
        prev = {
            key: self._metric_stats(prev_rows, key, unit="%" if key != "load1" else "")
            for key in RESOURCE_KEYS
        }
        for key in RESOURCE_KEYS:
            yesterday[key]["delta_text"] = _signed_pct(
                _delta_pct(yesterday[key]["avg"], prev[key]["avg"])
            )
        yesterday["samples"] = len(yesterday_rows)

        hourly_rows: dict[int, list[dict[str, Any]]] = {hour: [] for hour in range(24)}
        for row in yesterday_rows:
            local = datetime.fromtimestamp(int(row["ts"]), self.tz)
            hourly_rows[local.hour].append(row)

        def hourly_values(key: str) -> list[float | None]:
            out: list[float | None] = []
            for hour in range(24):
                values = [
                    float(item[key])
                    for item in hourly_rows[hour]
                    if item.get(key) is not None
                ]
                out.append(sum(values) / len(values) if values else None)
            return out

        hourly_chart = _line_chart(
            [f"{hour:02d}" for hour in range(24)],
            [
                {"name": "CPU", "color": "#2ecc71", "values": hourly_values("cpu")},
                {"name": "RAM", "color": "#4f8cff", "values": hourly_values("ram")},
                {"name": "SWAP", "color": "#f59e0b", "values": hourly_values("swap")},
            ],
            y_max=100,
            y_formatter=lambda value: f"{value:.0f}%",
        )

        days = [start_day + timedelta(days=i) for i in range(history_days)]
        labels = [item.strftime("%m-%d") for item in days]

        def daily_values(key: str) -> list[float | None]:
            out: list[float | None] = []
            for item in days:
                stats = self._metric_stats(
                    by_day.get(item.isoformat(), []),
                    key,
                    unit="%" if key != "load1" else "",
                )
                out.append(stats["avg"])
            return out

        daily_chart = _line_chart(
            labels,
            [
                {"name": "CPU", "color": "#2ecc71", "values": daily_values("cpu")},
                {"name": "RAM", "color": "#4f8cff", "values": daily_values("ram")},
            ],
            y_max=100,
            y_formatter=lambda value: f"{value:.0f}%",
        )
        days_with_data = sum(1 for item in days if by_day.get(item.isoformat()))
        if first_day and first_day > start_day.isoformat():
            history_note = (
                f"资源历史从 {first_day} 开始，{history_days} 天趋势仍在积累。"
            )
        elif days_with_data < history_days:
            history_note = (
                f"{history_days} 天趋势中已有 {days_with_data} 天数据，仍在积累。"
            )
        else:
            history_note = None
        interval = max(
            60,
            min(
                1800,
                cfg_int(self.config, "daily_report.resource_sample_seconds", 300),
            ),
        )
        expected = max(1, 86400 // interval)
        coverage = len(yesterday_rows) / expected
        gaps = detect_resource_gaps(
            [int(row.get("ts") or 0) for row in yesterday_rows], interval
        )
        gaps_text = None
        if gaps:
            total_minutes = sum((gap["end"] - gap["start"]) for gap in gaps) // 60
            gaps_text = (
                f"{len(gaps)} 段共约 {total_minutes} 分钟"
                "（采样缺失，期间插件或 Bot 停机、无法监测）"
            )
        coverage_note = (
            f"昨日资源采样 {len(yesterday_rows)} 个点（约覆盖 {coverage * 100:.0f}%），"
            "均值和峰值为已有数据口径。"
            + (f"停机盲区：{gaps_text}。" if gaps_text else "")
            if available and (coverage < 0.9 or gaps)
            else None
        )

        return {
            "available": available,
            "empty_reason": empty_reason,
            "first_day": first_day,
            "history_note": history_note,
            "coverage_note": coverage_note,
            "gaps": gaps,
            "gaps_text": gaps_text,
            "yesterday": yesterday,
            "previous": prev,
            "charts": {"hourly": hourly_chart, "daily": daily_chart},
        }

    @staticmethod
    def _metric_stats(
        rows: list[dict[str, Any]], key: str, unit: str = "%"
    ) -> dict[str, Any]:
        values = []
        for row in rows:
            raw = row.get(key)
            if raw is None:
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(number):
                continue
            values.append(number)
        if not values:
            return {
                "avg": None,
                "peak": None,
                "avg_text": "--",
                "peak_text": "--",
                "delta_text": "--",
                "count": 0,
            }
        average = sum(values) / len(values)
        peak = max(values)
        digits = 2 if key == "load1" else 1
        return {
            "avg": average,
            "peak": peak,
            "avg_text": f"{average:.{digits}f}{unit}",
            "peak_text": f"{peak:.{digits}f}{unit}",
            "delta_text": "--",
            "count": len(values),
        }

    # ------------------------------------------------------------------
    # anomalies
    # ------------------------------------------------------------------

    def _anomaly_section(
        self, day: date, previous: date, span: int = 1
    ) -> dict[str, Any]:
        window_days = {
            (day - timedelta(days=offset)).isoformat() for offset in range(span)
        }
        events = self.store.list_events(previous.isoformat(), limit=1000)
        for offset in range(span):
            boundary = day - timedelta(days=offset)
            events += self.store.list_events(boundary.isoformat(), limit=1000)
        events.sort(
            key=lambda item: (int(item.get("ts") or 0), int(item.get("id") or 0))
        )
        window_start_dt = datetime.combine(
            day - timedelta(days=span - 1), time.min, tzinfo=self.tz
        )
        window_start_ts = int(window_start_dt.timestamp())
        # ``resolved_ts`` is filled in when a condition clears, so the
        # duration survives a plugin restart. Events recorded before that
        # field was used fall back to pairing them in memory.
        open_alerts: dict[str, int] = {}
        # An interruption that began before the window still has an alert row
        # outside ``events``; load the preceding month so a recovery inside
        # the window can be paired with it instead of showing "--".
        for alert in self.store.list_events_range(
            window_start_ts - 30 * 86400, window_start_ts - 1, limit=5000
        ):
            status = str(alert.get("status") or "")
            name = str(alert.get("name") or "")
            if not name:
                continue
            if status == "alert":
                open_alerts[name] = int(alert.get("ts") or 0)
            elif status == "recovery":
                # A recovery before the window closes the alert for pairing
                # purposes; otherwise a later recovery could be matched with
                # an interruption that had already ended.
                open_alerts.pop(name, None)
        items: list[dict[str, Any]] = []
        count = 0
        for event in events:
            status = str(event.get("status") or "")
            name = str(event.get("name") or "")
            resolved_ts = event.get("resolved_ts")
            if status == "alert":
                open_alerts[name] = int(event.get("ts") or 0)
            if str(event.get("day") or "") not in window_days:
                continue
            if status not in {"alert", "recovery", "failed"}:
                continue
            duration_text = "--"
            if status == "recovery":
                start = open_alerts.pop(name, None)
                if start:
                    duration_text = format_duration(int(event.get("ts") or 0) - start)
                    if start < window_start_ts:
                        duration_text += "（窗口前开始）"
            elif status == "alert":
                if resolved_ts:
                    duration_text = format_duration(
                        int(resolved_ts) - int(event.get("ts") or 0)
                    )
                else:
                    duration_text = "进行中"
            if status in {"alert", "failed"}:
                count += 1
            # A weekly window spans several days, so a bare "HH:MM" cannot
            # locate the event inside it.
            time_format = "%H:%M" if span == 1 else "%m-%d %H:%M"
            items.append(
                {
                    "time": datetime.fromtimestamp(
                        int(event.get("ts") or 0), self.tz
                    ).strftime(time_format),
                    "kind": self._kind_text(str(event.get("kind") or "")),
                    "kind_slug": str(event.get("kind") or ""),
                    "name": name or "--",
                    "status": self._status_text(status),
                    "status_key": status,
                    "detail": str(event.get("detail") or "")[:80],
                    "duration": duration_text,
                }
            )
        # An interruption that began before the window and never got a
        # resolved timestamp (e.g. its recovery notice was lost to a reload)
        # would otherwise read as "no anomalies". Surface it explicitly, but
        # skip names that already show a recovery inside the window.
        recovered_names = {
            str(event.get("name") or "")
            for event in events
            if str(event.get("status") or "") == "recovery"
        }
        stale: dict[str, dict[str, Any]] = {}
        for alert in self.store.list_unresolved_alerts(
            window_start_ts, window_start_ts - 30 * 86400, limit=50
        ):
            name = str(alert.get("name") or "")
            if not name or name in recovered_names:
                continue
            # Oldest start per name wins ("ongoing since ...").
            if name not in stale:
                stale[name] = alert
        unresolved_items: list[dict[str, Any]] = []
        for alert in stale.values():
            started = datetime.fromtimestamp(int(alert.get("ts") or 0), self.tz)
            unresolved_items.append(
                {
                    "time": started.strftime("%m-%d %H:%M"),
                    "kind": self._kind_text(str(alert.get("kind") or "")),
                    "kind_slug": str(alert.get("kind") or ""),
                    "name": str(alert.get("name") or "--"),
                    "status": "未恢复",
                    "status_key": "alert",
                    "detail": "开始于此处，未记录恢复时刻（可能已恢复）",
                    "duration": "进行中",
                }
            )
        if unresolved_items:
            items = unresolved_items + items
        return {
            "available": bool(items),
            "empty_reason": (
                "昨日无异常事件" if span == 1 else f"近 {span} 天无异常事件"
            )
            if not items
            else None,
            "count": count,
            "unresolved": len(unresolved_items),
            "total": len(items),
            "items": items[:20],
            "normal": {
                "monitors": self._monitor_count(),
                "resource_rules": RRULE_COUNT,
            },
        }

    def _monitor_count(self) -> int:
        items = cfg_get(self.config, "monitors.items", []) or []
        if not isinstance(items, list):
            return 0
        return len(
            [
                item
                for item in items
                if isinstance(item, dict) and cfg_bool(item, "enabled", True)
            ]
        )

    @staticmethod
    def _kind_text(kind: str) -> str:
        return {
            "resource": "资源",
            "offline": "掉线",
            "service": "服务",
        }.get(kind, kind or "事件")

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "alert": "异常",
            "recovery": "恢复",
            "failed": "推送失败",
        }.get(status, status or "事件")

    # ------------------------------------------------------------------
    # QQ
    # ------------------------------------------------------------------

    def _qq_section(self, day: date, history_days: int) -> dict[str, Any]:
        if self.redactor.qq_stats:
            return {
                "available": False,
                "empty_reason": "已按脱敏配置隐藏 QQ 统计",
                "days": [],
                "labels": [],
                "yesterday": {},
                "previous": {},
                "reset_days": [],
                "sent_available": False,
                "snapshot_note": None,
                "recv_note": None,
                "charts": {},
                "note": "",
            }
        snapshots = {
            str(item.get("day")): item
            for item in self.store.list_snapshots(history_days + 2)
        }
        days = [day - timedelta(days=i) for i in range(history_days - 1, -1, -1)]
        labels = [item.strftime("%m-%d") for item in days]

        def snapshot_value(item: date, key: str) -> Any:
            snapshot = snapshots.get(item.isoformat())
            if not snapshot:
                return None
            value = snapshot.get(key)
            return value

        friends = [snapshot_value(item, "friends") for item in days]
        groups = [snapshot_value(item, "groups") for item in days]
        members = [snapshot_value(item, "members") for item in days]
        recv_values: list[int | None] = []
        reset_days: list[str] = []
        for item in days:
            # snapshot(D) is captured just after D 00:00, so the value that
            # covers day D is snap(D+1) - snap(D). Using snap(D)-snap(D-1)
            # shifted every receive number one day into the past.
            current = snapshots.get(item.isoformat())
            following = snapshots.get((item + timedelta(days=1)).isoformat())
            if (
                current
                and following
                and current.get("recv_total") is not None
                and following.get("recv_total") is not None
            ):
                delta = int(following["recv_total"]) - int(current["recv_total"])
                if delta < 0:
                    delta = 0
                    reset_days.append(item.isoformat())
                recv_values.append(delta)
            else:
                recv_values.append(None)

        sent_map, sent_min_day, sent_available = self._platform_sent(days[0], days[-1])
        sent_values: list[int | None] = []
        for item in days:
            day_text = item.isoformat()
            if not sent_available or not sent_min_day or day_text < sent_min_day:
                sent_values.append(None)
            else:
                sent_values.append(int(sent_map.get(day_text, 0)))

        yesterday = snapshots.get(day.isoformat(), {})
        previous = snapshots.get((day - timedelta(days=1)).isoformat(), {})
        snapshot_days = sum(1 for item in days if snapshots.get(item.isoformat()))
        if snapshot_days == 0:
            snapshot_note = "QQ 快照尚未开始积累。"
        elif snapshot_days < 2:
            snapshot_note = f"QQ 快照已有 {snapshot_days} 天数据，趋势仍在积累。"
        else:
            snapshot_note = None
        yesterday_recv = recv_values[-1] if recv_values else None
        previous_recv = recv_values[-2] if len(recv_values) >= 2 else None
        yesterday_sent = sent_values[-1] if sent_values else None
        previous_sent = sent_values[-2] if len(sent_values) >= 2 else None

        # The recv delta assumes both snapshots were captured around 00:05.
        # If OneBot was down at capture time the day's snapshot can be hours
        # late, in which case the delta silently covers extra hours.
        next_day = snapshots.get((day + timedelta(days=1)).isoformat(), {})
        recv_skewed = False
        try:
            cur_ts = float(yesterday.get("captured_at") or 0)
            next_ts = float(next_day.get("captured_at") or 0)
            gap_hours = abs(next_ts - cur_ts) / 3600.0 if cur_ts and next_ts else 24.0
            recv_skewed = not (23.0 <= gap_hours <= 25.0)
        except (TypeError, ValueError):
            recv_skewed = False

        available = any(
            value is not None
            for value in friends + groups + members + recv_values + sent_values
        )
        if available:
            empty_reason = None
        else:
            empty_reason = "暂无 QQ 快照或发送数据；从日报数据底座启用后开始积累"

        friends_groups = _line_chart(
            labels,
            [
                {"name": "好友", "color": "#4f8cff", "values": friends},
                {"name": "群聊", "color": "#8b5cf6", "values": groups},
            ],
            y_formatter=_fmt_num,
            height=150,
        )
        member_chart = _line_chart(
            labels,
            [{"name": "群员", "color": "#f59e0b", "values": members}],
            y_formatter=_fmt_num,
            height=140,
        )
        traffic_chart = _line_chart(
            labels,
            [
                {"name": "发送", "color": "#10b981", "values": sent_values},
                {"name": "接收", "color": "#ef4444", "values": recv_values},
            ],
            y_formatter=_fmt_num,
            height=150,
        )

        return {
            "available": available,
            "empty_reason": empty_reason,
            "days": [item.isoformat() for item in days],
            "labels": labels,
            "yesterday": {
                "friends": yesterday.get("friends"),
                "groups": yesterday.get("groups"),
                "members": yesterday.get("members"),
                "sent": yesterday_sent,
                "recv": yesterday_recv,
                "friends_text": _fmt_num(yesterday.get("friends")),
                "groups_text": _fmt_num(yesterday.get("groups")),
                "members_text": _fmt_num(yesterday.get("members")),
                "sent_text": _fmt_num(yesterday_sent),
                "recv_text": _fmt_num(yesterday_recv),
                "sent_delta_text": _signed_pct(
                    _delta_pct(yesterday_sent, previous_sent)
                ),
                "recv_delta_text": _signed_pct(
                    _delta_pct(yesterday_recv, previous_recv)
                ),
            },
            "previous": {
                "friends": previous.get("friends"),
                "groups": previous.get("groups"),
                "members": previous.get("members"),
                "sent": previous_sent,
                "recv": previous_recv,
            },
            "reset_days": reset_days,
            "sent_available": sent_available,
            "snapshot_note": snapshot_note,
            "recv_note": (
                "接收趋势需要至少两份每日快照，当前仍在积累。"
                if yesterday_recv is None
                else (
                    "昨日快照的记录时刻有偏移，接收差值仅供参考。"
                    if recv_skewed
                    else None
                )
            ),
            "charts": {
                "friends_groups": friends_groups,
                "members": member_chart,
                "traffic": traffic_chart,
            },
            "note": (
                "发=platform_stats 发送动作次数；收=message_stats 跟踪群累计差值；"
                "群员=各群 member_count 之和，跨群会重复计数。"
            ),
        }

    def _platform_sent(
        self, start_day: date, end_day: date
    ) -> tuple[dict[str, int], str | None, bool]:
        connection = self._readonly_db()
        if connection is None:
            return {}, None, False
        try:
            rows = connection.execute(
                """
                SELECT substr(timestamp, 1, 10) AS day, SUM(count)
                FROM platform_stats
                WHERE platform_id = 'aiocqhttp'
                  AND timestamp >= ? AND timestamp < ?
                GROUP BY day
                """,
                (
                    f"{start_day.isoformat()} 00:00:00",
                    f"{(end_day + timedelta(days=1)).isoformat()} 00:00:00",
                ),
            ).fetchall()
            min_row = connection.execute(
                """
                SELECT MIN(substr(timestamp, 1, 10))
                FROM platform_stats WHERE platform_id = 'aiocqhttp'
                """
            ).fetchone()
        except Exception:
            return {}, None, False
        finally:
            connection.close()
        mapping = {str(day): int(count or 0) for day, count in rows}
        min_day = str(min_row[0]) if min_row and min_row[0] else None
        return mapping, min_day, True

    # ------------------------------------------------------------------
    # website latency
    # ------------------------------------------------------------------

    def _website_section(
        self,
        day: date,
        previous: date,
        history_days: int,
        span: int = 1,
    ) -> dict[str, Any]:
        days = [
            day - timedelta(days=history_days - 1 - index)
            for index in range(history_days)
        ]
        start_ts = int(datetime.combine(days[0], time.min, tzinfo=self.tz).timestamp())
        end_ts = int(
            datetime.combine(
                day + timedelta(days=1), time.min, tzinfo=self.tz
            ).timestamp()
        )
        rows = self.store.list_website_samples(start_ts, end_ts)
        names = sorted({str(row.get("name") or "") for row in rows if row.get("name")})
        by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            local = datetime.fromtimestamp(int(row["ts"]), self.tz)
            key = (local.strftime("%Y-%m-%d"), str(row.get("name") or ""))
            by_key.setdefault(key, []).append(row)
        window_keys = [(day - timedelta(days=i)).isoformat() for i in range(span)]

        def latency_values(items: list[dict[str, Any]]) -> list[float]:
            return [
                float(item["latency_ms"])
                for item in items
                if item.get("ok") and item.get("latency_ms") is not None
            ]

        targets: list[dict[str, Any]] = []
        for name in names:
            window_rows = [
                row for key in window_keys for row in by_key.get((key, name), [])
            ]
            if not window_rows:
                continue
            ok_count = sum(1 for row in window_rows if row.get("ok"))
            latencies = latency_values(window_rows)
            average = sum(latencies) / len(latencies) if latencies else None
            peak = max(latencies) if latencies else None
            targets.append(
                {
                    "name": name,
                    "ok_count": ok_count,
                    "fail_count": len(window_rows) - ok_count,
                    "total": len(window_rows),
                    "availability_text": (
                        f"{ok_count / len(window_rows) * 100:.0f}%"
                        if window_rows
                        else "--"
                    ),
                    "avg_text": (f"{average:.0f} ms" if average is not None else "--"),
                    "peak_text": (f"{peak:.0f} ms" if peak is not None else "--"),
                }
            )

        colors = ["#4f8cff", "#2ecc71", "#f59e0b", "#8b5cf6", "#ef4444"]
        series: list[dict[str, Any]] = []
        for index, name in enumerate(names[:5]):
            values: list[float | None] = []
            for item in days:
                latencies = latency_values(by_key.get((item.isoformat(), name), []))
                values.append(sum(latencies) / len(latencies) if latencies else None)
            series.append(
                {
                    "name": name,
                    "color": colors[index % len(colors)],
                    "values": values,
                }
            )
        chart = _line_chart(
            [item.strftime("%m-%d") for item in days],
            series,
            y_formatter=lambda value: f"{value:.0f}ms",
            height=160,
        )

        first_ts = self.store.first_sample_ts("website_samples")
        first_day = (
            datetime.fromtimestamp(first_ts, self.tz).strftime("%Y-%m-%d")
            if first_ts
            else None
        )
        available = bool(targets)
        if available:
            empty_reason = None
        elif first_day:
            empty_reason = (
                f"网站延迟从 {first_day} 开始积累，暂无 {day.isoformat()} 数据"
            )
        else:
            empty_reason = "暂无网站延迟采样数据"
        history_note = None
        if first_day and first_day > days[0].isoformat():
            history_note = (
                f"网站延迟历史从 {first_day} 开始，{history_days} 天趋势仍在积累。"
            )
        return {
            "available": available,
            "empty_reason": empty_reason,
            "first_day": first_day,
            "targets": targets,
            "chart": chart,
            "history_note": history_note,
            "note": "延迟为 HTTP 请求耗时，可用率按“无网络错误”统计；目标复用状态卡的网站测试设置。",
        }

    # ------------------------------------------------------------------
    # top processes
    # ------------------------------------------------------------------

    def _process_section(
        self,
        day: date,
        previous: date,
        history_days: int,
        span: int = 1,
    ) -> dict[str, Any]:
        window_start = day - timedelta(days=span - 1)
        start_ts = int(
            datetime.combine(window_start, time.min, tzinfo=self.tz).timestamp()
        )
        end_ts = int(
            datetime.combine(
                day + timedelta(days=1), time.min, tzinfo=self.tz
            ).timestamp()
        )
        top_n = max(3, min(10, cfg_int(self.config, "daily_report.process_top_n", 5)))
        cpu_rows = self.store.list_process_samples(start_ts, end_ts, "cpu")
        mem_rows = self.store.list_process_samples(start_ts, end_ts, "mem")

        def aggregate(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
            values: dict[str, dict[str, Any]] = {}
            for row in rows:
                name = str(row.get("name") or "").strip()
                if not name:
                    continue
                item = values.setdefault(
                    name,
                    {
                        "name": name,
                        "cpu": 0.0,
                        "mem_mb": 0.0,
                        "samples": 0,
                    },
                )
                item["cpu"] = max(float(item["cpu"]), float(row.get("cpu") or 0.0))
                item["mem_mb"] = max(
                    float(item["mem_mb"]),
                    float(row.get("mem_mb") or 0.0),
                )
                item["samples"] += 1
            return sorted(values.values(), key=lambda item: item[key], reverse=True)[
                :top_n
            ]

        redact = self.redactor.process
        cores = psutil.cpu_count(logical=True) or 1

        def render(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for index, item in enumerate(items, 1):
                out.append(
                    {
                        "rank": index,
                        "name": "已隐藏" if redact else item["name"],
                        "cpu_text": f"{item['cpu'] / cores:.1f}%",
                        "mem_text": f"{item['mem_mb']:.0f} MB",
                        "samples": item["samples"],
                    }
                )
            return out

        cpu_top = render(aggregate(cpu_rows, "cpu"))
        mem_top = render(aggregate(mem_rows, "mem_mb"))
        first_ts = self.store.first_sample_ts("process_samples")
        first_day = (
            datetime.fromtimestamp(first_ts, self.tz).strftime("%Y-%m-%d")
            if first_ts
            else None
        )
        available = bool(cpu_top or mem_top)
        if available:
            empty_reason = None
        elif first_day:
            empty_reason = (
                f"进程采样从 {first_day} 开始积累，暂无 {day.isoformat()} 数据"
            )
        else:
            empty_reason = "暂无进程采样数据"
        note = (
            "按进程名聚合昨日采样峰值；同一程序的多进程会合并显示。"
            if span == 1
            else f"按进程名聚合近 {span} 天采样峰值；同一程序的多进程会合并显示。"
        )
        return {
            "available": available,
            "empty_reason": empty_reason,
            "first_day": first_day,
            "top_n": top_n,
            "cpu": cpu_top,
            "mem": mem_top,
            "note": note,
        }

    # ------------------------------------------------------------------
    # token
    # ------------------------------------------------------------------

    def _token_section(
        self, day: date, history_days: int, span: int = 1
    ) -> dict[str, Any]:
        chart_days = [day - timedelta(days=i) for i in range(history_days - 1, -1, -1)]
        window_days = [day - timedelta(days=i) for i in range(span - 1, -1, -1)]
        window_set = {item.isoformat() for item in window_days}
        labels = [item.strftime("%m-%d") for item in chart_days]
        rows, min_created, available, truncated = self._provider_rows(
            chart_days[0], chart_days[-1]
        )
        if not available or not rows:
            return {
                "available": False,
                "empty_reason": "暂无数据（所选时间范围内没有 provider 请求）",
                "total": 0,
                "total_text": "暂无数据",
                "calls": 0,
                "calls_text": "--",
                "success_text": "--",
                "top_models": [],
                "trend": _line_chart(labels, [], y_formatter=_fmt_compact),
                "note": "Token 口径：token_input_other + token_input_cached + token_output。",
            }

        min_day = None
        parsed_min = _parse_utc(min_created)
        if parsed_min is not None:
            min_day = parsed_min.astimezone(self.tz).strftime("%Y-%m-%d")

        by_day = {
            item.isoformat(): {"tokens": 0, "calls": 0, "completed": 0}
            for item in chart_days
        }
        by_model: dict[str, int] = {}
        for row in rows:
            created = _parse_utc(row[0])
            if created is None:
                continue
            local_day = created.astimezone(self.tz).strftime("%Y-%m-%d")
            bucket = by_day.get(local_day)
            if bucket is None:
                continue
            tokens = sum(int(value or 0) for value in (row[3], row[4], row[5]))
            bucket["tokens"] += tokens
            bucket["calls"] += 1
            if str(row[1] or "") == "completed":
                bucket["completed"] += 1
            if local_day not in window_set:
                continue
            model = str(row[2] or "未知模型")
            by_model[model] = by_model.get(model, 0) + tokens

        total = sum(
            bucket["tokens"] for key, bucket in by_day.items() if key in window_set
        )
        calls = sum(
            bucket["calls"] for key, bucket in by_day.items() if key in window_set
        )
        completed = sum(
            bucket["completed"] for key, bucket in by_day.items() if key in window_set
        )
        success_rate = (completed / calls * 100.0) if calls else None
        top_models = []
        for model, tokens in sorted(
            by_model.items(), key=lambda pair: pair[1], reverse=True
        )[:5]:
            share = tokens / total if total else 0.0
            top_models.append(
                {
                    "name": model,
                    "tokens": tokens,
                    "tokens_text": _fmt_num(tokens),
                    "share": share,
                    "share_text": f"{share * 100:.0f}%",
                }
            )

        trend_values: list[int | None] = []
        for item in chart_days:
            day_text = item.isoformat()
            if min_day and day_text < min_day:
                trend_values.append(None)
            else:
                trend_values.append(by_day[day_text]["tokens"])
        trend = _line_chart(
            labels,
            [{"name": "Token", "color": "#8b5cf6", "values": trend_values}],
            y_formatter=_fmt_compact,
            height=150,
        )

        return {
            "available": True,
            "empty_reason": None,
            "total": total,
            "total_text": _fmt_num(total),
            "calls": calls,
            "calls_text": _fmt_num(calls),
            "completed": completed,
            "success_text": (
                f"{success_rate:.1f}%" if success_rate is not None else "--"
            ),
            "top_models": top_models,
            "trend": trend,
            "note": (
                "Token 口径：token_input_other + token_input_cached + token_output；按 AstrBot provider_stats 记录统计。"
                + (
                    "；窗口内请求超过 2 万条，仅统计最近部分，合计可能偏小。"
                    if truncated
                    else ""
                )
            ),
        }

    def _provider_rows(
        self, start_day: date, end_day: date
    ) -> tuple[list[tuple[Any, ...]], str | None, bool, bool]:
        """Provider rows in [start_day, end_day], oldest-first capped by the SQL.

        The fourth value flags that the 20000-row cap was hit, in which case
        the oldest part of the window is missing from every aggregate.
        """

        connection = self._readonly_db()
        if connection is None:
            return [], None, False, False
        try:
            rows = connection.execute(
                """
                SELECT created_at, status, provider_model,
                       token_input_other, token_input_cached, token_output
                FROM provider_stats
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at DESC
                LIMIT 20000
                """,
                (
                    f"{(start_day - timedelta(days=2)).isoformat()} 00:00:00",
                    f"{(end_day + timedelta(days=3)).isoformat()} 00:00:00",
                ),
            ).fetchall()
            min_row = connection.execute(
                "SELECT MIN(created_at) FROM provider_stats"
            ).fetchone()
        except Exception:
            return [], None, False, False
        finally:
            connection.close()
        min_created = str(min_row[0]) if min_row and min_row[0] else None
        truncated = len(rows) >= 20000
        return rows, min_created, True, truncated


def detect_resource_gaps(
    timestamps: list[int], interval_seconds: int
) -> list[dict[str, int]]:
    """Sampling holes: the plugin or AstrBot itself was down.

    A spacing longer than two sampling intervals (or ten minutes, whichever
    is larger) means no sample could be taken at all, so aggregates for that
    window silently miss real incidents. Surfacing the window keeps an
    otherwise quiet report honest.
    """

    threshold = max(2 * max(60, interval_seconds), 600)
    ordered = sorted({int(ts) for ts in timestamps if ts})
    return [
        {"start": prev, "end": cur}
        for prev, cur in pairwise(ordered)
        if cur - prev > threshold
    ]


def build_report_text(view: dict[str, Any]) -> str:
    """Short text fallback used when the report image cannot be sent."""

    sections = view.get("sections") or []
    lines = [
        f"📊 {view.get('title') or '系统日报'} {view.get('day') or ''}",
        "──────────────",
    ]
    if not sections:
        lines.append("未选择日报内容（daily_report.sections 为空）。")
    # Weekly reports mix single-day figures (resource/QQ snapshot) with
    # window aggregates (Token/website/process), so each line carries the
    # scope it is actually computed over.
    day_label = str(view.get("day_label") or "昨日")
    window_label = str(view.get("window_label") or day_label)
    resources = view.get("resources") or {}
    if "resource" in sections:
        if resources.get("available") and resources.get("yesterday"):
            yesterday = resources["yesterday"]
            gaps_text = resources.get("gaps_text")
            if gaps_text:
                lines.append(f"停机盲区: {gaps_text}")
            lines.append(
                "资源({}): CPU {} / 峰值 {}；RAM {} / 峰值 {}".format(
                    day_label,
                    yesterday["cpu"]["avg_text"],
                    yesterday["cpu"]["peak_text"],
                    yesterday["ram"]["avg_text"],
                    yesterday["ram"]["peak_text"],
                )
            )
        else:
            lines.append(f"资源: {resources.get('empty_reason') or '暂无数据'}")
    anomalies = view.get("anomalies") or {}
    if "anomaly" in sections:
        line = f"异常/失败: {anomalies.get('count', 0)} 次"
        if anomalies.get("unresolved"):
            line += f"（另有 {anomalies['unresolved']} 项未恢复）"
        lines.append(line)
    qq = view.get("qq") or {}
    if "qq" in sections:
        if not qq.get("available", True):
            lines.append(f"QQ: {qq.get('empty_reason') or '暂无数据'}")
        else:
            yesterday = qq.get("yesterday") or {}
            recv_text = yesterday.get("recv_text") or "--"
            if recv_text == "--":
                recv_text = "数据积累中"
            lines.append(
                "QQ({}): 好友 {} / 群 {} / 群员 {}；发送 {} / 接收 {}".format(
                    day_label,
                    yesterday.get("friends_text", "--"),
                    yesterday.get("groups_text", "--"),
                    yesterday.get("members_text", "--"),
                    yesterday.get("sent_text", "--"),
                    recv_text,
                )
            )
    token = view.get("token") or {}
    if "token" in sections:
        if token.get("available"):
            lines.append(
                "Token({}): {}（{} 次，成功率 {}）".format(
                    window_label,
                    token.get("total_text", "--"),
                    token.get("calls_text", "--"),
                    token.get("success_text", "--"),
                )
            )
        else:
            lines.append(f"Token: {token.get('empty_reason') or '暂无数据'}")
    website = view.get("website") or {}
    if "website" in sections:
        if website.get("available"):
            parts = [
                "{} {}（可用率 {}）".format(
                    item.get("name"),
                    item.get("avg_text"),
                    item.get("availability_text"),
                )
                for item in website.get("targets", [])[:3]
            ]
            lines.append(f"网站({window_label}): " + "；".join(parts))
        else:
            lines.append(f"网站: {website.get('empty_reason') or '暂无数据'}")
    process = view.get("process") or {}
    if "process" in sections:
        if process.get("available") and process.get("cpu"):
            top = process["cpu"][0]
            lines.append(
                "Top 进程({}): {} CPU {}".format(
                    window_label, top.get("name"), top.get("cpu_text")
                )
            )
        else:
            lines.append(f"进程: {process.get('empty_reason') or '暂无数据'}")
    lines.append(
        "窗口: {}（{}）".format(view.get("window") or "", view.get("timezone") or "")
    )
    return "\n".join(lines)[:1500]
