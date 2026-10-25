from __future__ import annotations

import os
from pathlib import Path

from astrbot_plugin_statuscard.report_store import ReportStore


def test_report_store_roundtrip(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.available is True

    assert store.add_resource(
        1_700_000_000,
        cpu=12.5,
        ram=60.0,
        swap=3.0,
        disk=47.0,
        load1=0.4,
        net_up=1024.0,
        net_down=2048.0,
        disk_read=10.0,
        disk_write=20.0,
    )
    assert store.add_event(
        1_700_000_000,
        "2026-10-19",
        "service",
        "Nginx",
        "connection refused",
        "alert",
        "nginx.service",
    )
    assert store.record_snapshot(
        "2026-10-19", 100, 20, 3000, 50000, True, 1_700_000_000
    )

    resources = store.list_resources(0)
    assert len(resources) == 1
    assert resources[0]["cpu"] == 12.5

    events = store.list_events("2026-10-19")
    assert len(events) == 1
    assert events[0]["status"] == "alert"
    assert store.count_events("2026-10-19") == {"alert": 1}

    snapshot = store.get_snapshot("2026-10-19")
    assert snapshot is not None
    assert snapshot["friends"] == 100
    assert snapshot["complete"] == 1
    assert store.snapshot_complete("2026-10-19") is True

    # An incomplete retry must not overwrite a complete snapshot.
    assert store.record_snapshot(
        "2026-10-19", None, None, None, None, False, 1_700_000_100
    )
    snapshot = store.get_snapshot("2026-10-19")
    assert snapshot is not None and snapshot["friends"] == 100

    stats = store.stats()
    assert stats["resources"] == 1
    assert stats["snapshots"] == 1
    assert stats["events"] == 1
    assert stats["websites"] == 0
    assert stats["processes"] == 0

    if os.name == "posix":
        assert (tmp_path / "report.db").stat().st_mode & 0o777 == 0o600


def test_report_store_prunes_old_rows(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.add_resource(1_000, cpu=1.0)
    assert store.add_event(1_000, "2020-01-01", "resource", "CPU", "too high", "alert")
    assert store.record_snapshot("2020-01-01", 1, 2, 3, 4, True, 1_000)

    store.prune(resource_days=7, event_days=30, now=1_000_000_000)

    assert store.list_resources(0) == []
    assert store.list_events("2020-01-01") == []
    # Daily snapshots are kept for trend comparison.
    assert store.get_snapshot("2020-01-01") is not None


def test_report_store_latest_run(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_report_run("2026-10-20", 100, True, "image")
    store.add_report_run("2026-10-20", 200, False, "send failed")

    latest = store.latest_report_run("2026-10-20")
    assert latest is not None
    assert latest["ts"] == 200
    assert latest["ok"] is False
    assert latest["detail"] == "send failed"
    assert store.latest_report_run("2026-10-19") is None


def test_report_store_website_and_process_samples(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    start = 1_700_000_000
    assert store.add_website_samples(
        start,
        [
            {"name": "Baidu", "ok": True, "latency_ms": 120.5, "status": "200"},
            {"name": "Baidu", "ok": False, "latency_ms": 3000.0, "status": "ERR"},
        ],
    )
    rows = store.list_website_samples(start, start + 1)
    assert len(rows) == 2
    assert rows[0]["name"] == "Baidu"
    assert rows[0]["ok"] == 1
    assert rows[1]["ok"] == 0
    assert store.first_sample_ts("website_samples") == start

    assert store.add_process_samples(
        start,
        "cpu",
        [
            {"rank": 1, "name": "python", "pid": 10, "cpu": 55.5, "mem_mb": 200.0},
            {"rank": 2, "name": "nginx", "pid": 20, "cpu": 10.0, "mem_mb": 50.0},
        ],
    )
    assert store.add_process_samples(
        start,
        "mem",
        [
            {"rank": 1, "name": "python", "pid": 10, "cpu": 55.5, "mem_mb": 200.0},
        ],
    )
    cpu_rows = store.list_process_samples(start, start + 1, "cpu")
    mem_rows = store.list_process_samples(start, start + 1, "mem")
    assert [item["name"] for item in cpu_rows] == ["python", "nginx"]
    assert len(mem_rows) == 1
    assert store.first_sample_ts("process_samples") == start


def test_report_store_prunes_extended_tables(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = 1_000_000_000
    old = now - 100 * 86400
    store.add_website_samples(
        old,
        [{"name": "x", "ok": True, "latency_ms": 1.0, "status": "200"}],
    )
    store.add_process_samples(
        old,
        "cpu",
        [{"rank": 1, "name": "x", "pid": 1, "cpu": 1.0, "mem_mb": 1.0}],
    )

    store.prune(resource_days=7, event_days=30, now=now)

    assert store.list_website_samples(0, now + 1) == []
    assert store.list_process_samples(0, now + 1, "cpu") == []


def test_list_events_range_returns_sorted_window(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(1_700_000_000, "2026-10-19", "service", "A", "d", "alert")
    store.add_event(1_700_100_000, "2026-10-20", "service", "B", "d", "recovery")
    store.add_event(1_700_200_000, "2026-10-21", "service", "C", "d", "alert")

    rows = store.list_events_range(1_700_050_000, 1_700_150_000, limit=100)
    assert [row["name"] for row in rows] == ["B"]


def test_resolve_event_matches_kind_and_target(tmp_path: Path) -> None:
    """Two monitor items sharing a name must not close each other's alert."""

    store = ReportStore(tmp_path / "report.db")
    store.add_event(1000, "2026-10-19", "service", "同名", "a", "alert", "t1")
    store.add_event(2000, "2026-10-19", "service", "同名", "b", "alert", "t2")

    assert store.resolve_event(3000, "同名", 3000, kind="service", target="t1")
    rows = store.list_events("2026-10-19", limit=10)
    resolved = {row["target"]: row["resolved_ts"] for row in rows}
    assert resolved["t1"] == 3000
    assert resolved["t2"] is None

    assert store.resolve_event(4000, "同名", 4000, kind="service", target="t2")
    rows = store.list_events("2026-10-19", limit=10)
    assert {row["target"]: row["resolved_ts"] for row in rows}["t2"] == 4000


def test_list_keeps_newest_rows_when_over_limit(tmp_path: Path) -> None:
    """Over-limit windows must keep the NEWEST samples, oldest first.

    The ASC variant used to return the oldest N rows, so a 30-day export
    silently contained only the first week.
    """

    store = ReportStore(tmp_path / "report.db")
    base = 1_700_000_000
    for i in range(7):
        store.add_resource(
            base + i,
            cpu=float(i),
            ram=1.0,
            swap=1.0,
            disk=1.0,
            load1=0.0,
            net_up=0.0,
            net_down=0.0,
            disk_read=0.0,
            disk_write=0.0,
        )
    rows = store.list_resources(base, limit=5)
    assert [r["ts"] for r in rows] == [base + 2, base + 3, base + 4, base + 5, base + 6]
    assert rows[0]["cpu"] == 2.0

    for i in range(5):
        store.add_event(base + i, "2026-10-19", "service", "Nginx", "d", "alert", "t")
    events = store.list_events_range(base, base + 10, limit=3)
    assert [e["ts"] for e in events] == [base + 2, base + 3, base + 4]
