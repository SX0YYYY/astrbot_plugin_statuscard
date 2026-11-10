"""Regression tests for the v2.20.30 started_ts batch.

The event table's ``ts`` is the PUSH moment. With ``fail_threshold`` > 1 the
first failed probe happens ``(threshold-1)*interval`` earlier, and the recovery
notice has always billed the outage from that first failure (``state["since"]``).
The daily report paired alert/recovery rows by ``ts``, so it billed from the
push: one outage produced "中断时长 00:01:12" in the chat and "00:00:33" in the
report (2026-11-09, online).

v2.20.30 stores the real start in ``started_ts`` and reads it for display and
duration only; episode pairing still keys on ``ts`` so a late push cannot
reshuffle which alert belongs to which recovery.

Every test drives the production ReportStore / ServiceMonitor /
DailyReportBuilder.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import astrbot_plugin_statuscard.monitor as monitor_module
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder

TZ = ZoneInfo("Asia/Shanghai")
DAY = "2026-11-09"
TARGET = "https://h/x"
CFG = {"display": {"timezone": "Asia/Shanghai"}}


def _ts(hour: int, minute: int, second: int = 0) -> int:
    return int(dt.datetime(2026, 11, 9, hour, minute, second, tzinfo=TZ).timestamp())


def _anomalies(store: ReportStore) -> dict:
    view = DailyReportBuilder(store, CFG).build(day=DAY, span_days=1)
    return view.get("anomalies") or {}


class _MemoryState:
    def __init__(self) -> None:
        self.data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def setdefault(self, key, default):
        return self.data.setdefault(key, default)

    @contextmanager
    def section(self, key, default=None):
        section = self.data.setdefault(key, default if default is not None else {})
        if not isinstance(section, dict):
            section = {}
            self.data[key] = section
        yield section

    def save(self) -> None:
        pass


class _ScriptedPlugin:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str, str]] = []
        self.context = None

    async def send_text(self, targets, text, source="unknown"):
        self.sent.append((list(targets), text, source))
        return DeliveryResult(ok=True)


# ---------------------------------------------------------------------------
# Report durations
# ---------------------------------------------------------------------------


def test_report_bills_from_first_failed_probe(tmp_path: Path) -> None:
    """The notice and the report quote the same duration (one outage)."""

    store = ReportStore(tmp_path / "report.db")
    start = _ts(12, 0, 0)  # first failed probe
    pushed = start + 40  # second failure -> push
    recovered = start + 112  # 00:01:52 after the first failure
    store.add_event(
        pushed,
        DAY,
        "service",
        "网关",
        "down",
        "alert",
        TARGET,
        started_ts=start,
    )
    store.add_event(recovered, DAY, "service", "网关", "ok", "recovery", TARGET)
    store.resolve_event(
        recovered, "网关", recovered, kind="service", target=TARGET, close_all=True
    )

    anomalies = _anomalies(store)
    assert anomalies.get("count") == 1
    row = anomalies["items"][0]
    # 112 seconds, not the 72 the push-moment pairing used to report.
    assert row["duration"] == "00:01:52"
    assert row["status_key"] == "recovered"
    assert row["time"] == "12:00"


def test_legacy_rows_still_bill_from_ts(tmp_path: Path) -> None:
    """Rows written by <=2.20.29 carry no start and keep the old behaviour."""

    store = ReportStore(tmp_path / "report.db")
    pushed = _ts(12, 1, 10)
    recovered = _ts(12, 1, 52)
    store.add_event(pushed, DAY, "service", "网关", "down", "alert", TARGET)
    store.add_event(recovered, DAY, "service", "网关", "ok", "recovery", TARGET)
    store.resolve_event(
        recovered, "网关", recovered, kind="service", target=TARGET, close_all=True
    )

    rows = store.list_events(DAY, 5000)
    assert all(row["started_ts"] is None for row in rows)
    anomalies = _anomalies(store)
    row = anomalies["items"][0]
    assert row["duration"] == "00:00:42"
    assert row["time"] == "12:01"


def test_started_ts_does_not_move_the_pairing_key(tmp_path: Path) -> None:
    """One episode's failed+alert rows stay one episode and bill one start.

    ``failed`` (1000) and ``alert`` (1030) share the same first failure (970).
    Pairing keys on ``ts``, so they stay two rows of ONE episode and the count
    is 1; the duration is measured once from ``started_ts``.
    """

    store = ReportStore(tmp_path / "report.db")
    start = _ts(11, 59, 0)
    store.add_event(
        _ts(12, 0, 10),
        DAY,
        "service",
        "网关",
        "down-failed",
        "failed",
        TARGET,
        started_ts=start,
    )
    store.add_event(
        _ts(12, 0, 40),
        DAY,
        "service",
        "网关",
        "down-alert",
        "alert",
        TARGET,
        started_ts=start,
    )
    recovered = _ts(12, 3, 0)
    store.add_event(recovered, DAY, "service", "网关", "ok", "recovery", TARGET)
    store.resolve_event(
        recovered, "网关", recovered, kind="service", target=TARGET, close_all=True
    )

    anomalies = _anomalies(store)
    assert anomalies.get("count") == 1
    assert anomalies["items"][0]["duration"] == "00:04:00"


def test_two_episodes_bill_their_own_start(tmp_path: Path) -> None:
    """Two real outages keep two durations, each from its own first failure."""

    store = ReportStore(tmp_path / "report.db")
    first_alert = _ts(11, 0, 40)
    first_recovery = _ts(11, 5, 0)
    second_alert = _ts(12, 0, 40)
    second_recovery = _ts(12, 5, 0)
    store.add_event(
        first_alert,
        DAY,
        "service",
        "网关",
        "down1",
        "alert",
        TARGET,
        started_ts=_ts(11, 0, 0),
    )
    store.add_event(first_recovery, DAY, "service", "网关", "ok1", "recovery", TARGET)
    store.add_event(
        second_alert,
        DAY,
        "service",
        "网关",
        "down2",
        "alert",
        TARGET,
        started_ts=_ts(12, 0, 0),
    )
    store.add_event(second_recovery, DAY, "service", "网关", "ok2", "recovery", TARGET)

    anomalies = _anomalies(store)
    assert anomalies.get("count") == 2
    durations = sorted(
        item["duration"] for item in anomalies["items"] if item["status"] == "已恢复"
    )
    assert durations == ["00:05:00", "00:05:00"]


def test_cross_midnight_start_is_marked_before_window(tmp_path: Path) -> None:
    """A fault that began at 23:59:30 bills from there and says so."""

    store = ReportStore(tmp_path / "report.db")
    awaited = _ts(0, 0, 5)
    started = int(dt.datetime(2026, 11, 8, 23, 59, 30, tzinfo=TZ).timestamp())
    recovered = _ts(0, 2, 0)
    store.add_event(
        awaited, DAY, "service", "网关", "down", "alert", TARGET, started_ts=started
    )
    store.add_event(recovered, DAY, "service", "网关", "ok", "recovery", TARGET)
    store.resolve_event(
        recovered, "网关", recovered, kind="service", target=TARGET, close_all=True
    )

    anomalies = _anomalies(store)
    row = anomalies["items"][0]
    assert row["duration"] == "00:02:30（窗口前开始）"
    # The start belongs to the previous day, so the column carries the date:
    # a bare "23:59" under a report for the 9th reads as if it happened then.
    assert row["time"] == "11-08 23:59"
    assert anomalies.get("count") == 1


def test_rows_are_ordered_by_visible_start(tmp_path: Path) -> None:
    """A delayed push must not print a later time above an earlier one.

    Y pushes at 11:45 (normal). X's push is delayed to 12:00 (offline retry)
    but its outage began at 11:30. The table is sorted by what the time column
    says, so X comes first.
    """

    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        _ts(11, 44, 0),
        DAY,
        "service",
        "Y",
        "down-Y",
        "alert",
        "https://y/x",
        started_ts=_ts(11, 44, 0),
    )
    store.add_event(
        _ts(12, 0, 40),
        DAY,
        "service",
        "X",
        "down-X",
        "alert",
        "https://x/x",
        started_ts=_ts(11, 30, 0),
    )

    anomalies = _anomalies(store)
    names = [item["name"] for item in anomalies["items"]]
    times = [item["time"] for item in anomalies["items"]]
    assert names == ["X", "Y"]
    assert times == ["11:30", "11:44"]


def test_unresolved_row_uses_started_ts(tmp_path: Path) -> None:
    """The "ongoing since" row shows the first failure, not the push."""

    store = ReportStore(tmp_path / "report.db")
    # The unresolved sweep only covers rows that began BEFORE the window, so
    # this alert is written on 11-08 (its day) with a ts before 11-09 00:00.
    store.add_event(
        int(dt.datetime(2026, 11, 8, 23, 50, 40, tzinfo=TZ).timestamp()),
        "2026-11-08",
        "service",
        "网关",
        "down",
        "alert",
        TARGET,
        started_ts=int(dt.datetime(2026, 11, 8, 23, 45, 30, tzinfo=TZ).timestamp()),
    )

    anomalies = _anomalies(store)
    assert anomalies.get("unresolved") == 1
    row = anomalies["items"][0]
    assert row["status_key"] == "unresolved"
    assert row["time"] == "11-08 23:45"


def test_dirty_started_ts_falls_back_to_ts(tmp_path: Path) -> None:
    """Future or absurdly old starts are ignored, not rendered as nonsense."""

    for bad_start in (_ts(12, 0, 40) + 5000, _ts(12, 0, 40) - 500 * 86400):
        db = tmp_path / f"dirty_{bad_start}.db"
        store = ReportStore(db)
        pushed = _ts(12, 0, 40)
        recovered = _ts(12, 1, 52)
        store.add_event(
            pushed,
            DAY,
            "service",
            "网关",
            "down",
            "alert",
            TARGET,
            started_ts=bad_start,
        )
        store.add_event(recovered, DAY, "service", "网关", "ok", "recovery", TARGET)
        store.resolve_event(
            recovered, "网关", recovered, kind="service", target=TARGET, close_all=True
        )
        row = _anomalies(store)["items"][0]
        assert row["duration"] == "00:01:12"


# ---------------------------------------------------------------------------
# Writer paths
# ---------------------------------------------------------------------------


def test_recovery_rows_carry_no_started_ts(tmp_path: Path, monkeypatch) -> None:
    """``started_ts`` describes an alert; a recovery row must stay NULL."""

    monkeypatch.setattr(
        monitor_module,
        "now_in_timezone",
        lambda _config: dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=TZ),
    )
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(None, _MemoryState(), CFG, store)
    monitor._record_event(
        "网关",
        TARGET,
        "ok",
        "recovery",
        item_key="http:网关:h#kwX",
        started_ts=1794200000,
    )
    rows = store.list_events(DAY, 5000)
    assert [row["started_ts"] for row in rows] == [None]


def test_dirty_start_value_does_not_drop_the_event(tmp_path: Path, monkeypatch) -> None:
    """A corrupted ``since`` must leave the event in place, not abort it.

    The reverse audit found that a bare ``int(started_ts)`` raised inside
    ``_record_event``'s try block, so the INSERT never happened and the whole
    outage disappeared from the report.
    """

    monkeypatch.setattr(
        monitor_module,
        "now_in_timezone",
        lambda _config: dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=TZ),
    )
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(None, _MemoryState(), CFG, store)
    for bad in ("abc", float("inf")):
        monitor._record_event(
            "网关",
            TARGET,
            "down",
            "alert",
            item_key="http:网关:h#kwX",
            started_ts=bad,
        )
    rows = store.list_events(DAY, 5000)
    assert len(rows) == 2
    assert all(row["started_ts"] is None for row in rows)


def test_alert_rows_store_the_given_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        monitor_module,
        "now_in_timezone",
        lambda _config: dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=TZ),
    )
    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(None, _MemoryState(), CFG, store)
    monitor._record_event(
        "网关",
        TARGET,
        "down",
        "alert",
        item_key="http:网关:h#kwX",
        started_ts=1794200000,
    )
    rows = store.list_events(DAY, 5000)
    assert rows[0]["started_ts"] == 1794200000


def test_resolve_upper_bound_still_uses_push_ts(tmp_path: Path) -> None:
    """The recovery closes rows by ``ts``; the new column is not a bound."""

    store = ReportStore(tmp_path / "report.db")
    pushed = _ts(12, 0, 40)
    store.add_event(
        pushed,
        DAY,
        "service",
        "网关",
        "down",
        "alert",
        TARGET,
        started_ts=_ts(11, 59, 30),
    )
    # A recovery timestamp earlier than the push must not close the row: the
    # alert was written after that moment.
    store.resolve_event(
        _ts(12, 0, 0),
        "网关",
        _ts(12, 0, 0),
        kind="service",
        target=TARGET,
        close_all=True,
    )
    assert store.list_events(DAY, 5000)[0]["resolved_ts"] is None

    store.resolve_event(
        pushed, "网关", pushed, kind="service", target=TARGET, close_all=True
    )
    assert store.list_events(DAY, 5000)[0]["resolved_ts"] == pushed


def test_schema_v4_database_gains_started_ts_in_place(tmp_path: Path) -> None:
    """An existing schema-4 file gains the column without losing rows."""

    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
            name TEXT, target TEXT, detail TEXT, status TEXT NOT NULL,
            resolved_ts INTEGER, item_key TEXT
        );
        INSERT INTO events (ts, day, kind, name, target, detail, status, item_key)
        VALUES (1794200000, '2026-11-09', 'service', 'A', 't', 'd', 'alert',
                'http:A:t');
        PRAGMA user_version = 4;
        """
    )
    con.commit()
    con.close()

    store = ReportStore(db)
    assert store.available
    con = sqlite3.connect(db)
    cols = [row[1] for row in con.execute("PRAGMA table_info(events)")]
    rows = con.execute("SELECT ts, item_key, started_ts FROM events").fetchall()
    version = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    assert "started_ts" in cols
    assert rows == [(1794200000, "http:A:t", None)]
    assert version == 5

    again = ReportStore(db)
    assert again.available
    assert [row["ts"] for row in again.list_events(DAY, 5000)] == [1794200000]


