"""Read-only provider quality statistics from AstrBot's ``provider_stats``.

The AstrBot core already records one row per LLM agent response in
``data_v4.db`` (``provider_stats``): status, provider/model, conversation,
and the timing columns ``start_time`` / ``end_time`` /
``time_to_first_token``. The framework's own usage panel only shows counts
and tokens; this module adds the quality dimension - success rate, latency,
TTFT, per-model and per-conversation distributions, and a consecutive-error
detector for the alert engine.

Measurement rules (settled 2026-10-28):

- ``status`` is ``completed`` / ``error`` / ``aborted``. ``aborted`` means
  the user stopped the generation (``AgentRunner.was_aborted()``), so it is
  reported separately and never counted as a failure.
- Success rate = ``completed / (completed + error)``.
- Latency = ``end_time - start_time`` in seconds, completed rows only.
- TTFT = ``time_to_first_token`` in seconds; 0 means "not recorded" and is
  excluded from the percentiles.
- The consecutive-error walk skips ``aborted`` rows: a user abort is a
  human action, not a fault signal, so it neither counts nor breaks the
  streak.

All queries run on a ``mode=ro`` SQLite connection (the same access style
the LLM Guard review already accepted) and the callers wrap them in
``asyncio.to_thread``.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROVIDER_DB_NAME = "data_v4.db"


def parse_utc_value(value: Any) -> datetime | None:
    """Parse an AstrBot ``created_at`` cell into an aware UTC datetime.

    The column stores naive UTC text (``YYYY-MM-DD HH:MM:SS[.ffffff]``);
    anything unparseable becomes ``None`` and the caller skips the row.
    """

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


def astrbot_db_path(db_path: Path | str | None = None) -> Path | None:
    """Resolve the AstrBot ``data_v4.db`` path, or ``None`` when unknown."""

    if db_path is not None:
        return Path(db_path)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / PROVIDER_DB_NAME
    except Exception:
        return None


def connect_readonly(db_path: Path | str | None = None) -> sqlite3.Connection | None:
    """Open ``data_v4.db`` read-only; ``None`` when missing or unreadable."""

    path = astrbot_db_path(db_path)
    if path is None or not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
    except Exception:
        return None


def fetch_window(
    connection: sqlite3.Connection,
    start_text: str,
    end_text: str,
    limit: int = 20000,
) -> tuple[list[tuple[Any, ...]], str | None, bool]:
    """Rows in ``[start_text, end_text)`` newest-first, capped by SQL.

    Returns ``(rows, min_created, truncated)``; ``min_created`` is the oldest
    ``created_at`` in the whole table so callers can mark partial history,
    and ``truncated`` flags that the cap cut the oldest part of the window.
    """

    rows = connection.execute(
        """
        SELECT created_at, status, provider_model,
               conversation_id, start_time, end_time, time_to_first_token
        FROM provider_stats
        WHERE created_at >= ? AND created_at < ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (start_text, end_text, int(limit)),
    ).fetchall()
    min_row = connection.execute(
        "SELECT MIN(created_at) FROM provider_stats"
    ).fetchone()
    min_created = str(min_row[0]) if min_row and min_row[0] else None
    truncated = len(rows) >= int(limit)
    return rows, min_created, truncated


