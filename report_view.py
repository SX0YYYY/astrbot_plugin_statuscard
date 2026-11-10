from __future__ import annotations

import math
import socket
import sqlite3
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import psutil
from astrbot.api import logger

from .collectors import (
    cfg_bool,
    cfg_get,
    cfg_int,
    format_duration,
    get_timezone,
    now_in_timezone,
)
from .provider_quality import (
    aggregate as aggregate_provider_rows,
)
from .provider_quality import (
    connect_readonly as connect_provider_db,
)
from .provider_quality import (
    error_streak,
    parse_utc_value,
)
from .provider_quality import (
    fetch_recent as fetch_provider_recent,
)
from .provider_quality import (
    fetch_window as fetch_provider_window,
)
from .redactor import Redactor, read_rules
from .report_store import ReportStore
from .utils import redact_url, safe_float, safe_int

RESOURCE_KEYS = ("cpu", "ram", "swap", "disk", "load1")
RESOURCE_LABELS = {
    "cpu": "CPU",
    "ram": "RAM",
    "swap": "SWAP",
    "disk": "磁盘",
    # Stored value is the raw 1-minute load; only the alert threshold divides
    # it by cores, so "每核负载" was reading like a different metric.
    "load1": "1 分钟负载",
}
RRULE_COUNT = len(RESOURCE_KEYS)
VALID_SECTIONS = (
    "resource",
    "anomaly",
    "qq",
    "token",
    "website",
    "process",
    "provider",
)
DEFAULT_SECTIONS = ("resource", "anomaly", "qq", "token")

# The Chinese duration words the report produces, mapped to a stable code the
# dashboard can translate. The English page showed "进行中" and the
# "（窗口前开始）" suffix verbatim (v2.20.14).
_DURATION_CODES = {
    "进行中": "ongoing",
    "跨窗口关闭": "cross_window",
}


def _duration_code(duration_text: str) -> str | None:
    """The stable code for a rendered duration, or None for a plain one.

    The suffix "（窗口前开始）" means the outage started before the report
    window; it is appended to a normal duration, so it is checked first.
    """

    text = str(duration_text or "")
    # "跨窗口关闭（窗口前开始）" carries BOTH meanings; the window-close
    # reason must win, or the English page says the outage merely started
    # earlier instead of explaining that its end was never observed.
    if text.startswith("跨窗口关闭"):
        return "cross_window"
    if "（窗口前开始）" in text:
        return "started_before_window"
    return _DURATION_CODES.get(text)


def _fmt_num(value: Any) -> str:
    if value is None:
        return "--"
    try:
        return f"{round(float(value)):,}"
    except (TypeError, ValueError, OverflowError):
        return "--"