# ---------------------------------------------------------------------------
# End to end: the chat notice and the daily report must agree
# ---------------------------------------------------------------------------


def test_notice_and_report_agree_after_a_late_push(tmp_path: Path, monkeypatch) -> None:
    """fail_threshold=2 pushes 30 s late; both outputs bill 00:03:00."""

    fixed = dt.datetime(2026, 11, 9, 12, 0, 0, tzinfo=TZ)
    monkeypatch.setattr(monitor_module, "now_in_timezone", lambda _config: fixed)
    now_box = [float(_ts(12, 0, 0))]
    monkeypatch.setattr(
        monitor_module, "time", SimpleNamespace(time=lambda: now_box[0])
    )

    state = _MemoryState()
    plugin = _ScriptedPlugin()
    store = ReportStore(tmp_path / "report.db")
    cfg = {
        "display": {"timezone": "Asia/Shanghai"},
        "monitors": {
            "enabled": True,
            "fail_threshold": 2,
            "items": [
                {
                    "type": "tcp",
                    "name": "redis",
                    "host": "127.0.0.1",
                    "port": 6399,
                }
            ],
            "targets": ["aiocqhttp:GroupMessage:1"],
        },
    }
    monitor = ServiceMonitor(plugin, state, cfg, store)
    mode = {"ok": False}

    async def fake_probe(_item):
        if mode["ok"]:
            return True, "TCP ok", "127.0.0.1:6399"
        return False, "ConnectionRefusedError: x", "127.0.0.1:6399"

    monitor._probe = fake_probe
    item = monitor.items[0]
    key = monitor._item_key(item)

    async def run() -> None:
        # First failed probe at 12:00:00: the episode starts here.
        await monitor._check_item(item)
        # Second failure 30 s later: the push happens after the start.
        now_box[0] = float(_ts(12, 0, 30))
        await monitor._check_item(item)
        # Recovery 180 s after the first failure.
        mode["ok"] = True
        now_box[0] = float(_ts(12, 3, 0))
        await monitor._check_item(item)

    asyncio.run(run())

    stored = state.data["monitors"]["items"][key]
    assert stored.get("since") == 0
    rows = store.list_events(DAY, 5000)
    alert = next(row for row in rows if row["status"] == "alert")
    assert alert["ts"] == _ts(12, 0, 30)
    assert alert["started_ts"] == _ts(12, 0, 0)

    notice = next(text for _t, text, _s in plugin.sent if "服务恢复" in text)
    assert "中断时长: 00:03:00" in notice

    anomalies = _anomalies(store)
    assert anomalies.get("count") == 1
    assert anomalies["items"][0]["duration"] == "00:03:00"
    assert anomalies["items"][0]["time"] == "12:00"
