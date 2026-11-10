"""Regression tests for the v2.20.28 event-table item_key batch.

The event table used to pair rows by kind+name+target only. Two monitor
items that share all three (different keyword / query / timeout) therefore
closed each other's alerts: one item's recovery stamped the other's still-open
row with its own resolved_ts, and the daily report showed a failing service as
"已恢复" while the real fault disappeared from the count.

Every test drives the production ReportStore + DailyReportBuilder.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import astrbot_plugin_statuscard.monitor as monitor_module
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore

CFG = {"display": {"timezone": "Asia/Shanghai"}}
DAY = "2026-11-08"
TARGET = "https://h/x"
UPGRADE_DAY = "2026-11-09"


def _builder(store: ReportStore) -> DailyReportBuilder:
    return DailyReportBuilder(store, CFG)


def _anomalies(store: ReportStore) -> dict:
    # Pass the fixed DAY explicitly: build(day=None) means "yesterday", which
    # made this file pass only on 2026-11-09 and fail on every later day (the
    # third "hardcoded date + implicit yesterday" regression, after v2.20.21
    # and v2.20.25).
    view = _builder(store).build(day=DAY, span_days=1)
    return view.get("anomalies") or {}


def _write_two_items(store: ReportStore, *, keys: tuple[str | None, str | None]):
    kx, ky = keys
    store.add_event(
        1794100000, DAY, "service", "网关", "downX", "alert", TARGET, item_key=kx
    )
    store.add_event(
        1794100600, DAY, "service", "网关", "downY", "alert", TARGET, item_key=ky
    )
    store.add_event(
        1794101200, DAY, "service", "网关", "ok", "recovery", TARGET, item_key=ky
    )
    store.resolve_event(
        1794101200,
        "网关",
        1794101200,
        kind="service",
        target=TARGET,
        close_all=True,
        item_key=ky,
    )


def test_distinct_item_keys_do_not_close_each_other(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    _write_two_items(store, keys=("http:网关:h#kwX", "http:网关:h#kwY"))
    anomalies = _anomalies(store)
    assert anomalies.get("count") == 2
    assert anomalies.get("unresolved") == 1
    rows = store.list_events(DAY, 5000)
    by_detail = {row["detail"]: row for row in rows if row["status"] == "alert"}
    assert by_detail["downX"]["resolved_ts"] is None
    assert by_detail["downY"]["resolved_ts"] == 1794101200


def test_legacy_rows_keep_the_old_triple_pairing(tmp_path: Path) -> None:
    """Rows written by <=2.20.27 (item_key NULL) must pair exactly as before."""
    store = ReportStore(tmp_path / "report.db")
    _write_two_items(store, keys=(None, None))
    anomalies = _anomalies(store)
    # The historical behaviour: one episode, closed by the recovery.
    assert anomalies.get("count") == 1
    assert anomalies.get("unresolved") == 0


def test_resolve_without_key_keeps_legacy_close_all(tmp_path: Path) -> None:
    """A legacy caller (no item_key) still closes every open row it used to."""
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1794100000, DAY, "service", "A", "down", "alert", TARGET)
    store.add_event(1794100600, DAY, "service", "A", "down", "alert", TARGET)
    store.resolve_event(
        1794101200, "A", 1794101200, kind="service", target=TARGET, close_all=True
    )
    rows = store.list_events(DAY, 5000)
    alerts = [row for row in rows if row["status"] == "alert"]
    assert all(row["resolved_ts"] == 1794101200 for row in alerts)


def test_schema_v3_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """An existing schema-3 file gains the column without losing rows."""
    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
            name TEXT, target TEXT, detail TEXT, status TEXT NOT NULL,
            resolved_ts INTEGER
        );
        INSERT INTO events (ts, day, kind, name, target, detail, status)
        VALUES (1794100000, '2026-11-08', 'service', 'A', 't', 'd', 'alert');
        PRAGMA user_version = 3;
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
    assert "item_key" in cols
    assert "started_ts" in cols
    assert rows == [(1794100000, None, None)]
    assert version == 5

    # Re-opening must not raise and must not touch the rows.
    again = ReportStore(db)
    assert again.available
    assert [row["ts"] for row in again.list_events("2026-11-08", 5000)] == [1794100000]


def test_unresolved_query_keeps_both_items_apart(tmp_path: Path) -> None:
    """The unresolved sweep must not dedupe two items by the visible triple."""
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        1794100000,
        DAY,
        "service",
        "网关",
        "downX",
        "alert",
        TARGET,
        item_key="http:网关:h#kwX",
    )
    store.add_event(
        1794100600,
        DAY,
        "service",
        "网关",
        "downY",
        "alert",
        TARGET,
        item_key="http:网关:h#kwY",
    )
    rows = store.list_unresolved_alerts(1794200000, 1794000000, limit=50)
    assert len(rows) == 2
    assert {row["item_key"] for row in rows} == {
        "http:网关:h#kwX",
        "http:网关:h#kwY",
    }


def test_query_variant_targets_stay_separate(tmp_path: Path) -> None:
    """Different query strings share a redacted target but not the item_key."""
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        1794100000,
        DAY,
        "service",
        "健康检查",
        "x",
        "alert",
        TARGET,
        item_key="http:健康检查:h#q11111111",
    )
    store.add_event(
        1794100600,
        DAY,
        "service",
        "健康检查",
        "y",
        "alert",
        TARGET,
        item_key="http:健康检查:h#q22222222",
    )
    store.add_event(
        1794101200,
        DAY,
        "service",
        "健康检查",
        "ok",
        "recovery",
        TARGET,
        item_key="http:健康检查:h#q22222222",
    )
    store.resolve_event(
        1794101200,
        "健康检查",
        1794101200,
        kind="service",
        target=TARGET,
        close_all=True,
        item_key="http:健康检查:h#q22222222",
    )
    rows = store.list_events(DAY, 5000)
    by_detail = {row["detail"]: row for row in rows if row["status"] == "alert"}
    assert by_detail["x"]["resolved_ts"] is None
    assert by_detail["y"]["resolved_ts"] == 1794101200


def test_upgrade_episode_recovery_closes_legacy_alert_and_pairs(
    tmp_path: Path, monkeypatch
) -> None:
    """A fault open across the upgrade must still be closed and rendered.

    The alert row predates the column (item_key NULL); the recovery is written
    by the new code. ``resolve_event`` must close the NULL row and the recovery
    event must stay legacy-shaped so the report can pair them.
    """
    # 13:53:20 UTC == 21:53:20 Asia/Shanghai on 2026-11-09, one hour after the
    # alert row below. Spelled as an explicit epoch so the test does not depend
    # on the machine's local timezone (CI runs UTC).
    fixed_ts = 1794203600
    fixed = dt.datetime(2026, 11, 9, 21, 53, 20)

    def _fixed_now(config):
        return fixed

    monkeypatch.setattr(monitor_module, "now_in_timezone", _fixed_now)
    # Freeze the clock too: the recovery must be later than the alert row,
    # and a leaked clock from another test must not shift it.
    monkeypatch.setattr(monitor_module, "time", SimpleNamespace(time=lambda: fixed_ts))
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1794200000, UPGRADE_DAY, "service", "网关", "down", "alert", TARGET)
    state = StateStore(tmp_path / "state.json")
    monitor = ServiceMonitor(None, state, {}, store)
    monitor._record_event("网关", TARGET, "ok", "recovery", item_key="http:网关:h#kwX")

    rows = store.list_events(UPGRADE_DAY, 5000)
    alert = next(row for row in rows if row["status"] == "alert")
    recovery = next(row for row in rows if row["status"] == "recovery")
    assert alert["resolved_ts"] is not None
    assert recovery["item_key"] is None

    view = _builder(store).build(day=dt.date(2026, 11, 9), span_days=1)
    anomalies = view.get("anomalies") or {}
    assert anomalies.get("count") == 1
    assert anomalies.get("unresolved") == 0


def test_keyed_recovery_still_closes_its_own_new_alert(tmp_path: Path) -> None:
    """No legacy row -> the recovery is keyed and still closes its own alert."""
    store = ReportStore(tmp_path / "report.db")
    key = "http:网关:h#kwX"
    store.add_event(
        1794100000, DAY, "service", "网关", "down", "alert", TARGET, item_key=key
    )
    assert not store.has_open_legacy_alert("service", "网关", TARGET)
    store.add_event(
        1794100600, DAY, "service", "网关", "ok", "recovery", TARGET, item_key=key
    )
    store.resolve_event(
        1794100600,
        "网关",
        1794100600,
        kind="service",
        target=TARGET,
        close_all=True,
        item_key=key,
    )
    row = next(r for r in store.list_events(DAY, 5000) if r["status"] == "alert")
    assert row["resolved_ts"] == 1794100600


def test_state_key_migration_rekeys_open_event_rows(tmp_path: Path) -> None:
    """Editing a monitor must carry its still-open alert rows to the new key.

    The state slot is re-keyed on config edits (new sibling / duplicate
    removed / probe signature changed). If the open alert rows keep the old
    key, the later recovery resolves under the new key and the old row stays
    "unresolved" for the whole retention window.
    """
    store = ReportStore(tmp_path / "report.db")
    old = "http:网关:https://h/x"
    new = old + "#a1b2c3d4"
    store.add_event(
        1794100000, DAY, "service", "网关", "down", "alert", TARGET, item_key=old
    )
    state = StateStore(tmp_path / "state.json")
    items = state.data.setdefault("monitors", {}).setdefault("items", {})
    items[old] = {
        "name": "网关",
        "target": TARGET,
        "alerting": True,
        "since": 1794100000.0,
    }
    state.save()
    monitor = ServiceMonitor(None, state, {}, store)
    monitor._move_state_key(old, new)

    row = next(r for r in store.list_events(DAY, 5000) if r["status"] == "alert")
    assert row["item_key"] == new

    store.add_event(
        1794100600, DAY, "service", "网关", "ok", "recovery", TARGET, item_key=new
    )
    store.resolve_event(
        1794100600,
        "网关",
        1794100600,
        kind="service",
        target=TARGET,
        close_all=True,
        item_key=new,
    )
    row = next(r for r in store.list_events(DAY, 5000) if r["status"] == "alert")
    assert row["resolved_ts"] == 1794100600
    assert store.list_unresolved_alerts(1794200000, 1794000000) == []


def test_keyed_episode_is_not_treated_as_legacy(tmp_path: Path) -> None:
    """A keyed open row must win over an older NULL orphan.

    Otherwise the recovery is written WITHOUT a key and pairs with the old
    orphan's start, producing a phantom long-duration row next to the real
    episode (two "recovered" lines for one blip).
    """
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1793992800, DAY, "service", "Svc", "old", "alert", "t")
    store.add_event(
        1794100000, DAY, "service", "Svc", "new", "alert", "t", item_key="svc:Svc:t"
    )
    assert not store.has_open_legacy_alert("service", "Svc", "t")

    # No keyed row at all -> the legacy shape is still needed.
    other = ReportStore(tmp_path / "other.db")
    other.add_event(1794100000, DAY, "service", "Svc", "old", "alert", "t")
    assert other.has_open_legacy_alert("service", "Svc", "t")
