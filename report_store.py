from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("astrbot_plugin_statuscard")

SCHEMA_VERSION = 2


class ReportStore:
    """Small SQLite store for daily-report history.

    The store only keeps numeric samples, daily snapshots and alert/service
    events. It never stores message text, user IDs or session identifiers.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.available = False
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            try:
                os.chmod(self.path, 0o600)
            except Exception:
                pass
            self.available = True
            logger.info("日报数据库已就绪: %s", self.path)
        except Exception:
            logger.exception("初始化日报数据库失败，历史数据将不可用")

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_db(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS resource_samples (
                    ts         INTEGER PRIMARY KEY,
                    cpu        REAL,
                    ram        REAL,
                    swap       REAL,
                    disk       REAL,
                    load1      REAL,
                    net_up     REAL,
                    net_down   REAL,
                    disk_read  REAL,
                    disk_write REAL
                );
                CREATE INDEX IF NOT EXISTS idx_resource_ts
                    ON resource_samples(ts);

                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    day         TEXT PRIMARY KEY,
                    friends     INTEGER,
                    groups      INTEGER,
                    members     INTEGER,
                    recv_total  INTEGER,
                    complete    INTEGER NOT NULL DEFAULT 0,
                    captured_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          INTEGER NOT NULL,
                    day         TEXT NOT NULL,
                    kind        TEXT NOT NULL,
                    name        TEXT,
                    target      TEXT,
                    detail      TEXT,
                    status      TEXT NOT NULL,
                    resolved_ts INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_events_day
                    ON events(day);
                CREATE INDEX IF NOT EXISTS idx_events_ts
                    ON events(ts);

                CREATE TABLE IF NOT EXISTS report_runs (
                    id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    day    TEXT NOT NULL,
                    ts     INTEGER NOT NULL,
                    ok     INTEGER NOT NULL,
                    detail TEXT
                );

                CREATE TABLE IF NOT EXISTS website_samples (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         INTEGER NOT NULL,
                    name       TEXT NOT NULL,
                    ok         INTEGER NOT NULL,
                    latency_ms REAL,
                    status     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_website_ts
                    ON website_samples(ts);
                CREATE INDEX IF NOT EXISTS idx_website_name_ts
                    ON website_samples(name, ts);

                CREATE TABLE IF NOT EXISTS process_samples (
                    id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts     INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    rank   INTEGER NOT NULL,
                    name   TEXT NOT NULL,
                    pid    INTEGER,
                    cpu    REAL,
                    mem_mb REAL
                );
                CREATE INDEX IF NOT EXISTS idx_process_ts
                    ON process_samples(ts);
                CREATE INDEX IF NOT EXISTS idx_process_metric_ts
                    ON process_samples(metric, ts);

                DROP TABLE IF EXISTS todos;
                PRAGMA user_version = 3;
                """
            )
            con.commit()
        finally:
            con.close()

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> bool:
        if not self.available:
            return False
        con: sqlite3.Connection | None = None
        with self._lock:
            try:
                con = self._connect()
                con.execute(sql, params)
                con.commit()
                return True
            except Exception:
                if con is not None:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                logger.exception("日报数据库写入失败")
                return False
            finally:
                if con is not None:
                    con.close()

    def _write_many(
        self, sql: str, rows: list[tuple[Any, ...]]
    ) -> bool:
        if not self.available or not rows:
            return False
        con: sqlite3.Connection | None = None
        with self._lock:
            try:
                con = self._connect()
                con.executemany(sql, rows)
                con.commit()
                return True
            except Exception:
                if con is not None:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                logger.exception("日报数据库批量写入失败")
                return False
            finally:
                if con is not None:
                    con.close()

    def _query(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        if not self.available:
            return []
        con: sqlite3.Connection | None = None
        with self._lock:
            try:
                con = self._connect()
                return list(con.execute(sql, params).fetchall())
            except Exception:
                logger.exception("日报数据库查询失败")
                return []
            finally:
                if con is not None:
                    con.close()

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def add_resource(
        self,
        ts: int,
        cpu: float | None = None,
        ram: float | None = None,
        swap: float | None = None,
        disk: float | None = None,
        load1: float | None = None,
        net_up: float | None = None,
        net_down: float | None = None,
        disk_read: float | None = None,
        disk_write: float | None = None,
    ) -> bool:
        return self._write(
            """
            INSERT OR REPLACE INTO resource_samples
                (ts, cpu, ram, swap, disk, load1, net_up, net_down,
                 disk_read, disk_write)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(ts),
                cpu,
                ram,
                swap,
                disk,
                load1,
                net_up,
                net_down,
                disk_read,
                disk_write,
            ),
        )

    def record_snapshot(
        self,
        day: str,
        friends: int | None,
        groups: int | None,
        members: int | None,
        recv_total: int | None,
        complete: bool,
        captured_at: int | None = None,
    ) -> bool:
        return self._write(
            """
            INSERT INTO daily_snapshots
                (day, friends, groups, members, recv_total, complete, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                friends = excluded.friends,
                groups = excluded.groups,
                members = excluded.members,
                recv_total = excluded.recv_total,
                complete = excluded.complete,
                captured_at = excluded.captured_at
            WHERE daily_snapshots.complete = 0
            """,
            (
                day,
                friends,
                groups,
                members,
                recv_total,
                1 if complete else 0,
                captured_at,
            ),
        )

    def add_event(
        self,
        ts: int,
        day: str,
        kind: str,
        name: str,
        detail: str,
        status: str,
        target: str = "",
        resolved_ts: int | None = None,
    ) -> bool:
        return self._write(
            """
            INSERT INTO events
                (ts, day, kind, name, target, detail, status, resolved_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(ts),
                day,
                kind,
                name,
                target,
                detail,
                status,
                resolved_ts,
            ),
        )

    def add_website_samples(
        self, ts: int, samples: list[dict[str, Any]]
    ) -> bool:
        rows = []
        for item in samples:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            latency = item.get("latency_ms")
            try:
                latency = round(float(latency), 1) if latency is not None else None
            except (TypeError, ValueError):
                latency = None
            rows.append(
                (
                    int(ts),
                    name[:120],
                    1 if item.get("ok") else 0,
                    latency,
                    str(item.get("status") or "")[:20],
                )
            )
        return self._write_many(
            """
            INSERT INTO website_samples (ts, name, ok, latency_ms, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    def add_process_samples(
        self, ts: int, metric: str, samples: list[dict[str, Any]]
    ) -> bool:
        rows = []
        for item in samples:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rows.append(
                (
                    int(ts),
                    str(metric),
                    int(item.get("rank") or 0),
                    name[:120],
                    int(item.get("pid") or 0),
                    float(item.get("cpu") or 0.0),
                    float(item.get("mem_mb") or 0.0),
                )
            )
        return self._write_many(
            """
            INSERT INTO process_samples
                (ts, metric, rank, name, pid, cpu, mem_mb)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def add_report_run(
        self, day: str, ts: int, ok: bool, detail: str = ""
    ) -> bool:
        return self._write(
            """
            INSERT INTO report_runs (day, ts, ok, detail)
            VALUES (?, ?, ?, ?)
            """,
            (day, int(ts), 1 if ok else 0, detail[:500]),
        )

    def latest_report_run(self, day: str | None = None) -> dict[str, Any] | None:
        sql = (
            "SELECT day, ts, ok, detail FROM report_runs "
            "ORDER BY ts DESC, id DESC LIMIT 1"
        )
        params: tuple[Any, ...] = ()
        if day is not None:
            sql = (
                "SELECT day, ts, ok, detail FROM report_runs "
                "WHERE day = ? ORDER BY ts DESC, id DESC LIMIT 1"
            )
            params = (day,)
        rows = self._query(sql, params)
        if not rows:
            return None
        keys = ("day", "ts", "ok", "detail")
        result = dict(zip(keys, rows[0]))
        result["ok"] = bool(result["ok"])
        return result

    def list_report_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT day, ts, ok, detail FROM report_runs
            ORDER BY ts DESC, id DESC LIMIT ?
            """,
            (max(1, min(200, limit)),),
        )
        keys = ("day", "ts", "ok", "detail")
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(keys, row))
            item["ok"] = bool(item["ok"])
            out.append(item)
        return out

    def prune(
        self,
        resource_days: int = 30,
        event_days: int = 90,
        now: int | None = None,
    ) -> None:
        now = int(now if now is not None else time.time())
        resource_cutoff = now - max(1, resource_days) * 86400
        event_cutoff = now - max(1, event_days) * 86400
        self._write(
            "DELETE FROM resource_samples WHERE ts < ?",
            (resource_cutoff,),
        )
        self._write(
            "DELETE FROM website_samples WHERE ts < ?",
            (resource_cutoff,),
        )
        self._write(
            "DELETE FROM process_samples WHERE ts < ?",
            (resource_cutoff,),
        )
        self._write(
            "DELETE FROM events WHERE ts < ?",
            (event_cutoff,),
        )
        self._write(
            "DELETE FROM report_runs WHERE ts < ?",
            (event_cutoff,),
        )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def snapshot_complete(self, day: str) -> bool:
        rows = self._query(
            "SELECT complete FROM daily_snapshots WHERE day = ?", (day,)
        )
        return bool(rows and rows[0][0])

    def get_snapshot(self, day: str) -> dict[str, Any] | None:
        rows = self._query(
            """
            SELECT day, friends, groups, members, recv_total, complete, captured_at
            FROM daily_snapshots WHERE day = ?
            """,
            (day,),
        )
        if not rows:
            return None
        keys = (
            "day",
            "friends",
            "groups",
            "members",
            "recv_total",
            "complete",
            "captured_at",
        )
        return dict(zip(keys, rows[0]))

    def list_snapshots(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT day, friends, groups, members, recv_total, complete, captured_at
            FROM daily_snapshots ORDER BY day DESC LIMIT ?
            """,
            (max(1, min(365, limit)),),
        )
        keys = (
            "day",
            "friends",
            "groups",
            "members",
            "recv_total",
            "complete",
            "captured_at",
        )
        return [dict(zip(keys, row)) for row in rows]

    def list_resources(self, since_ts: int, limit: int = 20000) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT ts, cpu, ram, swap, disk, load1, net_up, net_down,
                   disk_read, disk_write
            FROM resource_samples WHERE ts >= ?
            ORDER BY ts ASC LIMIT ?
            """,
            (int(since_ts), max(1, min(100000, limit))),
        )
        keys = (
            "ts",
            "cpu",
            "ram",
            "swap",
            "disk",
            "load1",
            "net_up",
            "net_down",
            "disk_read",
            "disk_write",
        )
        return [dict(zip(keys, row)) for row in rows]

    def list_website_samples(
        self, start_ts: int, end_ts: int, limit: int = 100000
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT ts, name, ok, latency_ms, status
            FROM website_samples
            WHERE ts >= ? AND ts < ?
            ORDER BY ts ASC LIMIT ?
            """,
            (
                int(start_ts),
                int(end_ts),
                max(1, min(200000, limit)),
            ),
        )
        keys = ("ts", "name", "ok", "latency_ms", "status")
        return [dict(zip(keys, row)) for row in rows]

    def list_process_samples(
        self,
        start_ts: int,
        end_ts: int,
        metric: str,
        limit: int = 100000,
    ) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT ts, metric, rank, name, pid, cpu, mem_mb
            FROM process_samples
            WHERE metric = ? AND ts >= ? AND ts < ?
            ORDER BY ts ASC, rank ASC LIMIT ?
            """,
            (
                str(metric),
                int(start_ts),
                int(end_ts),
                max(1, min(200000, limit)),
            ),
        )
        keys = ("ts", "metric", "rank", "name", "pid", "cpu", "mem_mb")
        return [dict(zip(keys, row)) for row in rows]

    def first_sample_ts(self, table: str) -> int | None:
        if table not in {"website_samples", "process_samples"}:
            return None
        rows = self._query(f"SELECT MIN(ts) FROM {table}")
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def list_events(self, day: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT id, ts, day, kind, name, target, detail, status, resolved_ts
            FROM events WHERE day = ? ORDER BY ts ASC LIMIT ?
            """,
            (day, max(1, min(1000, limit))),
        )
        keys = (
            "id",
            "ts",
            "day",
            "kind",
            "name",
            "target",
            "detail",
            "status",
            "resolved_ts",
        )
        return [dict(zip(keys, row)) for row in rows]

    def count_events(self, day: str) -> dict[str, int]:
        rows = self._query(
            """
            SELECT status, COUNT(*) FROM events
            WHERE day = ? GROUP BY status
            """,
            (day,),
        )
        return {str(status): int(count) for status, count in rows}

    def stats(self) -> dict[str, int]:
        if not self.available:
            return {
                "resources": 0,
                "snapshots": 0,
                "events": 0,
                "websites": 0,
                "processes": 0,
            }
        resources = self._query("SELECT COUNT(*) FROM resource_samples")
        snapshots = self._query("SELECT COUNT(*) FROM daily_snapshots")
        events = self._query("SELECT COUNT(*) FROM events")
        websites = self._query("SELECT COUNT(*) FROM website_samples")
        processes = self._query("SELECT COUNT(*) FROM process_samples")
        return {
            "resources": int(resources[0][0]) if resources else 0,
            "snapshots": int(snapshots[0][0]) if snapshots else 0,
            "events": int(events[0][0]) if events else 0,
            "websites": int(websites[0][0]) if websites else 0,
            "processes": int(processes[0][0]) if processes else 0,
        }