def fetch_recent(
    connection: sqlite3.Connection, limit: int = 20
) -> list[tuple[str, str]]:
    """The newest ``limit`` ``(created_at, status)`` pairs, newest first."""

    rows = connection.execute(
        """
        SELECT created_at, status
        FROM provider_stats
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [(str(row[0] or ""), str(row[1] or "")) for row in rows]


def error_streak(recent: list[tuple[str, str]]) -> int:
    """Consecutive ``error`` rows at the head of a newest-first list.

    ``aborted`` rows are skipped without breaking the streak (a user abort
    is not a fault); any other status - ``completed``, unknown values, empty
    rows - ends the walk. An empty list means "no evidence", reported as 0.
    """

    streak = 0
    for _created, status in recent:
        if status == "error":
            streak += 1
        elif status == "aborted":
            continue
        else:
            break
    return streak


def recent_streak(
    db_path: Path | str | None = None, limit: int = 20
) -> tuple[int, str, int]:
    """Consecutive-error streak over the newest rows.

    Returns ``(streak, latest_status, sampled)``; ``latest_status`` is the
    status of the newest row (``""`` when the database is missing or the
    table is empty) so the alert engine can tell "recovered" apart from
    "no data".
    """

    connection = connect_readonly(db_path)
    if connection is None:
        return 0, "", 0
    try:
        recent = fetch_recent(connection, limit)
    except Exception:
        return 0, "", 0
    finally:
        connection.close()
    latest = recent[0][1] if recent else ""
    return error_streak(recent), latest, len(recent)


def watch_window_text(
    config: Any, days: int = 1, today: Any = None
) -> tuple[str, str, set[str], Any]:
    """SQL window plus local day-keys for a quality report window.

    Returns ``(start_text, end_text, window_keys, tz)`` for the last
    ``days`` local days ending ``today`` (default: today in
    ``display.timezone``). The SQL window is widened by two days on each
    side so the UTC string comparison cannot clip local-day boundaries;
    ``window_keys`` narrows the aggregates back to the local window.
    """

    from .collectors import get_timezone, now_in_timezone

    tz = get_timezone(config)
    today = today or now_in_timezone(config).date()
    days = max(1, int(days))
    start_day = today - timedelta(days=days - 1)
    keys = {(start_day + timedelta(days=offset)).isoformat() for offset in range(days)}
    start_text = f"{(start_day - timedelta(days=2)).isoformat()} 00:00:00"
    end_text = f"{(today + timedelta(days=3)).isoformat()} 00:00:00"
    return start_text, end_text, keys, tz


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of an ascending list; ``None`` when empty."""

    if not sorted_values:
        return None
    index = max(
        0, min(len(sorted_values) - 1, math.ceil(fraction * len(sorted_values)) - 1)
    )
    return sorted_values[index]


def aggregate(
    rows: list[tuple[Any, ...]],
    window_keys: set[str] | None = None,
    tz: Any = UTC,
) -> dict[str, Any]:
    """Aggregate provider rows into quality figures.

    ``rows`` come from :func:`fetch_window` (newest-first, columns in
    ``created_at, status, provider_model, conversation_id, start_time,
    end_time, time_to_first_token`` order). ``window_keys`` is a set of
    local ``YYYY-MM-DD`` day strings that limits the headline figures and
    the per-model / per-conversation tables to the requested window;
    ``None`` keeps every row. The per-day trend always covers every row so
    the caller can draw a longer history. ``tz`` converts ``created_at``
    (UTC) into local days.
    """

    total = 0
    completed = errors = aborted = other = 0
    latencies: list[float] = []
    ttfts: list[float] = []
    by_model: dict[str, dict[str, float]] = {}
    by_conversation: dict[str, dict[str, float]] = {}
    by_day: dict[str, dict[str, int]] = {}

    for row in rows:
        created = parse_utc_value(row[0])
        if created is None:
            continue
        status = str(row[1] or "")

        try:
            start_ts = float(row[4] or 0.0)
            end_ts = float(row[5] or 0.0)
            ttft = float(row[6] or 0.0)
        except (TypeError, ValueError):
            start_ts = end_ts = ttft = 0.0
        latency = end_ts - start_ts if end_ts > start_ts > 0 else 0.0

        local_day = created.astimezone(tz).strftime("%Y-%m-%d")
        day_bucket = by_day.setdefault(local_day, {"calls": 0, "errors": 0})
        day_bucket["calls"] += 1
        if status == "error":
            day_bucket["errors"] += 1

        # The per-day trend above covers every fetched row so a longer
        # chart window can show history; everything below only counts the
        # requested window, keeping the headline figures and the tables on
        # the same scope.
        if window_keys is not None and local_day not in window_keys:
            continue

        total += 1
        if status == "completed":
            completed += 1
        elif status == "error":
            errors += 1
        elif status == "aborted":
            aborted += 1
        else:
            other += 1

        model = str(row[2] or "未知模型")
        model_stats = by_model.setdefault(
            model, {"calls": 0, "errors": 0, "latency_sum": 0.0, "latency_n": 0}
        )
        model_stats["calls"] += 1
        if status == "error":
            model_stats["errors"] += 1
        if status == "completed" and latency > 0:
            model_stats["latency_sum"] += latency
            model_stats["latency_n"] += 1
            latencies.append(latency)
        if status == "completed" and ttft > 0:
            ttfts.append(ttft)

        conv = str(row[3] or "").strip()
        conv_key = conv[:8] if conv else "未知会话"
        conv_stats = by_conversation.setdefault(conv_key, {"calls": 0, "errors": 0})
        conv_stats["calls"] += 1
        if status == "error":
            conv_stats["errors"] += 1

    attempts = completed + errors
    success_rate = (completed / attempts * 100.0) if attempts else None
    latencies.sort()
    ttfts.sort()
    avg_latency = sum(latencies) / len(latencies) if latencies else None
    avg_ttft = sum(ttfts) / len(ttfts) if ttfts else None

    models_out: list[dict[str, Any]] = []
    for model, stats in sorted(
        by_model.items(), key=lambda pair: pair[1]["calls"], reverse=True
    ):
        models_out.append(
            {
                "name": model,
                "calls": int(stats["calls"]),
                "errors": int(stats["errors"]),
                "fail_rate": (
                    stats["errors"] / stats["calls"] * 100.0 if stats["calls"] else None
                ),
                "avg_latency": (
                    stats["latency_sum"] / stats["latency_n"]
                    if stats["latency_n"]
                    else None
                ),
            }
        )

    conversations_out: list[dict[str, Any]] = []
    for conv, stats in sorted(
        by_conversation.items(), key=lambda pair: pair[1]["calls"], reverse=True
    ):
        conversations_out.append(
            {
                "conversation": conv,
                "calls": int(stats["calls"]),
                "errors": int(stats["errors"]),
            }
        )

    return {
        "total": total,
        "completed": completed,
        "errors": errors,
        "aborted": aborted,
        "other": other,
        "success_rate": success_rate,
        "avg_latency": avg_latency,
        "p95_latency": _percentile(latencies, 0.95),
        "avg_ttft": avg_ttft,
        "p95_ttft": _percentile(ttfts, 0.95),
        "by_model": models_out,
        "by_conversation": conversations_out,
        "by_day": by_day,
    }


def today_overview(config: Any, db_path: Path | str | None = None) -> dict[str, Any]:
    """Small quality snapshot for the dashboard overview card.

    Covers today (``display.timezone``) only. Everything degrades to
    ``available=False`` with a reason when the database is missing or the
    day has no records yet.
    """

    from .collectors import get_timezone, now_in_timezone

    result: dict[str, Any] = {
        "available": False,
        "empty_reason": None,
        "calls": 0,
        "completed": 0,
        "errors": 0,
        "aborted": 0,
        "success_rate": None,
        "success_text": "--",
        "error_streak": 0,
    }
    connection = connect_readonly(db_path)
    if connection is None:
        result["empty_reason"] = "未找到 AstrBot 数据库（data_v4.db）"
        return result
    tz = get_timezone(config)
    try:
        today = now_in_timezone(config).date()
        start_text = f"{(today - timedelta(days=2)).isoformat()} 00:00:00"
        end_text = f"{(today + timedelta(days=2)).isoformat()} 00:00:00"
        rows, _min_created, _truncated = fetch_window(connection, start_text, end_text)
    except Exception:
        result["empty_reason"] = "provider_stats 查询失败"
        return result
    finally:
        connection.close()

    window_rows = [
        row
        for row in rows
        if (parsed := parse_utc_value(row[0])) is not None
        and parsed.astimezone(tz).date() == today
    ]
    if not window_rows:
        result["empty_reason"] = "今天还没有 LLM 调用记录"
        return result

    stats = aggregate(window_rows, None, tz)
    streak = error_streak(
        [(str(row[0] or ""), str(row[1] or "")) for row in window_rows]
    )
    result.update(
        {
            "available": True,
            "empty_reason": None,
            "calls": stats["total"],
            "completed": stats["completed"],
            "errors": stats["errors"],
            "aborted": stats["aborted"],
            "success_rate": stats["success_rate"],
            "success_text": (
                f"{stats['success_rate']:.1f}%"
                if stats["success_rate"] is not None
                else "--"
            ),
            "error_streak": streak,
        }
    )
    return result
