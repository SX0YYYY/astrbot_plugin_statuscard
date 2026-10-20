from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import (
    DailyReportBuilder,
    build_report_text,
)
from astrbot_plugin_statuscard.t2i_renderer import build_report_html

TZ = ZoneInfo("Asia/Shanghai")
CONFIG = {
    "display": {"timezone": "Asia/Shanghai"},
    "monitors": {"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
}


def _builder(tmp_path: Path, store: ReportStore, db_path: Path | None = None):
    return DailyReportBuilder(store, CONFIG, db_path=db_path)


def _stats_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE platform_stats (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME NOT NULL,
            platform_id VARCHAR NOT NULL,
            platform_type VARCHAR NOT NULL,
            count INTEGER NOT NULL
        );
        CREATE TABLE provider_stats (
            created_at DATETIME NOT NULL,
            status VARCHAR NOT NULL,
            provider_model VARCHAR,
            token_input_other INTEGER NOT NULL,
            token_input_cached INTEGER NOT NULL,
            token_output INTEGER NOT NULL
        );
        """
    )
    return con


def test_resource_section_aggregates_yesterday(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
    previous = day - timedelta(days=1)
    for hour in range(24):
        store.add_resource(
            int((day + timedelta(hours=hour)).timestamp()),
            cpu=10 + hour,
            ram=50,
            swap=5,
            disk=40,
            load1=1.0,
        )
        store.add_resource(
            int((previous + timedelta(hours=hour)).timestamp()),
            cpu=20 + hour,
            ram=60,
            swap=10,
            disk=40,
            load1=2.0,
        )

    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7, sections=["resource"]
    )
    resource = view["resources"]

    assert resource["available"] is True
    assert resource["yesterday"]["cpu"]["avg"] == pytest.approx(21.5)
    assert resource["yesterday"]["cpu"]["peak"] == 33
    assert resource["yesterday"]["ram"]["avg"] == 50
    assert resource["yesterday"]["cpu"]["delta_text"].startswith("-")
    assert resource["charts"]["hourly"]["series"]
    assert len(resource["charts"]["daily"]["x_labels"]) <= 5
    assert "resource" in view["sections"]


def test_qq_and_token_sections_use_astrbot_history(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.record_snapshot("2026-10-19", 10, 2, 100, 1000, True, 1)
    store.record_snapshot("2026-10-20", 12, 3, 120, 1050, True, 2)

    db = tmp_path / "data_v4.db"
    con = _stats_db(db)
    con.executemany(
        "INSERT INTO platform_stats (timestamp, platform_id, platform_type, count) "
        "VALUES (?, ?, ?, ?)",
        [
            ("2026-10-19 10:00:00", "aiocqhttp", "unknown", 3),
            ("2026-10-20 10:00:00", "aiocqhttp", "unknown", 5),
            ("2026-10-20 11:00:00", "aiocqhttp", "unknown", 7),
            ("2026-10-20 10:00:00", "webchat", "unknown", 100),
        ],
    )
    con.executemany(
        "INSERT INTO provider_stats "
        "(created_at, status, provider_model, token_input_other, "
        " token_input_cached, token_output) VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2026-10-19 02:00:00.000000", "completed", "kimi-k3", 1, 2, 3),
            ("2026-10-20 02:00:00.000000", "completed", "kimi-k3", 100, 200, 300),
            ("2026-10-20 15:00:00.000000", "error", "other", 10, 20, 30),
        ],
    )
    con.commit()
    con.close()

    view = _builder(tmp_path, store, db_path=db).build(
        day="2026-10-20", history_days=7, sections=["qq", "token"]
    )

    qq = view["qq"]
    assert qq["yesterday"]["friends"] == 12
    assert qq["yesterday"]["groups"] == 3
    assert qq["yesterday"]["members"] == 120
    assert qq["yesterday"]["sent"] == 12
    assert qq["yesterday"]["recv"] == 50
    assert qq["yesterday"]["sent_delta_text"] == "+300.0%"
    assert qq["charts"]["traffic"]["series"]

    token = view["token"]
    assert token["total"] == 666
    assert token["total_text"] == "666"
    assert token["calls"] == 3
    assert token["success_text"] == "66.7%"
    assert token["top_models"][0]["name"] == "kimi-k3"
    assert token["top_models"][0]["tokens"] == 606
    assert token["trend"]["series"]


def test_anomaly_pairs_alert_and_recovery(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        1_700_000_000, "2026-10-19", "service", "Nginx", "down", "alert"
    )
    store.add_event(
        1_700_000_600, "2026-10-20", "service", "Nginx", "up", "recovery"
    )
    store.add_event(
        1_700_000_700, "2026-10-20", "resource", "CPU 使用率", "too high", "alert"
    )
    store.add_event(
        1_700_000_800, "2026-10-20", "resource", "磁盘使用率", "send failed", "failed"
    )

    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7, sections=["anomaly"]
    )
    anomalies = view["anomalies"]

    assert anomalies["count"] == 2
    assert anomalies["normal"]["monitors"] == 3
    assert anomalies["normal"]["resource_rules"] == 5
    recovery = next(
        item for item in anomalies["items"] if item["status_key"] == "recovery"
    )
    assert recovery["duration"] == "00:10:00"


def test_empty_report_shows_accumulating_messages(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7
    )

    assert view["resources"]["available"] is False
    assert "暂无" in view["resources"]["empty_reason"]
    assert view["qq"]["available"] is False
    assert view["token"]["available"] is False
    assert view["anomalies"]["items"] == []
    assert view["summary"]["sent_text"] == "--"
    assert view["summary"]["token_total_text"] == "暂无数据"


def test_report_html_is_self_contained_and_svg_only(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
    for hour in range(24):
        store.add_resource(
            int((day + timedelta(hours=hour)).timestamp()),
            cpu=20 + hour,
            ram=50,
            swap=5,
            disk=40,
            load1=1.0,
        )
    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7, sections=["resource"]
    )
    html = build_report_html(view, render_width=650)

    assert "系统日报" in html
    assert "<svg" in html
    assert "<polyline" in html
    assert "<script" not in html
    assert "width=650" in html


def _sample_report_view() -> dict:
    return {
        "title": "系统日报",
        "day": "2026-10-20",
        "window": "2026-10-20 00:00–24:00",
        "timezone": "Asia/Shanghai",
        "sections": ["resource", "anomaly", "qq", "token"],
        "resources": {
            "available": True,
            "yesterday": {
                "cpu": {"avg_text": "3.5%", "peak_text": "86.0%"},
                "ram": {"avg_text": "57.4%", "peak_text": "63.3%"},
            },
        },
        "anomalies": {"count": 5},
        "qq": {
            "yesterday": {
                "friends_text": "22",
                "groups_text": "10",
                "members_text": "2,345",
                "sent_text": "96",
                "recv_text": "--",
            }
        },
        "token": {
            "available": True,
            "total_text": "9,548,645",
            "calls_text": "316",
            "success_text": "92.7%",
        },
    }


def _report_config() -> dict:
    return {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {"platform_id": "default", "targets": ["123456"]},
        "daily_report": {
            "enabled": True,
            "time": "21:00",
            "targets": [],
            "sections": ["qq"],
            "with_image": True,
            "history_days": 14,
        },
    }


def test_resource_empty_message_mentions_start_day(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    store.add_resource(1_700_000_000, cpu=1.0)
    view = _builder(tmp_path, store).build(
        day="2026-10-19", history_days=7, sections=["resource"]
    )
    assert "2026-10-19" in view["resources"]["empty_reason"]


def test_report_text_contains_key_figures() -> None:
    text = build_report_text(_sample_report_view())
    assert "系统日报 2026-10-20" in text
    assert "CPU 3.5%" in text
    assert "异常/失败: 5 次" in text
    assert "好友 22" in text
    assert "接收 数据积累中" in text
    assert "Token: 9,548,645" in text


def test_report_hostname_respects_redact(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    config = {
        "display": {"timezone": "Asia/Shanghai", "redact": ["hostname"]},
    }
    view = DailyReportBuilder(store, config).build(
        day="2026-10-20", sections=["resource"], meta={"host": "secret-host"}
    )
    assert view["host"] == "已隐藏"


class _FakePlugin:
    def __init__(self) -> None:
        self.context = None
        self.image_ok = False
        self.text_ok = True
        self.images: list[tuple[list[str], str]] = []
        self.texts: list[tuple[list[str], str]] = []

    async def render_report_image(self, view: dict) -> str:
        return "https://example.invalid/report.jpg"

    async def send_image(self, targets: list[str], url: str) -> bool:
        self.images.append((targets, url))
        return self.image_ok

    async def send_text(self, targets: list[str], text: str) -> bool:
        self.texts.append((targets, text))
        return self.text_ok


def test_daily_reporter_targets_sections_and_text_fallback(
    tmp_path: Path,
) -> None:
    store = ReportStore(tmp_path / "report.db")
    plugin = _FakePlugin()
    reporter = DailyReporter(plugin, store, _report_config())
    assert reporter._targets() == ["default:GroupMessage:123456"]
    assert reporter._sections() == ["qq"]
    assert reporter._history_days() == 14
    assert reporter._time_text() == "21:00"

    reporter._build_view = lambda day=None: _sample_report_view()
    asyncio.run(reporter._send_scheduled())

    assert plugin.images and plugin.images[0][0] == [
        "default:GroupMessage:123456"
    ]
    assert plugin.texts
    latest = store.latest_report_run("2026-10-20")
    assert latest is not None
    assert latest["ok"] is True
    assert latest["detail"] == "text"

    reporter._last_sent_day = None
    plugin.image_ok = True
    plugin.texts.clear()
    asyncio.run(reporter._send_scheduled())
    latest = store.latest_report_run("2026-10-20")
    assert latest is not None
    assert latest["detail"] == "image"


def test_daily_reporter_defaults_for_old_config(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    reporter = DailyReporter(_FakePlugin(), store, {})

    assert reporter._enabled() is False
    assert reporter._sections() == ["resource", "anomaly", "qq", "token"]
    assert reporter._targets() == []
    assert reporter._history_days() == 7
    assert reporter._time_text() == "21:00"


def test_website_section_aggregates_latency(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
    for hour in (0, 6, 12, 18):
        ts = int((day + timedelta(hours=hour)).timestamp())
        store.add_website_samples(
            ts,
            [
                {
                    "name": "Baidu",
                    "ok": True,
                    "latency_ms": 100 + hour,
                    "status": "200",
                },
                {
                    "name": "API",
                    "ok": hour != 12,
                    "latency_ms": 200 + hour,
                    "status": "ERR" if hour == 12 else "200",
                },
            ],
        )

    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7, sections=["website"]
    )
    website = view["website"]
    assert website["available"] is True
    baidu = next(item for item in website["targets"] if item["name"] == "Baidu")
    assert baidu["avg_text"] == "109 ms"
    assert baidu["peak_text"] == "118 ms"
    assert baidu["availability_text"] == "100%"
    api = next(item for item in website["targets"] if item["name"] == "API")
    assert api["availability_text"] == "75%"
    assert website["chart"]["series"]


def test_process_section_tops_and_redact(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
    for hour, cpu, mem in ((0, 50.0, 100.0), (1, 80.0, 120.0)):
        ts = int((day + timedelta(hours=hour)).timestamp())
        store.add_process_samples(
            ts,
            "cpu",
            [
                {
                    "rank": 1,
                    "name": "python",
                    "pid": 1,
                    "cpu": cpu,
                    "mem_mb": mem,
                },
                {
                    "rank": 2,
                    "name": "nginx",
                    "pid": 2,
                    "cpu": 10.0,
                    "mem_mb": 30.0,
                },
            ],
        )
        store.add_process_samples(
            ts,
            "mem",
            [
                {
                    "rank": 1,
                    "name": "python",
                    "pid": 1,
                    "cpu": cpu,
                    "mem_mb": mem,
                },
            ],
        )

    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7, sections=["process"]
    )
    process = view["process"]
    assert process["available"] is True
    assert process["cpu"][0]["name"] == "python"
    assert process["cpu"][0]["cpu_text"] == "80.0%"
    assert process["mem"][0]["mem_text"] == "120 MB"

    config = {
        "display": {"timezone": "Asia/Shanghai", "redact": ["process"]},
    }
    redacted = DailyReportBuilder(store, config).build(
        day="2026-10-20", history_days=7, sections=["process"]
    )
    assert redacted["process"]["cpu"][0]["name"] == "已隐藏"


def test_report_html_renders_new_sections(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    day = datetime(2026, 10, 20, 0, 0, tzinfo=TZ)
    start = int(day.timestamp())
    store.add_website_samples(
        start,
        [{"name": "Baidu", "ok": True, "latency_ms": 120.0, "status": "200"}],
    )
    store.add_process_samples(
        start,
        "cpu",
        [{"rank": 1, "name": "python", "pid": 1, "cpu": 50.0, "mem_mb": 100.0}],
    )
    store.add_process_samples(
        start,
        "mem",
        [{"rank": 1, "name": "python", "pid": 1, "cpu": 50.0, "mem_mb": 100.0}],
    )

    view = _builder(tmp_path, store).build(
        day="2026-10-20",
        history_days=7,
        sections=["website", "process"],
    )
    html = build_report_html(view, render_width=650)

    assert "网站延迟" in html
    assert "Top 异常进程" in html
    assert "<script" not in html


def test_report_html_renders_dark_theme(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    view = _builder(tmp_path, store).build(
        day="2026-10-20", history_days=7, sections=[]
    )
    html = build_report_html(view, render_width=650, theme="dark")

    assert 'data-theme="dark"' in html
    assert '[data-theme="dark"] body' in html
    assert "<script" not in html


def test_report_text_contains_new_sections() -> None:
    view = {
        "title": "系统日报",
        "day": "2026-10-20",
        "window": "2026-10-20 00:00–24:00",
        "timezone": "Asia/Shanghai",
        "sections": ["website", "process"],
        "website": {
            "available": True,
            "targets": [
                {
                    "name": "Baidu",
                    "avg_text": "120 ms",
                    "availability_text": "100%",
                }
            ],
        },
        "process": {
            "available": True,
            "cpu": [{"name": "python", "cpu_text": "50.0%"}],
        },
    }
    text = build_report_text(view)
    assert "网站: Baidu 120 ms（可用率 100%）" in text
    assert "Top 进程: python CPU 50.0%" in text
