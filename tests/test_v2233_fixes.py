"""v2.20.33: offline/掉线与 LLM 告警的 started_ts（待办②收尾）。

- 掉线：alert/failed/recovery 事件行带 episode 起点（st["since"]，含既有
  「回退一个采样间隔」的补偿）；恢复事件与通知的时长保持原口径。
- LLM：recent_streak 返回首败行 start_time；alert/recovery 事件带真实起点；
  恢复通知新增「本次异常持续」；episode 结束清 alert_since。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore


class _OkPlugin:
    context = None

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown") -> bool:
        self.sent.append(str(text))
        return True


def _engine(tmp_path: Path, plugin: _OkPlugin) -> AlertEngine:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        }
    }
    return AlertEngine(
        plugin, state, config, report_store=ReportStore(tmp_path / "report.db")
    )


# ---------------------------------------------------------------------------
# offline / 掉线
# ---------------------------------------------------------------------------


def _go_offline(engine: AlertEngine, monkeypatch, rounds: int = 2) -> None:
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )

    class ApiNotAvailable(Exception):
        pass

    async def boom(*args, **kwargs):
        # The name matters: a bare RuntimeError is "unknown state", while
        # ApiNotAvailable is a CONFIRMED offline bot (no reverse-WS client).
        raise ApiNotAvailable("no reverse-ws client")

    monkeypatch.setattr(alerts_module, "_call_action", boom)
    for _ in range(rounds):
        asyncio.run(engine._check_offline())


def _go_online(engine: AlertEngine, monkeypatch) -> None:
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )

    async def ok(*args, **kwargs):
        return {"status": "Ack", "retcode": 0}

    monkeypatch.setattr(alerts_module, "_call_action", ok)
    asyncio.run(engine._check_offline())


def test_offline_alert_event_carries_started_ts(monkeypatch, tmp_path: Path) -> None:
    """掉线 alert 事件的 started_ts = episode 起点（含一个采样间隔回退）。"""

    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    _go_offline(engine, monkeypatch, rounds=2)
    rows = engine.report_store.list_events_range(0, 4 * 10**18, limit=50)
    alerts = [r for r in rows if r["status"] == "alert" and r["kind"] == "offline"]
    assert alerts, "offline alert event expected"
    assert alerts[0]["started_ts"] is not None, (
        "offline alert must carry the episode start (backdated one interval)"
    )
    with engine.state.section("alerts") as section:
        # alerts.offline is a FLAT dict: "Bot 连接" is the event name, not a
        # state key.
        st = section.get("offline") or {}
        assert st.get("since") == pytest.approx(alerts[0]["started_ts"], abs=1.0)


def test_offline_recovery_event_carries_started_ts(monkeypatch, tmp_path: Path) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    _go_offline(engine, monkeypatch, rounds=2)
    _go_online(engine, monkeypatch)
    rows = engine.report_store.list_events_range(0, 4 * 10**18, limit=50)
    recov = [r for r in rows if r["status"] == "recovery" and r["kind"] == "offline"]
    assert recov, "offline recovery event expected"
    assert recov[0]["started_ts"] is not None
    with engine.state.section("alerts") as section:
        st = section.get("offline") or {}
        assert not st.get("since"), "the closed episode must clear its start"


# ---------------------------------------------------------------------------
# provider / LLM
# ---------------------------------------------------------------------------


def test_llm_alert_carries_first_error_start(monkeypatch, tmp_path: Path) -> None:
    """LLM alert 事件的 started_ts = 连续失败的第一次调用时刻。"""

    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (3, "error", 20, 1790000300.0),
    )
    asyncio.run(engine._check_provider_quality())
    rows = engine.report_store.list_events_range(0, 4 * 10**18, limit=50)
    alerts = [r for r in rows if r["status"] == "alert" and r["kind"] == "provider"]
    assert alerts, "provider alert event expected"
    assert alerts[0]["started_ts"] == 1790000300.0, (
        "started_ts must be the streak's first failing call, not the poll time"
    )
    with engine.state.section("alerts") as section:
        watch = section.get("provider_watch") or {}
        assert watch.get("alert_since") == 1790000300.0


def test_llm_recovery_reports_duration_and_clears_start(
    monkeypatch, tmp_path: Path
) -> None:
    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (3, "error", 20, 1790000300.0),
    )
    asyncio.run(engine._check_provider_quality())
    # Recover: the newest call succeeded ~5 minutes later.
    real_time = alerts_module.time.time
    monkeypatch.setattr(
        alerts_module.time, "time", lambda: real_time() + 300, raising=False
    )
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (0, "completed", 20, None),
    )
    engine._provider_last_check = 0.0
    asyncio.run(engine._check_provider_quality())
    rows = engine.report_store.list_events_range(0, 4 * 10**18, limit=50)
    recov = [r for r in rows if r["status"] == "recovery" and r["kind"] == "provider"]
    assert recov, "provider recovery event expected"
    assert recov[0]["started_ts"] is not None
    assert any("本次异常持续" in t for t in plugin.sent), (
        "the recovery notice must quote the outage duration now that the"
        " provider watch knows its start"
    )
    with engine.state.section("alerts") as section:
        watch = section.get("provider_watch") or {}
        assert not watch.get("alert_since"), "the closed episode must clear its start"


def test_llm_recovery_clears_start_on_disk(monkeypatch, tmp_path: Path) -> None:
    """审计 #1（P1）：alert_since 清零必须落盘，否则重启后旧起点复活。"""

    import json as _json

    plugin = _OkPlugin()
    engine = _engine(tmp_path, plugin)
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (3, "error", 20, 1790000300.0),
    )
    asyncio.run(engine._check_provider_quality())
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda db_path, limit: (0, "completed", 20, None),
    )
    engine._provider_last_check = 0.0
    asyncio.run(engine._check_provider_quality())
    raw = _json.loads(Path(engine.state.path).read_text(encoding="utf-8"))
    disk_watch = (raw.get("alerts") or {}).get("provider_watch") or {}
    assert not disk_watch.get("alert_since"), (
        "the cleared episode clock must reach the disk (a restart would"
        " otherwise inherit it as the next episode's start)"
    )
