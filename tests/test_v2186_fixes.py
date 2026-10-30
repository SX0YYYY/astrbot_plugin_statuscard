"""v2.18.6：A/B 两批待办（38 项）的回归覆盖。

Each case pins one behaviour that the 2026-10-29 second audit changed.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import collectors
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.sampler import Sampler
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import redact_url


class _NullPlugin:
    context = None

    async def send_text(self, targets, text, source="unknown") -> bool:
        return True

    async def send_image(self, targets, url, source="unknown"):
        return True


# ---------------------------------------------------------------------------
# every configured mount gets its own rule
# ---------------------------------------------------------------------------


def test_disk_rules_cover_every_mount(tmp_path: Path, monkeypatch) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"disk_paths": ["/", "/data"]},
        "alerts": {"enabled": True, "targets": ["1"], "sustain_samples": 1},
    }
    engine = AlertEngine(_NullPlugin(), state, config)

    calls: list[str] = []

    async def fake_eval(key, name, value, threshold, unit, value_text):
        calls.append(key)

    monkeypatch.setattr(engine, "_evaluate", fake_eval)

    async def no_offline():
        return None

    monkeypatch.setattr(engine, "_check_offline", no_offline)
    monkeypatch.setattr(
        alerts_module,
        "disk_usage_bounded",
        lambda paths: [
            collectors.DiskUsage(name=p, used=10, total=100, percent=10.0)
            for p in paths
        ],
    )

    asyncio.run(engine._sample_once())
    disk_calls = [key for key in calls if str(key).startswith("disk")]
    # The first mount keeps the unqualified key so adding a second mount does
    # not look like "old disk recovered, new disk full".
    assert disk_calls == ["disk", "disk:/data"]


# ---------------------------------------------------------------------------
# recovery notices stop after the retry budget
# ---------------------------------------------------------------------------


def test_recovery_retry_has_a_ceiling(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.data["alerts"] = {
        "rules": {
            "cpu": {
                "pending_recovery": "✅ recovered",
                "recovery_retry_count": alerts_module.MAX_RECOVERY_RETRIES,
                "recovery_next_attempt": 0,
            }
        }
    }
    config = {"alerts": {"enabled": True, "targets": ["1"], "sustain_samples": 1}}
    engine = AlertEngine(_NullPlugin(), state, config)

    asyncio.run(engine._evaluate("cpu", "CPU", 1.0, 85.0, "%", "1.0%"))

    rule = state.data["alerts"]["rules"]["cpu"]
    assert "pending_recovery" not in rule


# ---------------------------------------------------------------------------
# a broadcast template with an unknown brace still fills the known tokens
# ---------------------------------------------------------------------------


def test_broadcast_template_keeps_unknown_braces() -> None:
    template = "掉线 {offline_time}，{备注}，时长 {duration}"
    text = template
    for token, value in (
        ("{offline_time}", "10:00"),
        ("{recover_time}", "10:05"),
        ("{duration}", "5 分钟"),
    ):
        text = text.replace(token, value)
    assert text == "掉线 10:00，{备注}，时长 5 分钟"


# ---------------------------------------------------------------------------
# a restart must not book the downtime as current traffic
# ---------------------------------------------------------------------------


def test_network_sample_skips_history_after_downtime(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.data["net"] = {
        "last_ts": time.time() - 7200,
        "last_sent": 1000,
        "last_recv": 2000,
        "month_up": 1000,
        "month_down": 2000,
        "month": time.strftime("%Y-%m"),
        "history": [],
    }
    sampler = Sampler(state=state, config={"display": {"timezone": "Asia/Shanghai"}})

    fake = type("C", (), {"bytes_sent": 5000, "bytes_recv": 9000})()
    sampler._pick = lambda: ("eth0", fake)  # type: ignore[attr-defined]

    sampler._sample_network(time.time())

    net = state.data["net"]
    assert net["month_up"] == 1000  # the downtime delta is not credited
    assert net["history"] == []  # and no bogus rate point is drawn


# ---------------------------------------------------------------------------
# redact_url strips query even without a scheme
# ---------------------------------------------------------------------------


def test_redact_url_without_scheme_strips_query() -> None:
    assert redact_url("example.com/api?token=abc#frag") == "example.com/api"
    assert redact_url("https://u:p@h/x?t=1") == "https://h/x"


# ---------------------------------------------------------------------------
# audit: an invalid schedule time is an error
# ---------------------------------------------------------------------------


def test_audit_flags_invalid_schedule_time() -> None:
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {"enabled": False, "targets": [], "platform_id": "default"},
        "monitors": {"enabled": False, "targets": [], "items": []},
        "daily_report": {
            "enabled": True,
            "weekly_enabled": False,
            "targets": ["1"],
            "time": "25:00",  # 运行时与 audit 都拒绝：小时超范围
        },
    }
    codes = [issue.code for issue in run_audit(config)]
    assert "invalid_schedule_time" in codes


def test_audit_reports_unknown_notify_kind() -> None:
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {"enabled": False, "targets": [], "platform_id": "default"},
        "monitors": {"enabled": False, "targets": [], "items": []},
        "daily_report": {"enabled": False, "weekly_enabled": False, "targets": []},
        "notify": {
            "rules": [
                {"enabled": True, "target": "", "kinds": ["daliy"]},
            ]
        },
    }
    codes = [issue.code for issue in run_audit(config)]
    assert "notify_rule_invalid" in codes


# ---------------------------------------------------------------------------
# report_view: an in-window unresolved episode is counted
# ---------------------------------------------------------------------------


def test_unresolved_counts_in_window_episodes(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.report_view import DailyReportBuilder

    store = ReportStore(tmp_path / "report.db")
    now = int(time.time()) - 86400
    # The report groups by display.timezone, so the event's day must be
    # derived from that zone too: time.localtime() made the test depend on the
    # host zone (it failed on the UTC CI runner once the two dates differed).
    tz = ZoneInfo("Asia/Shanghai")
    day = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
    # One alert inside the report window without a recovery.
    assert store.add_event(now, day, "resource", "内存使用率", "90%", "alert")

    builder = DailyReportBuilder(
        store,
        {"display": {"timezone": "Asia/Shanghai"}, "daily_report": {}},
    )
    view = builder.build(history_days=7, sections=["anomaly"])
    anomalies = view.get("anomalies") or {}
    assert anomalies.get("unresolved") == 1
