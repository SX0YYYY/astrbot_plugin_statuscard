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
    assert stats == {"resources": 1, "snapshots": 1, "events": 1}

    if os.name == "posix":
        assert (tmp_path / "report.db").stat().st_mode & 0o777 == 0o600


def test_report_store_prunes_old_rows(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    assert store.add_resource(1_000, cpu=1.0)
    assert store.add_event(
        1_000, "2020-01-01", "resource", "CPU", "too high", "alert"
    )
    assert store.record_snapshot(
        "2020-01-01", 1, 2, 3, 4, True, 1_000
    )

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