def _fmt_seconds(value: Any) -> str:
    """Human seconds for latency/TTFT: ``--``, ``1.23s``, ``12.3s``, ``123s``."""

    if value is None:
        return "--"
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return "--"
    if seconds < 0 or not math.isfinite(seconds):
        return "--"
    if seconds < 10:
        return f"{seconds:.2f}s"
    if seconds < 100:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _fmt_compact(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if not math.isfinite(number):
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


def _numeric(value: Any) -> float | None:
    """Best-effort float for chart input; non-numeric values become gaps."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
        number
        for item in series
        for value in item.get("values", [])
        if value is not None
        for number in (_numeric(value),)
        if number is not None
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
            x, y = xy(index, safe_float(value, 0.0))
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
            (index, safe_float(value, 0.0))
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
    if maximum <= 1:
        # An all-zero series has nothing to scale: five ticks all collapse to
        # "0"/"1" and read like duplicated axis labels.
        for index in (0, 4):
            value = maximum * index / 4
            y = top + plot_h * (1 - index / 4)
            y_labels.append({"y": round(y, 1), "text": y_formatter(value)})
    else:
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


def _pair_key(item: dict[str, Any], name: str | None = None) -> tuple:
    """Episode key for one event row.

    The first three slots are the historical rule identity
    (``kind, name, target``). The fourth is the monitor's stable state key
    (``item_key``) when the row carries one; legacy rows written by <=2.20.27
    have ``item_key IS NULL`` and therefore always compare as ``""`` -- two
    legacy rows keep exactly the pairing they had before this column existed.
    Two monitor items that share kind+name+target but differ in keyword/query
    now occupy different slots and cannot close each other's episode.

    ``name`` is optional because the call sites already computed it (and skip
    empty names); passing it avoids reading the field twice.
    """

    return (
        str(item.get("kind") or ""),
        str(item.get("name") or "") if name is None else name,
        str(item.get("target") or ""),
        str(item.get("item_key") or ""),
    )


def _event_start(item: dict[str, Any]) -> int:
    """When the interruption began, for display and duration purposes.

    ``ts`` is the moment the row was written, which for an alert/failed row is
    the PUSH moment: with ``fail_threshold`` > 1 the first failed probe is
    ``(threshold-1)*interval`` earlier. The recovery notice has always billed
    from that first failure (``state["since"]``), so reading ``started_ts``
    here makes the daily report agree with it. Rows written by <=2.20.29 (and
    every recovery row) carry NULL and fall back to ``ts``, which is exactly
    the old behaviour.

    Pairing is deliberately NOT driven by this: episode keys and the
    ``episode_of`` bookkeeping stay on ``ts`` so this column cannot reshuffle
    which alert belongs to which recovery.
    """

    ts = safe_int(item.get("ts") or 0)
    started = safe_int(item.get("started_ts") or 0)
    if not started or not ts:
        return started or ts
    # A hand-edited or corrupted state file can persist a future ``since`` or
    # one from years ago. A negative duration (or a 1970 start) is worse than
    # the old push-moment fallback, so only plausible starts are trusted. The
    # bound is wider than the default 90-day retention because
    # ``daily_report.event_retention_days`` is configurable up to 365.
    if started > ts or ts - started > 400 * 86400:
        return ts
    return started


# The provider-quality note is rendered in two places: the report IMAGE
# (always Chinese, built here) and the Web dashboard (translated through
# .astrbot-plugin/i18n/*.json). Keeping the Chinese text in one constant lets
# a contract test prove the dashboard copies never drift; the dashboard adds
# the long/truncated sentences itself, so they are separate constants.
QUALITY_NOTE_BASE = (
    "口径：成功率 = 完成 / (完成 + 失败)；中断（aborted）单列，不计入失败。"
    "中断指调用被取消，不一定是人为中止：含同会话新消息/命令接管、其他插件"
    "停止事件、上游超时被取消等。延迟与 TTFT 仅统计成功调用，TTFT 为 0 视为"
    "未记录。按 AstrBot provider_stats 记录统计。"
)
QUALITY_NOTE_LONG = "窗口内持续 ≥60 秒的中断 {n} 次，多为上游超时后被取消，建议关注。"
QUALITY_NOTE_TRUNCATED = "窗口内请求超过 2 万条，仅统计最近部分，合计可能偏小。"


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

        # The label must follow the report day, not the wall clock: a retry or
        # an offline-recovery delivery can send a report that is two or more
        # days old, and calling it "昨日" made the numbers look like they
        # belonged to yesterday (v2.20.8).
        today = now_in_timezone(self.config).date()
        delta_days = (today - target).days
        if span == 1:
            if delta_days <= 0:
                day_label = "今天"
                day_label_code = "today"
            elif delta_days == 1:
                day_label = "昨日"
                day_label_code = "yesterday"
            else:
                day_label = f"{target.month}月{target.day}日"
                day_label_code = "date"
            title = "系统日报"
            window = f"{target.isoformat()} 00:00–24:00"
            window_label = day_label
            token_label = f"{day_label} · 趋势最近 {history_days} 天"
        else:
            title = "系统周报"
            window = f"{start_day.isoformat()} 00:00 – {target.isoformat()} 24:00"
            day_label = "最后一天"
            day_label_code = "last_day"
            window_label = f"近 {span} 天"
            token_label = f"近 {span} 天合计 · 趋势最近 {history_days} 天"

        view: dict[str, Any] = {
            "title": title,
            "day": target.isoformat(),
            "start_day": start_day.isoformat(),
            "span_days": span,
            "day_label": day_label,
            "day_label_code": day_label_code,
            "window_label": window_label,
            # Structured marker so a non-Chinese dashboard can translate the
            # label instead of printing "昨日" / "10月30日" verbatim.
            "window_label_code": (day_label_code if span == 1 else "last_days"),
            "window_label_days": span if span > 1 else 0,
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
            view["resources"] = self._resource_section(
                target, previous, history_days, span, day_label=day_label
            )
        if "anomaly" in selected:
            view["anomalies"] = self._anomaly_section(
                target, previous, span, day_label=day_label
            )
        if "qq" in selected:
            view["qq"] = self._qq_section(
                target, history_days, span, day_label=day_label
            )
        if "token" in selected:
            view["token"] = self._token_section(
                target, history_days, span, day_label=day_label
            )
        if "provider" in selected:
            view["provider_quality"] = self._provider_quality_section(
                target, history_days, span
            )
        if "website" in selected:
            view["website"] = self._website_section(
                target, previous, history_days, span
            )
        if "process" in selected:
            view["process"] = self._process_section(
                target, previous, history_days, span, day_label=day_label
            )
        view["summary"] = self._summary(view, span)
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

    def _summary(self, view: dict[str, Any], span: int = 1) -> dict[str, Any]:
        sections = view.get("sections") or []
        anomalies = view.get("anomalies") or {}
        token = view.get("token") or {}
        quality = view.get("provider_quality") or {}
        qq = view.get("qq") or {}
        yesterday = qq.get("yesterday") or {}
        # A figure for a section the operator did not select would read as a
        # real "0" or "no data"; label it as not enabled instead.
        return {
            # The four headline cards mix scopes in a weekly report (anomalies
            # and tokens cover 7 days, sent/received only the last day); the
            # template prints these labels so the numbers are not misread.
            "span_days": max(1, int(span or 1)),
            "window_label": str(
                view.get("window_label") or ("昨日" if span == 1 else f"近 {span} 天")
            ),
            "window_label_code": str(view.get("window_label_code") or ""),
            "window_label_days": int(view.get("window_label_days") or 0),
            "last_day_label": "最后一天",
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
            # The text above is Chinese and the daily-report image prints it
            # verbatim, so it stays. The code lets the English dashboard
            # show its own wording instead of the Chinese fallback (v2.20.14).
            "token_total_code": (
                "no_data"
                if "token" in sections and token.get("total_text") == "暂无数据"
                else ""
            ),
            "token_enabled": "token" in sections,
            "provider_calls_text": (
                (quality.get("stats") or {}).get("calls_text", "暂无数据")
                if "provider" in sections
                else "未开启"
            ),
            "provider_enabled": "provider" in sections,
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
        self,
        day: date,
        previous: date,
        history_days: int,
        span: int = 1,
        day_label: str = "昨日",
    ) -> dict[str, Any]:
        span = max(1, int(span or 1))
        # A weekly report compares against the previous period, so the query
        # window must reach back at least two spans; otherwise "较上期" can
        # never be computed (it silently disappeared with history_days=7).
        lookback = max(history_days, 2 * span)
        query_start = day - timedelta(days=lookback - 1)
        start_ts = int(
            datetime.combine(query_start, time.min, tzinfo=self.tz).timestamp()
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
        # The report window itself: one day for a daily report, the last
        # ``span`` days for a weekly one. A weekly report used to average only
        # its last day while labelling the figure "较上期".
        window_days = [
            day - timedelta(days=offset) for offset in range(span - 1, -1, -1)
        ]
        yesterday_rows = [
            row for item in window_days for row in by_day.get(item.isoformat(), [])
        ]
        prev_rows = [
            row
            for offset in range(span)
            for row in by_day.get((previous - timedelta(days=offset)).isoformat(), [])
        ]
        available = len(yesterday_rows) >= 2
        empty_reason_code = None
        empty_reason_vars: dict[str, Any] = {}
        if not rows:
            first_ts = self.store.first_resource_ts()
            if first_ts is not None:
                first_day = datetime.fromtimestamp(first_ts, self.tz).strftime(
                    "%Y-%m-%d"
                )
                empty_reason = (
                    f"资源采样从 {first_day} 开始，暂无 {day.isoformat()} 数据"
                )
                empty_reason_code = "resource_since"
                empty_reason_vars = {"first_day": first_day, "day": day.isoformat()}
            else:
                empty_reason = "暂无资源采样数据"
                empty_reason_code = "reason_no_resource_samples"
        elif not available:
            # Report the REAL first sample, not the oldest day inside the
            # lookback window: that window slides every day, so the notice
            # said "sampling starts 2026-10-30" one day and "10-31" the next
            # (2026-11-06 audit). A dirty timestamp must not break the whole
            # report; keep the window value in that case.
            try:
                first_ts = self.store.first_resource_ts()
                if first_ts is not None:
                    first_day = datetime.fromtimestamp(first_ts, self.tz).strftime(
                        "%Y-%m-%d"
                    )
            except (OSError, OverflowError, ValueError):
                pass
            if span == 1:
                empty_reason = (
                    f"资源采样从 {first_day or '--'} 开始，暂无完整{day_label}数据"
                )
                empty_reason_code = "resource_incomplete_day"
                # The structured code is rendered on the English page too;
                # an ISO date keeps both languages free of Chinese words.
                empty_reason_vars = {
                    "first_day": first_day or "--",
                    "day": day.isoformat(),
                }
            else:
                empty_reason = (
                    f"资源采样从 {first_day or '--'} 开始，暂无完整近 {span} 天数据"
                )
                empty_reason_code = "resource_incomplete_span"
                empty_reason_vars = {"first_day": first_day or "--", "span": span}
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

        if span == 1:
            # 24 hourly buckets for a daily report.
            buckets: dict[str, list[dict[str, Any]]] = {
                f"{hour:02d}": [] for hour in range(24)
            }
            for row in yesterday_rows:
                local = datetime.fromtimestamp(int(row["ts"]), self.tz)
                buckets[f"{local.hour:02d}"].append(row)
            hour_labels = list(buckets)
            hour_label = f"{day_label} 24 小时"
            hour_note = "按小时均值"
        else:
            # One bucket per day for a weekly report: averaging the last day
            # only and calling it the week was the original defect.
            buckets = {item.strftime("%m-%d"): [] for item in window_days}
            for row in yesterday_rows:
                local = datetime.fromtimestamp(int(row["ts"]), self.tz)
                key = local.strftime("%m-%d")
                if key in buckets:
                    buckets[key].append(row)
            hour_labels = list(buckets)
            hour_label = f"近 {span} 天"
            hour_note = "按日均值"

        def hourly_values(key: str) -> list[float | None]:
            out: list[float | None] = []
            for label in hour_labels:
                values: list[float] = []
                for item in buckets[label]:
                    raw = item.get(key)
                    if raw is None:
                        continue
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        # A single corrupt row (hand-edited DB or a bad cache
                        # backfill) must not fail the whole report build and
                        # leave the user without a daily report.
                        continue
                    if math.isfinite(value):
                        values.append(value)
                out.append(sum(values) / len(values) if values else None)
            return out

        hourly_chart = _line_chart(
            hour_labels,
            [
                {"name": "CPU", "color": "#2ecc71", "values": hourly_values("cpu")},
                {"name": "RAM", "color": "#4f8cff", "values": hourly_values("ram")},
                {"name": "SWAP", "color": "#f59e0b", "values": hourly_values("swap")},
            ],
            y_max=100,
            y_formatter=lambda value: f"{value:.0f}%",
        )

        chart_start = day - timedelta(days=history_days - 1)
        days = [chart_start + timedelta(days=i) for i in range(history_days)]
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
        if first_day and first_day > chart_start.isoformat():
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
        expected = max(1, 86400 // interval) * span
        coverage = len(yesterday_rows) / expected
        gaps = detect_resource_gaps(
            [int(row.get("ts") or 0) for row in yesterday_rows],
            max(2 * interval, 600),
        )
        gaps_text = None
        if gaps:
            total_minutes = sum((gap["end"] - gap["start"]) for gap in gaps) // 60
            gaps_text = (
                f"{len(gaps)} 段共约 {total_minutes} 分钟"
                "（采样缺失，期间插件或 Bot 停机、无法监测）"
            )
        scope_text = day_label if span == 1 else f"近 {span} 天"
        coverage_note = (
            f"{scope_text}资源采样 {len(yesterday_rows)} 个点"
            f"（约覆盖 {coverage * 100:.0f}%），"
            "均值和峰值为已有数据口径。"
            + (f"停机盲区：{gaps_text}。" if gaps_text else "")
            if available and (coverage < 0.9 or gaps)
            else None
        )

        return {
            "available": available,
            "empty_reason": empty_reason,
            "empty_reason_code": empty_reason_code,
            "empty_reason_vars": empty_reason_vars,
            # "较前日" is only meaningful for a single-day report; a weekly
            # report compares against the previous period (or shows nothing
            # when that window has no samples).
            "delta_label": "较前日" if span == 1 else "较上期",
            "hour_label": hour_label,
            "hour_note": hour_note,
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
            # A finite but absurd value (1e300 from a hand-edited/external
            # row) still overflows the SUM to inf and renders a 300-digit
            # peak. No real CPU/RAM/disk percentage/load sample is anywhere
            # near 1e9; treat it as invalid rather than printing "inf%".
            if abs(number) > 1e9:
                continue
            # A negative CPU/RAM/disk percentage can only come from a
            # hand-edited or corrupted row: drop it like any other invalid
            # sample. Including it (or clamping it to zero) pulled the day's
            # average down and disagreed with the visible data.
            if number < 0:
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
        # A finite value close to float's maximum can still overflow the
        # SUM (inf), and a 1e300 peak renders as a 300-digit number that
        # breaks the card. Only hand-edited/external rows can carry these,
        # but the report should degrade to "--", not print "inf%".
        if not math.isfinite(average):
            average = None
        if not math.isfinite(peak):
            peak = None
        digits = 2 if key == "load1" else 1
        return {
            "avg": average,
            "peak": peak,
            "avg_text": ("--" if average is None else f"{average:.{digits}f}{unit}"),
            "peak_text": "--" if peak is None else f"{peak:.{digits}f}{unit}",
            "delta_text": "--",
            "count": len(values),
        }

    # ------------------------------------------------------------------
    # anomalies
    # ------------------------------------------------------------------

    def _event_lookback_seconds(self) -> int:
        """How far back to look for an episode that started before the window.

        The old fixed 30 days hid a fault that had been running longer than
        that: its only alert row was older than the lookback, so the report
        said "no anomalies" while the service was still down. Follow the
        configured event retention instead (default 90 days); the DB prunes
        rows beyond it anyway, so the query cannot grow without bound.
        """

        days = cfg_int(self.config, "daily_report.event_retention_days", 90)
        return max(1, min(365, days)) * 86400

    def _anomaly_section(
        self,
        day: date,
        previous: date,
        span: int = 1,
        day_label: str = "昨日",
    ) -> dict[str, Any]:
        window_days = {
            (day - timedelta(days=offset)).isoformat() for offset in range(span)
        }
        events = self.store.list_events(previous.isoformat(), limit=5000)
        for offset in range(span):
            boundary = day - timedelta(days=offset)
            events += self.store.list_events(boundary.isoformat(), limit=5000)
        # The per-day query caps at 5000 rows and drops the OLDEST rows of the
        # day. A flapping rule can exceed that, which made the pairing below
        # under-count interruptions (and the headline disagree with the event
        # page). Detect the overflow and disclose it instead of silently
        # reporting a smaller number (v2.20.24).
        #
        # The dropped rows are the OLDEST ones, which is exactly where the
        # start of an interruption lives: a truncated day can lose a whole
        # episode, and the durations of the episodes that survive can be
        # shortened. The disclosure says so.
        event_row_limit = 5000
        data_truncated = 0
        for day_text in window_days | {previous.isoformat()}:
            total_rows = sum(self.store.count_events(day_text).values())
            if total_rows > event_row_limit:
                data_truncated += total_rows - event_row_limit
        events.sort(
            key=lambda item: (
                safe_int(item.get("ts") or 0),
                safe_int(item.get("id") or 0),
            )
        )
        window_start_dt = datetime.combine(
            day - timedelta(days=span - 1), time.min, tzinfo=self.tz
        )
        window_start_ts = int(window_start_dt.timestamp())
        # ``resolved_ts`` is filled in when a condition clears, so the
        # duration survives a plugin restart. Events recorded before that
        # field was used fall back to pairing them in memory.
        #
        # The pairing walks the events of one rule in chronological order.
        # A ``recovery`` closes the currently open episode of that rule, so
        # the next alert starts a NEW episode. This matters because
        # ``close_all`` writes ONE resolved_ts onto every open alert of a
        # rule: an orphan from three days ago and yesterday's blip share
        # that timestamp, and reading it as "this alert lasted until then"
        # rendered a 90-second outage as "3天22:27:29" (v2.20.12).
        #
        # ``open_episode`` maps a rule to the episode that is currently open
        # for it: the start of the interruption and the moment it ended (0
        # while it is still running).
        open_episode: dict[tuple[str, str, str, str], tuple[int, int]] = {}
        # The moment each alert row's interruption actually began, keyed by
        # (rule, row ts). Alert rows carry ``started_ts`` (the first failed
        # probe); legacy rows and recovery rows fall back to their own ts.
        # Only display and duration read this -- the pairing dicts above and
        # below keep using ``ts`` so a late push cannot reshuffle episodes.
        display_start: dict[tuple[tuple[str, str, str, str], int], int] = {}
        # An interruption that began before the window still has an alert row
        # outside ``events``; load the preceding month so a recovery inside
        # the window can be paired with it instead of showing "--".
        #
        # Sorted by time: the query returns newest-first, so a recovery was
        # popped before its own alert was seen again and the stale start time
        # survived into the window (a 30-second blip rendered as 16 hours).
        #
        # Rows already stamped by ``close_all`` are remembered here: when a
        # fault outlives the previous day, its alert row is outside
        # ``events`` but carries THIS recovery's timestamp, and the recovery
        # branch below needs that row to recover the start (v2.20.20).
        outside_stamped: dict[tuple[str, str, str, str], list[tuple[int, int]]] = {}
        # Keep the row itself: an outage that spans the WHOLE window needs its
        # name/detail to render a row, and that row lives outside ``events``.
        outside_rows: dict[tuple[str, str, str, str], dict[int, dict[str, Any]]] = {}
        # Recovery events before the window. They prove an episode actually
        # ended before the window started, even when ``close_all`` later
        # stamped the old alert row with an unrelated (much later) recovery
        # -- the historical orphan rows have exactly this shape.
        outside_recoveries: dict[tuple[str, str, str, str], list[int]] = {}
        outside_lookback = self._event_lookback_seconds()
        for alert in sorted(
            self.store.list_events_range(
                window_start_ts - outside_lookback, window_start_ts - 1, limit=5000
            ),
            key=lambda item: (
                safe_int(item.get("ts") or 0),
                safe_int(item.get("id") or 0),
            ),
        ):
            status = str(alert.get("status") or "")
            name = str(alert.get("name") or "")
            if not name:
                continue
            key = _pair_key(alert, name)
            stamp = safe_int(alert.get("ts") or 0)
            if status in {"alert", "failed"}:
                display_start[(key, stamp)] = _event_start(alert)
                # A "failed" row means the fault happened but its push did
                # not arrive; it still starts an interruption for pairing.
                # A row that already carries a ``resolved_ts`` belongs to a
                # CLOSED episode: its own recovery has been accounted for,
                # so it must not become the open episode (v2.20.12).
                if alert.get("resolved_ts"):
                    resolved = safe_int(alert.get("resolved_ts") or 0)
                    if resolved:
                        outside_stamped.setdefault(key, []).append((stamp, resolved))
                        bucket = outside_rows.setdefault(key, {})
                        # A "failed" row written in the same second as the
                        # delivered "alert" row must not hide it: the covering
                        # row is then skipped and a whole-window outage
                        # disappears from the report (2026-11-06 audit).
                        existing = bucket.get(stamp)
                        if (
                            existing is None
                            or str(alert.get("status") or "") == "alert"
                        ):
                            bucket[stamp] = alert
                    open_episode.pop(key, None)
                    continue
                current = open_episode.get(key)
                if current is None:
                    open_episode[key] = (stamp, 0)
                else:
                    # Keep the earliest start of the episode that is still
                    # open: several rows of one flickering interruption
                    # belong together.
                    open_episode[key] = (min(current[0], stamp), 0)
            elif status == "recovery":
                # A recovery before the window closes the episode for pairing
                # purposes; otherwise a later recovery could be matched with
                # an interruption that had already ended.
                open_episode.pop(key, None)
                outside_recoveries.setdefault(key, []).append(stamp)
        items: list[dict[str, Any]] = []
        count = 0
        # One interruption can leave both a "failed" row (the first push did
        # not arrive) and an "alert" row (the retry succeeded), and a
        # flickering threshold can leave several rows before the condition
        # clears. All of them belong to ONE episode, so the episode must be
        # counted once.
        #
        # The pairing walks the events of a rule in chronological order:
        #
        #   * an "alert"/"failed" row joins the episode that is currently
        #     open for that rule, or opens one when there is none;
        #   * a "recovery" row closes the open episode.
        #
        # A recovery is therefore the only thing that separates two
        # outages of the same rule. This keeps the two rows of one
        # interruption together, keeps a flickering threshold as one
        # anomaly, and separates two genuine outages.
        #
        # ``close_all`` writes ONE resolved_ts onto every open alert of a
        # rule, so that timestamp alone identifies nothing: the weekly
        # report used to read it as "this alert lasted until then" and
        # rendered "已恢复 3天22:27:29" for a 90-second outage, while the
        # outage that really happened was swallowed by the de-duplication
        # (v2.20.12).
        seen_ids: set[tuple[tuple[str, str, str, str], int]] = set()
        # For every alert row: the start of its episode and the moment that
        # episode ended (0 while it is still open).
        episode_of: dict[tuple[tuple[str, str, str, str], int], tuple[int, int]] = {}
        # For every recovery row: the episode it closed.
        recovery_episode: dict[
            tuple[tuple[str, str, str, str], int], tuple[int, int]
        ] = {}
        # ``open_episode`` already holds the interruptions that began before
        # the window; the scan below continues from there, so a recovery
        # inside the window closes the right episode.
        for event in events:
            status = str(event.get("status") or "")
            name = str(event.get("name") or "")
            if not name:
                continue
            key = _pair_key(event, name)
            stamp = safe_int(event.get("ts") or 0)
            if status in {"alert", "failed"}:
                display_start[(key, stamp)] = _event_start(event)
                current = open_episode.get(key)
                if current is None:
                    # A new episode of this rule.
                    current = (stamp, 0)
                    open_episode[key] = current
                episode_of[(key, stamp)] = current
            elif status == "recovery":
                current = open_episode.pop(key, None)
                if current is None:
                    # The episode began before the loaded events (the window
                    # plus the previous day): its alert row is outside, but
                    # ``close_all`` stamped it with THIS recovery's
                    # timestamp. Take the NEAREST such row as the start so
                    # the recovery day keeps its count and duration instead
                    # of showing "恢复 --" (v2.20.20). Nearest (max ts)
                    # avoids reviving an orphan from days ago -- that used to
                    # turn a 90-second outage into "3天22:27:29" (v2.20.12).
                    candidates = [
                        ts
                        for ts, resolved in outside_stamped.get(key, [])
                        if resolved == stamp
                    ]
                    if not candidates:
                        continue
                    current = (max(candidates), 0)
                start = current[0]
                # Every row of that episode now ends here.
                for member, member_episode in list(episode_of.items()):
                    if member[0] == key and member_episode[0] == start:
                        episode_of[member] = (start, stamp)
                # The recovery row itself describes that episode.
                recovery_episode[(key, stamp)] = (start, stamp)
                # The FIRST row of the episode may carry a resolved_ts of its
                # own that does not match this recovery. That means the row
                # was closed earlier -- it is an orphan whose recovery event
                # was lost -- and the outage that really happened is the one
                # this recovery closes. The orphan must not take the episode
                # start with it, otherwise the real outage inherits the
                # orphan's start time and a 90-second outage is reported as
                # "07:01:30 已恢复" (v2.20.14).
                #
                # The rows of ONE interruption all carry the same resolved_ts
                # (``close_all`` writes one value onto every open row), so
                # they are never separated here.
                first_row = next(
                    (
                        item
                        for item in events
                        if str(item.get("status") or "") in {"alert", "failed"}
                        and _pair_key(item) == key
                        and safe_int(item.get("ts") or 0) == start
                    ),
                    None,
                )
                if first_row is not None:
                    own = first_row.get("resolved_ts")
                    own_ts = safe_int(own) if own is not None else None
                    # Only a row closed at a DIFFERENT moment is an orphan.
                    #
                    # ``close_all=True`` (the only production caller) stamps
                    # every still-open row of the rule with the recovery's
                    # timestamp, including an orphan from an earlier fault.
                    # Such an orphan therefore carries ``own_ts == stamp`` and
                    # cannot be distinguished from the first row of one
                    # interrupted episode. Treating ``own_ts is None`` (the
                    # pre-v2.12 historical rows) as an orphan split ONE
                    # failed+alert episode into two rows (v2.20.17 audit).
                    if own_ts is not None and own_ts != stamp:
                        # The orphan closes at its own moment; the episode
                        # starts at the first row that is still open.
                        orphan_end = own_ts
                        later = [
                            safe_int(item.get("ts") or 0)
                            for item in events
                            if str(item.get("status") or "") in {"alert", "failed"}
                            and _pair_key(item) == key
                            and safe_int(item.get("ts") or 0) > start
                            and safe_int(item.get("ts") or 0) <= stamp
                        ]
                        if later:
                            real_start = min(later)
                            episode_of[(key, start)] = (start, orphan_end)
                            episode_of[(key, real_start)] = (real_start, stamp)
                            recovery_episode[(key, stamp)] = (real_start, stamp)

        # Keys whose episode started inside the window and is still open; the
        # unresolved count below reads this set.
        episode_open: set[tuple[str, str, str, str]] = set()
        # Row index of the last alert/failed row per key. A recovery that
        # belongs to a row already shown must not append a second line: an
        # outage that started AND ended inside the window used to appear twice
        # ("已恢复 00:01:30" followed by "恢复 00:01:30"), which reads like two
        # incidents.
        emitted: dict[tuple[str, str, str, str], int] = {}
        # Keys that already have a row in this section. The whole-window
        # sweep below must not duplicate an outage that is already shown.
        rendered_keys: set[tuple[str, str, str, str]] = set()
        for event in events:
            status = str(event.get("status") or "")
            name = str(event.get("name") or "")
            key = _pair_key(event, name)
            resolved_ts = event.get("resolved_ts")
            started_i = safe_int(event.get("ts") or 0)
            outside = str(event.get("day") or "") not in window_days
            if outside:
                # Rendering skips events outside the window. Their EPISODE
                # bookkeeping (a pre-window recovery closing its episode) is
                # the pairing loop's job above -- dropping it here too used
                # to pair an in-window recovery with a stale start time from
                # the day before (16 hours for a 30-second blip).
                continue
            if status not in {"alert", "recovery", "failed"}:
                continue
            duration_text = "--"
            if status == "recovery":
                episode = recovery_episode.get((key, started_i))
                if episode is None:
                    # A recovery without a matching alert row (an old
                    # database, or a row that was pruned): there is no start
                    # to measure the duration from.
                    start = None
                else:
                    start = episode[0]
                # The pairing key stays on ``ts``; the outage is billed and
                # positioned from the first failed probe (``started_ts``), so
                # this row agrees with the recovery notice the operator got.
                start_display = (
                    display_start.get((key, start), start) if start else None
                )
                # A fault that began before the window and recovered inside
                # it has no in-window alert row, so the counter never counted
                # it while the table showed it: "异常 0 次" above a 20-minute
                # outage (v2.20.17). Count it here unless its alert row was
                # already counted above.
                if (
                    start_display
                    and start_display < window_start_ts
                    and (key, start) not in seen_ids
                ):
                    count += 1
                # The episode is over: a later alert with the same key is a
                # new one and must count again.
                seen_ids = {sid for sid in seen_ids if sid[0] != key}
                episode_open.discard(key)
                if start_display:
                    if started_i - start_display > (span + 1) * 86400:
                        # Same guard as the alert branch below. The recovery
                        # branch used to overwrite the guarded "跨窗口关闭"
                        # with the raw duration, so a three-day-old orphan
                        # became "3天01:05:00" (v2.20.17).
                        duration_text = "跨窗口关闭"
                        if start_display < window_start_ts:
                            duration_text += "（窗口前开始）"
                    else:
                        duration_text = format_duration(started_i - start_display)
                        if start_display < window_start_ts:
                            duration_text += "（窗口前开始）"
                prior = emitted.get(key)
                if prior is not None and prior < len(items):
                    previous = items[prior]
                    if previous.get("status_key") == "recovered":
                        # The episode is already rendered with its duration
                        # (resolved_ts was written back), so this recovery
                        # event tells the same story. Appending it printed the
                        # same outage twice: "已恢复 00:01:30" + "恢复 00:01:30".
                        continue
                    # The alert row is still "进行中" because old data has no
                    # resolved_ts; complete that row instead of adding another.
                    previous["status"] = "已恢复"
                    previous["status_key"] = "recovered"
                    previous["duration"] = duration_text
                    # The row is no longer "ongoing": it has a duration now.
                    # Leaving the marker behind made the English dashboard
                    # show "Ongoing" for an outage that had already
                    # recovered (v2.20.14).
                    previous["duration_code"] = _duration_code(duration_text)
                    continue
            elif status in {"alert", "failed"}:
                episode = episode_of.get((key, started_i))
                if episode is None:
                    episode_start, episode_end = started_i, 0
                else:
                    episode_start, episode_end = episode
                # Same split as the recovery branch: ``episode_start`` stays
                # the pairing key, ``episode_display_start`` is the real first
                # failure used for the duration and the time column.
                episode_display_start = display_start.get(
                    (key, episode_start), episode_start
                )
                if not episode_end:
                    # The recovery of this episode is not inside the window
                    # (a fault that ended after midnight). The row carries the
                    # exact end in ``resolved_ts``, which the alert engine
                    # wrote when the condition cleared; use it instead of
                    # pretending the duration is unknown (v2.20.12).
                    episode_end = safe_int(resolved_ts or 0)
                episode_id = (key, episode_start)
                if episode_id in seen_ids:
                    continue
                seen_ids.add(episode_id)
                count += 1
                if resolved_ts:
                    # The episode ended after the report day (its recovery
                    # event belongs to the next day, but resolved_ts was
                    # written back). It is complete, so it must not inflate
                    # "未恢复 N 项" while showing a finished duration.
                    episode_open.discard(key)
                    if not episode_end:
                        # Neither a recovery event nor a resolved_ts: the real
                        # end of the outage is unknown, so no duration is
                        # invented.
                        duration_text = "跨窗口关闭"
                    elif episode_end - episode_display_start > (span + 1) * 86400:
                        # close_all wrote the same resolved_ts onto every old
                        # open episode of a rule; pairing the newest alert
                        # with it turned a 5-minute outage into "5 days".
                        duration_text = "跨窗口关闭"
                    else:
                        duration_text = format_duration(
                            episode_end - episode_display_start
                        )
                        if episode_display_start < window_start_ts:
                            # The alert row is rendered (not the recovery
                            # row) when the episode began before the window
                            # but an in-window alert/failed row exists. The
                            # recovery branch already marked this; omitting
                            # it here printed an in-window time with a
                            # duration measured from the previous day
                            # (v2.20.19).
                            duration_text += "（窗口前开始）"
                else:
                    episode_open.add(key)
                    duration_text = "进行中"
            # A weekly window spans several days, so a bare "HH:MM" cannot
            # locate the event inside it. The same applies to a start that
            # falls on another day: "23:59" under a report for the 9th read as
            # if it happened that day (2026-11-10 reverse audit).
            row_time = (
                display_start.get((key, started_i), started_i)
                if status in {"alert", "failed"}
                else started_i
            )
            row_dt = datetime.fromtimestamp(row_time, self.tz)
            time_format = (
                "%H:%M" if span == 1 and row_dt.date() == day else "%m-%d %H:%M"
            )
            rendered_keys.add(key)
            # The time column shows when the interruption actually started,
            # not when its push happened: the recovery notice and the report
            # then quote the same moment (v2.20.30). Recovery rows keep their
            # own ts -- that IS the moment the service came back.
            items.append(
                {
                    # Internal sort key: the visible time, not the push ts.
                    # Removed by the sort right after this loop.
                    "_sort_ts": row_time,
                    "time": row_dt.strftime(time_format),
                    "kind": self._kind_text(str(event.get("kind") or "")),
                    "kind_slug": str(event.get("kind") or ""),
                    "name": self.redactor.mask_name(redact_url(name)) if name else "--",
                    "status": (
                        "已恢复"
                        if status in {"alert", "failed"}
                        and resolved_ts
                        and duration_text not in ("跨窗口关闭",)
                        else self._status_text(status)
                    ),
                    "status_key": (
                        "recovered"
                        if status in {"alert", "failed"}
                        and resolved_ts
                        and duration_text not in ("跨窗口关闭",)
                        else status
                    ),
                    "detail": self.redactor.mask_addresses(
                        str(event.get("detail") or "")
                    )[:80],
                    "duration": duration_text,
                    # Structured marker so the English dashboard can translate
                    # the Chinese duration words instead of mixing them into
                    # the table: "进行中" (ongoing), "跨窗口关闭"
                    # (cross_window) and the "（窗口前开始）" suffix
                    # (started_before_window).
                    "duration_code": _duration_code(duration_text),
                }
            )
            if status in {"alert", "failed"}:
                emitted[key] = len(items) - 1

        # The rows were appended in push-ts order, but the time column shows
        # the start. A push delayed past a later rule's alert (offline retry,
        # cooldown record, silent-window self-heal) would print a later time
        # above an earlier one. Re-sort by the visible moment; the unresolved
        # and whole-window rows are prepended after this and stay on top.
        items.sort(key=lambda item: item.pop("_sort_ts", 0) or 0)

        # An interruption that began before the window and never got a
        # resolved timestamp (e.g. its recovery notice was lost to a reload)
        # would otherwise read as "no anomalies". Surface it explicitly.
        def _event_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
            return _pair_key(item)

        stale: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        # The query keeps the earliest unclosed row of each rule (grouped by
        # kind/name/target) and returns them ascending, so a flapping rule
        # cannot spend the limit on duplicate rows and hide newer outages
        # (v2.20.24). Its NOT EXISTS clause already guarantees that a row
        # returned here has NO later recovery of the same rule -- so a
        # recovery from an EARLIER incident (yesterday's flap) must not
        # filter it out: a rule that recovered yesterday and broke again
        # afterwards used to vanish from the report entirely (v2.20.24
        # audit follow-up), because this filter matched on the rule key
        # alone. No filter is needed: the storage query is the guard.
        for alert in self.store.list_unresolved_alerts(
            window_start_ts, window_start_ts - outside_lookback, limit=50
        ):
            key = _event_key(alert)
            if not key[1]:
                continue
            # Oldest start per rule/target wins ("ongoing since ...").
            if key not in stale:
                stale[key] = alert
        unresolved_items: list[dict[str, Any]] = []
        for alert in stale.values():
            # "Ongoing since ..." must be the first failed probe, not the push
            # moment, or the row disagrees with the alert text in the chat.
            started = datetime.fromtimestamp(_event_start(alert), self.tz)
            unresolved_items.append(
                {
                    "time": started.strftime("%m-%d %H:%M"),
                    "kind": self._kind_text(str(alert.get("kind") or "")),
                    "kind_slug": str(alert.get("kind") or ""),
                    "name": self.redactor.mask_name(
                        redact_url(str(alert.get("name") or "--"))
                    ),
                    "status": "未恢复",
                    # A dedicated key, not "alert": the English dashboard
                    # maps "alert" to "Alert", which told the operator that
                    # a NEW incident had happened instead of showing the
                    # outage that is still open. The report image also
                    # coloured the row with the alert badge (v2.20.12).
                    "status_key": "unresolved",
                    "detail": "开始于此处，未记录恢复时刻（可能已恢复）",
                    "duration": "进行中",
                    # The normal rows carry a code for the English dashboard;
                    # this row used to show the raw Chinese text there
                    # ("进行中" instead of "Ongoing", v2.20.17).
                    "duration_code": "ongoing",
                }
            )
        # An outage that covered the WHOLE window and recovered only after it
        # has no in-window row: its alert is outside the loaded events and
        # its recovery is after the window, so the day filter drops both and
        # the unresolved query (resolved_ts IS NULL) skips it too. The report
        # then claimed "no anomalies" for a day the service was down from
        # start to finish. Emit one row covering that window (v2.20.21).
        #
        # Only rows whose push succeeded ("alert") are used. A "failed" row
        # is the shape of the historical orphan (its recovery event was never
        # written; close_all later stamped it with an unrelated recovery), so
        # accepting it added a fake whole-window row to every earlier report
        # in the online database. An outage whose first push also failed is
        # not reported this way -- the unresolved list still covers outages
        # that never recovered.
        window_end_ts = int(
            datetime.combine(
                day + timedelta(days=1), datetime.min.time(), tzinfo=self.tz
            ).timestamp()
        )
        covering_rows: list[dict[str, Any]] = []
        for key, stamped in outside_stamped.items():
            if key in rendered_keys or not key[1]:
                continue
            # Group by the stamping recovery: ONE recovery closes one
            # episode. Within a batch use the latest DELIVERED ("alert") row
            # as the start -- a "failed" row right after it belongs to the
            # same episode and must not hide it.
            batches: dict[int, list[int]] = {}
            for start, resolved in stamped:
                if resolved >= window_end_ts:
                    batches.setdefault(resolved, []).append(start)
            for resolved, starts in batches.items():
                alert_starts = [
                    ts
                    for ts in starts
                    if str(
                        (outside_rows.get(key, {}).get(ts) or {}).get("status") or ""
                    )
                    == "alert"
                ]
                if not alert_starts:
                    continue
                start = max(alert_starts)
                # A recovery BEFORE the window proves this episode ended
                # before it began. Its alert row only carries a later
                # ``resolved_ts`` because ``close_all`` stamps every
                # historical open row of the rule.
                if any(
                    start < recovery_ts <= window_start_ts
                    for recovery_ts in outside_recoveries.get(key, [])
                ):
                    continue
                # Another DELIVERED alert between the window and the
                # stamping recovery means that recovery closed a LATER
                # episode: this old row is an orphan that close_all merely
                # stamped (its own recovery event was lost). A "failed" row
                # in that range is a retry of the same outage, not a new one.
                late = self.store.list_events_range(window_end_ts, resolved, limit=5000)
                if any(
                    str(item.get("status") or "") == "alert" and _pair_key(item) == key
                    for item in late
                ):
                    continue
                row = outside_rows.get(key, {}).get(start) or {}
                marker = "（该窗口内持续异常）"
                detail = self.redactor.mask_addresses(str(row.get("detail") or ""))
                detail = (detail[: 80 - len(marker)] + marker) if detail else marker
                covering_rows.append(
                    {
                        "time": datetime.fromtimestamp(
                            window_start_ts, self.tz
                        ).strftime("%H:%M" if span == 1 else "%m-%d %H:%M"),
                        "kind": self._kind_text(str(row.get("kind") or "")),
                        "kind_slug": str(row.get("kind") or ""),
                        "name": self.redactor.mask_name(
                            redact_url(str(row.get("name") or "--"))
                        ),
                        "status": "异常",
                        "status_key": "alert",
                        "detail": detail,
                        "duration": "跨窗口关闭",
                        "duration_code": "cross_window",
                    }
                )
                rendered_keys.add(key)
                count += 1
                break
        if covering_rows:
            # These rows describe the start of the window; appending them
            # after the in-window events made the table read out of order.
            items[:0] = covering_rows
        raw_total = len(items)
        unresolved_total = len(unresolved_items)
        hidden_unresolved = 0
        dropped_in_window = 0
        if unresolved_items:
            # Unresolved rows are capped at half the table: with 20+ stale
            # incidents the old formula left room=0 and the day's real events
            # disappeared completely (v2.20.8). The newest in-window rows keep
            # the remaining slots, and anything hidden is disclosed below.
            cap = 10
            if len(unresolved_items) > cap:
                hidden_unresolved = len(unresolved_items) - cap
                unresolved_items = unresolved_items[:cap]
            room = max(0, 20 - len(unresolved_items))
            # The whole-window rows describe the start of the window and must
            # not be the first thing dropped when the table is truncated.
            covering_ids = {id(item) for item in covering_rows}
            kept_covering = [item for item in items if id(item) in covering_ids]
            rest = [item for item in items if id(item) not in covering_ids]
            head = kept_covering + rest[-max(0, room - len(kept_covering)) :]
            dropped_in_window = max(0, len(items) - len(head))
            items = unresolved_items + head
        return {
            "available": bool(items),
            "empty_reason": (
                f"{day_label}无异常事件" if span == 1 else f"近 {span} 天无异常事件"
            )
            if not items
            else None,
            "count": count,
            # Episodes that started inside the window and never recovered are
            # shown as "进行中" but were not counted here, so the headline said
            # "未恢复 0 项" right above a row that said otherwise.
            "unresolved": unresolved_total + len(episode_open - set(stale.keys())),
            # Count before the display cut: with unresolved rows prepended the
            # old total became 20 and the "另有 N 条未列出" disclosure vanished.
            "total": raw_total + unresolved_total,
            "truncated": dropped_in_window + hidden_unresolved,
            # Rows the per-day query could not read (the day exceeded the row
            # cap), so the count above may be smaller than reality.
            "data_truncated": data_truncated,
            "items": items[-20:],
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
            # The "cannot confirm the online state" episode: the connection
            # state itself is unknown, which is neither a resource nor a
            # service fault. It gets its own label so the report does not
            # show it as another offline outage (v2.20.24).
            "connection": "连接状态",
            "service": "服务",
            "provider": "LLM",
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

    def _qq_section(
        self,
        day: date,
        history_days: int,
        span: int = 1,
        day_label: str = "昨日",
    ) -> dict[str, Any]:
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
                "scope_label": "已脱敏",
                "snapshot_note": None,
                "recv_note": None,
                "charts": {},
                "note": "",
            }
        # Bound the query to the window instead of taking the newest N rows:
        # a report older than ``history_days`` found no snapshots at all and
        # the QQ section claimed "暂无数据" while the rows were in the table
        # (v2.20.20). The window ends one day past ``day`` because the
        # receive delta needs snapshot(D+1).
        snapshots = {
            str(item.get("day")): item
            for item in self.store.list_snapshots(
                history_days + 2,
                since_day=(day - timedelta(days=history_days)).isoformat(),
                until_day=(day + timedelta(days=1)).isoformat(),
            )
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
                delta = safe_int(following["recv_total"]) - safe_int(
                    current["recv_total"]
                )
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
            elif day_text in sent_map:
                sent_values.append(int(sent_map[day_text]))
            else:
                # A day with no row is "no data" (bot down, platform id
                # changed), not "sent 0"; drawing it as zero invented an
                # outage-shaped dip on the chart.
                sent_values.append(None)

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
        recv_gap_text = ""
        try:
            cur_ts = float(yesterday.get("captured_at") or 0)
            next_ts = float(next_day.get("captured_at") or 0)
            if cur_ts and next_ts:
                gap_seconds = abs(next_ts - cur_ts)
                gap_hours = gap_seconds / 3600.0
                # ±15 minutes, not ±1 hour: a snapshot taken 55 minutes after
                # midnight (the bot reconnected at 00:56) moved part of one
                # day's receive count into the previous day and the old
                # tolerance kept it silent (v2.20.5).
                recv_skewed = not (23.75 <= gap_hours <= 24.25)
                if recv_skewed:
                    recv_gap_text = (
                        f"{int(gap_seconds // 3600)} 小时 "
                        f"{int((gap_seconds % 3600) // 60)} 分"
                    )
            # Missing captured_at (old rows) means the gap cannot be judged;
            # defaulting to 24h claimed "the snapshot is on time" without
            # evidence.
        except (TypeError, ValueError):
            recv_skewed = False
            recv_gap_text = ""

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
                # "still accumulating" only when there really are fewer than
                # two snapshots; a missing next-day snapshot is a different
                # (and much more common) reason for an empty value.
                "接收趋势需要至少两份每日快照，当前仍在积累。"
                if yesterday_recv is None and len(snapshots) < 2
                else (
                    "该日或次日缺少快照，无法计算接收差值。"
                    if yesterday_recv is None
                    else (
                        f"{day_label}快照的记录时刻有偏移（实际覆盖 {recv_gap_text}），"
                        "接收差值仅供参考。"
                        if recv_skewed
                        else None
                    )
                )
            ),
            "charts": {
                "friends_groups": friends_groups,
                "members": member_chart,
                "traffic": traffic_chart,
            },
            # The numbers above are a single snapshot + its daily delta, so a
            # weekly report must not label them "最近 7 天".
            "scope_label": (
                f"{day.strftime('%m-%d')} 快照值与当日增量"
                if span > 1
                else f"最近 {history_days} 天"
            ),
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
            server_offset = datetime.now().astimezone().utcoffset()
            tz_offset = datetime.now(self.tz).utcoffset()
            if server_offset == tz_offset:
                # Same offset: grouping by the timestamp text is exact and
                # keeps the primary-key index usable.
                rows = connection.execute(
                    """
                    SELECT substr(timestamp, 1, 10) AS day, SUM(count)
                    FROM platform_stats
                    WHERE (platform_id = 'aiocqhttp' OR platform_type = 'aiocqhttp')
                      AND timestamp >= ? AND timestamp < ?
                    GROUP BY day
                    """,
                    (
                        f"{start_day.isoformat()} 00:00:00",
                        f"{(end_day + timedelta(days=1)).isoformat()} 00:00:00",
                    ),
                ).fetchall()
                mapping = {str(day): int(count or 0) for day, count in rows}
            else:
                # ``platform_stats.timestamp`` is written in the *server's*
                # local time while every other section converts UTC into
                # display.timezone. When the two offsets differ the day
                # boundary was hours off, so re-bucket here instead.
                raw = connection.execute(
                    """
                    SELECT timestamp, count FROM platform_stats
                    WHERE (platform_id = 'aiocqhttp' OR platform_type = 'aiocqhttp')
                      AND timestamp >= ? AND timestamp < ?
                    """,
                    (
                        f"{(start_day - timedelta(days=1)).isoformat()} 00:00:00",
                        f"{(end_day + timedelta(days=2)).isoformat()} 00:00:00",
                    ),
                ).fetchall()
                mapping = {}
                for stamp, count in raw:
                    try:
                        local = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
                    except (TypeError, ValueError):
                        continue
                    key = local.astimezone(self.tz).date().isoformat()
                    if start_day.isoformat() <= key <= end_day.isoformat():
                        mapping[key] = mapping.get(key, 0) + int(count or 0)
            min_row = connection.execute(
                """
                SELECT MIN(timestamp)
                FROM platform_stats
                WHERE (platform_id = 'aiocqhttp' OR platform_type = 'aiocqhttp')
                """
            ).fetchone()
        except Exception:
            return {}, None, False
        finally:
            connection.close()
        min_day = None
        if min_row and min_row[0]:
            # The stored timestamp is the server's local wall clock. Comparing
            # its date text with a display-timezone day marked the first chart
            # column as "no data" whenever the two offsets differed.
            try:
                stamp = datetime.strptime(str(min_row[0])[:19], "%Y-%m-%d %H:%M:%S")
                min_day = stamp.astimezone(self.tz).date().isoformat()
            except (TypeError, ValueError):
                min_day = str(min_row[0])[:10]
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
        # Old rows stored the full URL as the target name when the operator
        # left the name blank, so a query token could reach the report image
        # and the dashboard. Strip it on read as well; new samples are
        # already stored redacted.
        for row in rows:
            raw_name = str(row.get("name") or "")
            if raw_name:
                row["name"] = redact_url(raw_name)
        names = sorted({str(row.get("name") or "") for row in rows if row.get("name")})
        by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            local = datetime.fromtimestamp(int(row["ts"]), self.tz)
            key = (local.strftime("%Y-%m-%d"), str(row.get("name") or ""))
            by_key.setdefault(key, []).append(row)
        window_keys = [(day - timedelta(days=i)).isoformat() for i in range(span)]

        def latency_values(items: list[dict[str, Any]]) -> list[float]:
            # One dirty row (a hand-edited report.db can hold any text in a
            # REAL column) used to raise and take the whole report down.
            # Skip the bad row instead of losing the day's report.
            values: list[float] = []
            for item in items:
                if not item.get("ok") or item.get("latency_ms") is None:
                    continue
                try:
                    value = float(item["latency_ms"])
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(value):
                    continue
                values.append(value)
            return values

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
                    # Old rows were written before URL redaction existed; a
                    # forwarded report must not show their query tokens, and
                    # with the hostname rule on the host itself is hidden too.
                    "name": self.redactor.mask_name(redact_url(name)),
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
                    "name": self.redactor.mask_name(redact_url(name)),
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
        empty_reason_code = None
        empty_reason_vars: dict[str, Any] = {}
        if available:
            empty_reason = None
        elif first_day:
            empty_reason = (
                f"网站延迟从 {first_day} 开始积累，暂无 {day.isoformat()} 数据"
            )
            empty_reason_code = "website_since"
            empty_reason_vars = {"first_day": first_day, "day": day.isoformat()}
        else:
            empty_reason = "暂无网站延迟采样数据"
            empty_reason_code = "website_none"
        history_note = None
        if first_day and first_day > days[0].isoformat():
            history_note = (
                f"网站延迟历史从 {first_day} 开始，{history_days} 天趋势仍在积累。"
            )
        return {
            "available": available,
            "empty_reason": empty_reason,
            "empty_reason_code": empty_reason_code,
            "empty_reason_vars": empty_reason_vars,
            "first_day": first_day,
            "targets": targets,
            "chart": chart,
            "history_note": history_note,
            "note": "延迟为 HTTP 请求耗时，可用率按响应状态码 2xx/3xx 统计；目标复用状态卡的网站测试设置。",
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
        day_label: str = "昨日",
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
                # Same guard as _metric_stats below: a hand-edited or
                # externally written row can carry inf or an absurd finite
                # value, and the template prints it verbatim ("inf%" and a
                # 300-digit peak). No real per-process CPU/MEM sample is
                # anywhere near 1e9.
                raw_cpu = safe_float(row.get("cpu"), 0.0)
                if not math.isfinite(raw_cpu) or abs(raw_cpu) > 1e9:
                    raw_cpu = 0.0
                raw_mem = safe_float(row.get("mem_mb"), 0.0)
                if not math.isfinite(raw_mem) or abs(raw_mem) > 1e9:
                    raw_mem = 0.0
                item = values.setdefault(
                    name,
                    {
                        "name": name,
                        "cpu": 0.0,
                        "mem_mb": 0.0,
                        "samples": 0,
                    },
                )
                item["cpu"] = max(float(item["cpu"]), raw_cpu)
                item["mem_mb"] = max(float(item["mem_mb"]), raw_mem)
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
        empty_reason_code = None
        empty_reason_vars: dict[str, Any] = {}
        if available:
            empty_reason = None
        elif first_day:
            empty_reason = (
                f"进程采样从 {first_day} 开始积累，暂无 {day.isoformat()} 数据"
            )
            empty_reason_code = "process_since"
            empty_reason_vars = {"first_day": first_day, "day": day.isoformat()}
        else:
            empty_reason = "暂无进程采样数据"
            empty_reason_code = "process_none"
        note = (
            f"按进程名聚合{day_label}采样峰值；同一程序的多进程会合并显示。"
            if span == 1
            else f"按进程名聚合近 {span} 天采样峰值；同一程序的多进程会合并显示。"
        )
        return {
            "available": available,
            "empty_reason": empty_reason,
            "empty_reason_code": empty_reason_code,
            "empty_reason_vars": empty_reason_vars,
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
        self,
        day: date,
        history_days: int,
        span: int = 1,
        day_label: str = "昨日",
    ) -> dict[str, Any]:
        chart_days = [day - timedelta(days=i) for i in range(history_days - 1, -1, -1)]
        window_days = [day - timedelta(days=i) for i in range(span - 1, -1, -1)]
        window_set = {item.isoformat() for item in window_days}
        labels = [item.strftime("%m-%d") for item in chart_days]
        rows, min_created, available, truncated = self._provider_rows(
            chart_days[0], chart_days[-1]
        )
        if not available:
            # The database itself is missing/unreadable. Saying "no provider
            # requests in range" made the Token section contradict the LLM
            # quality block on the same page, which correctly reported the
            # missing data_v4.db and sent the operator looking in the wrong
            # place (v2.20.19).
            return {
                "available": False,
                "empty_reason": "未找到或无法读取 AstrBot 数据库（data_v4.db）",
                "empty_reason_code": "reason_no_provider_db",
                "total": 0,
                "total_text": "暂无数据",
                "calls": 0,
                "calls_text": "--",
                "success_text": "--",
                "top_models": [],
                "trend": _line_chart(labels, [], y_formatter=_fmt_compact),
                "note": "Token 口径：token_input_other + token_input_cached + token_output。",
            }
        if not rows:
            return {
                "available": False,
                "empty_reason": "暂无数据（所选时间范围内没有 provider 请求）",
                "empty_reason_code": "reason_no_provider_data",
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
        parsed_min = parse_utc_value(min_created)
        if parsed_min is not None:
            min_day = parsed_min.astimezone(self.tz).strftime("%Y-%m-%d")

        by_day = {
            item.isoformat(): {"tokens": 0, "calls": 0, "completed": 0, "errors": 0}
            for item in chart_days
        }
        by_model: dict[str, int] = {}
        for row in rows:
            created = parse_utc_value(row[0])
            if created is None:
                continue
            local_day = created.astimezone(self.tz).strftime("%Y-%m-%d")
            bucket = by_day.get(local_day)
            if bucket is None:
                continue
            # One dirty token column used to raise ValueError out of
            # build(), which sent the whole day's report down the retry
            # ladder and dropped it after three attempts. Every other
            # numeric read in this file uses safe_* (v2.20.19).
            tokens = sum(safe_int(value) for value in (row[3], row[4], row[5]))
            bucket["tokens"] += tokens
            bucket["calls"] += 1
            status = str(row[1] or "")
            if status == "completed":
                bucket["completed"] += 1
            elif status == "error":
                bucket["errors"] += 1
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
        errors = sum(
            bucket["errors"] for key, bucket in by_day.items() if key in window_set
        )
        # Same denominator as the LLM quality section: cancellations are
        # listed separately and do not count as failures.
        attempts = completed + errors
        success_rate = (completed / attempts * 100.0) if attempts else None
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
                "Token 口径：token_input_other + token_input_cached + token_output；"
                "成功率 = 完成 / (完成 + 失败)，中断（aborted）单列不计入分母。"
                + (
                    " 窗口内请求超过 2 万条，仅统计最近部分，合计可能偏小。"
                    if truncated
                    else ""
                )
            ),
        }

    def _provider_quality_section(
        self, day: date, history_days: int, span: int = 1
    ) -> dict[str, Any]:
        """LLM service quality over the report window (provider_stats)."""

        chart_days = [day - timedelta(days=i) for i in range(history_days - 1, -1, -1)]
        window_days = [day - timedelta(days=i) for i in range(span - 1, -1, -1)]
        window_set = {item.isoformat() for item in window_days}
        labels = [item.strftime("%m-%d") for item in chart_days]

        connection = connect_provider_db(self.db_path)
        if connection is None:
            return {
                "available": False,
                "empty_reason": "未找到 AstrBot 数据库（data_v4.db）",
                "empty_reason_code": "reason_no_provider_db",
                "stats": {},
                "top_models": [],
                "top_conversations": [],
                "trend": None,
                "note": "",
            }
        try:
            start_text = f"{(chart_days[0] - timedelta(days=2)).isoformat()} 00:00:00"
            end_text = f"{(chart_days[-1] + timedelta(days=3)).isoformat()} 00:00:00"
            rows, oldest_kept, truncated = fetch_provider_window(
                connection, start_text, end_text
            )
            recent = fetch_provider_recent(connection, 30)
        except Exception:
            return {
                "available": False,
                "empty_reason": "provider_stats 查询失败",
                "empty_reason_code": "reason_provider_query_failed",
                "stats": {},
                "top_models": [],
                "top_conversations": [],
                "trend": None,
                "note": "",
            }
        finally:
            connection.close()

        if not rows:
            return {
                "available": False,
                "empty_reason": "暂无数据（所选时间范围内没有 LLM 调用记录）",
                "empty_reason_code": "reason_no_llm_calls",
                "stats": {},
                "top_models": [],
                "top_conversations": [],
                "trend": None,
                "note": "",
            }

        try:
            stats = aggregate_provider_rows(rows, window_set, self.tz)
            streak = error_streak(recent)
        except Exception:
            # A malformed row set must cost this section only, never the
            # whole report build.
            logger.exception("LLM 服务质量聚合失败")
            return {
                "available": False,
                "empty_reason": "LLM 服务质量数据异常",
                "empty_reason_code": "reason_llm_data_broken",
                "stats": {},
                "top_models": [],
                "top_conversations": [],
                "trend": None,
                "note": "",
            }

        # The cap keeps the newest rows and drops the oldest ones: days
        # older than the oldest kept row are unknown, so the trend shows a
        # gap there instead of a fake zero.
        min_day = None
        parsed_min = parse_utc_value(oldest_kept)
        if parsed_min is not None:
            min_day = parsed_min.astimezone(self.tz).strftime("%Y-%m-%d")

        trend_calls: list[int | None] = []
        trend_errors: list[int | None] = []
        for item in chart_days:
            day_text = item.isoformat()
            bucket = stats["by_day"].get(day_text) or {"calls": 0, "errors": 0}
            if min_day and day_text < min_day:
                trend_calls.append(None)
                trend_errors.append(None)
            else:
                trend_calls.append(bucket["calls"])
                trend_errors.append(bucket["errors"])
        trend = _line_chart(
            labels,
            [
                {
                    "name": "调用",
                    "color": "#38bdf8",
                    "values": trend_calls,
                },
                {
                    "name": "失败",
                    "color": "#f87171",
                    "values": trend_errors,
                },
            ],
            y_formatter=_fmt_compact,
            height=150,
        )

        top_models = []
        for model in stats["by_model"][:5]:
            top_models.append(
                {
                    "name": model["name"],
                    "calls_text": _fmt_num(model["calls"]),
                    "errors_text": _fmt_num(model["errors"]),
                    "fail_rate_text": (
                        f"{model['fail_rate']:.1f}%"
                        if model["fail_rate"] is not None
                        else "--"
                    ),
                    "avg_latency_text": _fmt_seconds(model["avg_latency"]),
                }
            )
        top_conversations = []
        for conv in stats["by_conversation"][:5]:
            top_conversations.append(
                {
                    "conversation": conv["conversation"],
                    "calls_text": _fmt_num(conv["calls"]),
                    "errors_text": _fmt_num(conv["errors"]),
                }
            )

        note = QUALITY_NOTE_BASE
        if stats.get("aborted_long"):
            note += QUALITY_NOTE_LONG.format(n=stats["aborted_long"])
        if truncated:
            # A space, not "；": the base note already ends with "。" and
            # "。；" read as a doubled punctuation (2026-11-08 audit).
            note += " " + QUALITY_NOTE_TRUNCATED

        return {
            "available": True,
            "empty_reason": None,
            "stats": {
                "calls": stats["total"],
                "calls_text": _fmt_num(stats["total"]),
                "completed_text": _fmt_num(stats["completed"]),
                "errors_text": _fmt_num(stats["errors"]),
                "aborted_text": _fmt_num(stats["aborted"]),
                # Numeric key: the dashboard note appends the long-abort
                # sentence from THIS field ("aborted_long_text" has no
                # consumer -- the text key exists for future text surfaces).
                "aborted_long": int(stats.get("aborted_long") or 0),
                "aborted_long_text": _fmt_num(stats.get("aborted_long") or 0),
                "success_rate_text": (
                    f"{stats['success_rate']:.1f}%"
                    if stats["success_rate"] is not None
                    else "--"
                ),
                "avg_latency_text": _fmt_seconds(stats["avg_latency"]),
                "p95_latency_text": _fmt_seconds(stats["p95_latency"]),
                "avg_ttft_text": _fmt_seconds(stats["avg_ttft"]),
                "p95_ttft_text": _fmt_seconds(stats["p95_ttft"]),
                "streak": streak,
                "streak_text": _fmt_num(streak),
            },
            "top_models": top_models,
            "top_conversations": top_conversations,
            "trend": trend,
            "note": note,
            "note_code": "quality_note",
            "note_truncated": bool(truncated),
        }

    def _provider_rows(
        self, start_day: date, end_day: date
    ) -> tuple[list[tuple[Any, ...]], str | None, bool, bool]:
        """Provider rows in [start_day, end_day], newest-first capped by SQL.

        The second value is the oldest row actually kept; when the cap cuts
        the window the trend must show a gap before that point, not zero.
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
                LIMIT 20001
                """,
                (
                    f"{(start_day - timedelta(days=2)).isoformat()} 00:00:00",
                    f"{(end_day + timedelta(days=3)).isoformat()} 00:00:00",
                ),
            ).fetchall()
        except Exception:
            return [], None, False, False
        finally:
            connection.close()
        # Fetch one extra row to distinguish "exactly at the cap" from
        # "truncated"; the oldest kept row (not the global MIN) is the point
        # before which the trend must show a gap instead of zero.
        truncated = len(rows) > 20000
        rows = rows[:20000]
        oldest_kept = str(rows[-1][0]) if rows else None
        return rows, oldest_kept, True, truncated


def detect_resource_gaps(
    timestamps: list[int], threshold_seconds: int
) -> list[dict[str, int]]:
    """Sampling holes: the plugin or AstrBot itself was down.

    A spacing longer than two sampling intervals (or ten minutes, whichever
    is larger) means no sample could be taken at all, so aggregates for that
    window silently miss real incidents. Surfacing the window keeps an
    otherwise quiet report honest.
    """

    threshold = max(60, int(threshold_seconds))
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
                # Weekly aggregates span the whole window, so the resource
                # line must carry the window label -- "最后一天" used to
                # describe a 7-day average.
                "资源({}): CPU {} / 峰值 {}；RAM {} / 峰值 {}；SWAP {}；"
                "磁盘 {}；负载 {}".format(
                    window_label,
                    (yesterday.get("cpu") or {}).get("avg_text", "--"),
                    (yesterday.get("cpu") or {}).get("peak_text", "--"),
                    (yesterday.get("ram") or {}).get("avg_text", "--"),
                    (yesterday.get("ram") or {}).get("peak_text", "--"),
                    (yesterday.get("swap") or {}).get("avg_text", "--"),
                    (yesterday.get("disk") or {}).get("avg_text", "--"),
                    (yesterday.get("load1") or {}).get("avg_text", "--"),
                )
            )
        else:
            lines.append(f"资源: {resources.get('empty_reason') or '暂无数据'}")
    anomalies = view.get("anomalies") or {}
    if "anomaly" in sections:
        # The count covers the same window as Token/website/process: label it
        # like they do (the weekly text used to read as if it were one day).
        line = f"异常/失败({window_label}): {anomalies.get('count', 0)} 次"
        if anomalies.get("unresolved"):
            line += f"（另有 {anomalies['unresolved']} 项未恢复）"
        if anomalies.get("data_truncated"):
            # The per-day row cap hid the oldest events of that day: the
            # number above is a lower bound, not the whole story (v2.20.24).
            line += (
                "（事件量超出单日统计上限，可能有故障未被统计，故障时长也可能不准确）"
            )
        brief_items = anomalies.get("items") or []
        if brief_items:
            # Without the text image this is the only place the user learns
            # which service failed and when. The name used to be cut at 12
            # characters ("New API 端口 3000" -> "New API 端口 3"), which
            # invented a port that does not exist; the whole message already
            # has a line-boundary 2000-character budget, so no per-name cut
            # is needed (v2.20.24 audit).
            brief = "；".join(
                f"{item.get('time', '')} {item.get('name') or ''!s} "
                f"{item.get('status', '')}"
                for item in brief_items[:3]
            )
            line += f"（{brief}）"
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
            # The offset warning must reach the text version too: it was
            # only shown in the picture, so a group chat received a badly
            # under-estimated "接收" number with no hint at all.
            recv_note = qq.get("recv_note")
            if recv_note:
                lines.append(f"（{recv_note}）")
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
    quality = view.get("provider_quality") or {}
    if "provider" in sections:
        if quality.get("available"):
            stats = quality.get("stats") or {}
            models = quality.get("top_models") or []
            model_note = ""
            if models:
                model_note = "；最忙模型 " + models[0].get("name", "--")
            lines.append(
                "LLM 质量({}): {} 次调用，成功率 {}，平均延迟 {}，TTFT {}{}".format(
                    window_label,
                    stats.get("calls_text", "--"),
                    stats.get("success_rate_text", "--"),
                    stats.get("avg_latency_text", "--"),
                    stats.get("avg_ttft_text", "--"),
                    model_note,
                )
            )
            if stats.get("streak"):
                lines.append(f"LLM 当前连续失败: {stats['streak_text']} 次")
        else:
            lines.append(f"LLM 质量: {quality.get('empty_reason') or '暂无数据'}")
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
    window_line = "窗口: {}（{}）".format(
        view.get("window") or "", view.get("timezone") or ""
    )

    # Cut on a line boundary and ALWAYS keep the window/timezone line: it is
    # the only place that says which day the text covers. A literal
    # ``[:2000]`` sliced mid-character and usually removed this exact line
    # (v2.20.21).
    text = ""
    truncated = False
    for line in lines:
        candidate = f"{text}\n{line}" if text else line
        if len(candidate) + len(window_line) + 8 > 2000:
            truncated = True
            break
        text = candidate
    if truncated:
        text += "\n…（内容已截断）"
    text += "\n" + window_line
    return text
