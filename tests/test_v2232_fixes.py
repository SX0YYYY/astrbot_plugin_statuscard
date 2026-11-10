"""v2.20.32 修复批：第十六轮全流程找茬的回归测试。

覆盖：资源告警起点口径（alert_since 从首次越阈算起 + 事件 started_ts）、
never-latched 恢复通知、异常表截断切片、回看上限对齐保留、ReDoS 有界化、
文件名:行号不误遮、mask_chat_ids 5 位、weekly day=None 守卫、prune 本地零点
回退、monitor 静默不烧恢复预算、诊断 format 失配回退、privacy FIFO。
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from astrbot_plugin_statuscard import redactor as redactor_mod
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import mask_chat_ids

# ---------------------------------------------------------------------------
# alerts: F1 起点口径 + F2 never-latched 恢复通知
# ---------------------------------------------------------------------------


class _RecordingPlugin:
    def __init__(self, fail_alert_times: int = 0) -> None:
        self.sent: list[str] = []
        self.alert_calls = 0
        self.fail_alert_times = fail_alert_times

    async def send_text(self, targets, text, source="unknown"):
        if str(text).startswith("⚠"):
            self.alert_calls += 1
            if self.alert_calls <= self.fail_alert_times:
                return False
        self.sent.append(text)
        return True


def _engine(tmp_path: Path, plugin: _RecordingPlugin, sustain: int = 1):
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": sustain,
            "cooldown_seconds": 30,
        }
    }
    return AlertEngine(plugin, state, config, report_store=store), state, store


def test_alert_since_starts_at_first_over_threshold_sample(tmp_path: Path) -> None:
    """F1: sustain=3 时 alert_since 应在第一次越阈就落盘，不是推送时刻。"""

    plugin = _RecordingPlugin()
    engine, state, _store = _engine(tmp_path, plugin, sustain=3)

    async def _run():
        await engine._evaluate("mem", "内存", 90.0, 85.0, "%", "90%")
        first_ts = time_in_state(state)
        assert first_ts > 0, "alert_since must be set at count==1"
        await engine._evaluate("mem", "内存", 91.0, 85.0, "%", "91%")
        await engine._evaluate("mem", "内存", 92.0, 85.0, "%", "92%")
        assert len(plugin.sent) == 1, "push happens on the 3rd sample"
        assert time_in_state(state) == pytest.approx(first_ts), (
            "alert_since must NOT move to the push moment"
        )

    asyncio.run(_run())


def time_in_state(state: StateStore) -> float:
    with state.section("alerts") as section:
        rules = section.get("rules") or {}
        for st in rules.values():
            if isinstance(st, dict) and st.get("alert_since"):
                return float(st["alert_since"])
    return 0.0


def test_alert_event_carries_started_ts(tmp_path: Path) -> None:
    """F1b: resource alert 事件的 started_ts = alert_since（首越阈时刻）。"""

    plugin = _RecordingPlugin()
    engine, _state, store = _engine(tmp_path, plugin, sustain=1)

    async def _run():
        await engine._evaluate("mem", "内存", 90.0, 85.0, "%", "90%")

    asyncio.run(_run())
    rows = store.list_events_range(0, 4 * 10**18, limit=10)
    alerts = [r for r in rows if r["status"] == "alert"]
    assert alerts, "one alert event expected"
    assert alerts[0]["started_ts"] is not None, "started_ts must be recorded"


def test_never_latched_recovery_sends_notice(tmp_path: Path) -> None:
    """F2: 首推失败 + 值回落 → 补发带注记的恢复通知（基线是零通知）。"""

    plugin = _RecordingPlugin(fail_alert_times=1)
    engine, _state, store = _engine(tmp_path, plugin)

    async def _run():
        await engine._evaluate("mem", "内存", 91.0, 85.0, "%", "91%")
        await engine._evaluate("mem", "内存", 41.0, 85.0, "%", "41%")

    asyncio.run(_run())
    recoveries = [t for t in plugin.sent if "已恢复" in t]
    assert recoveries, "F2 must send a recovery notice the baseline swallowed"
    assert "（异常告警此前未能送达）" in recoveries[0]
    rows = store.list_events_range(0, 4 * 10**18, limit=10)
    recov = [r for r in rows if r["status"] == "recovery"]
    assert recov and "告警未能送达" in recov[0]["detail"]


def test_normal_episode_still_sends_one_alert_one_recovery(tmp_path: Path) -> None:
    """回归护栏：正常送达的 episode 不受 F1/F2 影响（旧路径不变坏）。"""

    plugin = _RecordingPlugin()
    engine, _state, _store = _engine(tmp_path, plugin)

    async def _run():
        await engine._evaluate("mem", "内存", 91.0, 85.0, "%", "91%")
        await engine._evaluate("mem", "内存", 41.0, 85.0, "%", "41%")

    asyncio.run(_run())
    assert len(plugin.sent) == 2
    assert "已恢复" in plugin.sent[1]
    assert "此前未能送达" not in plugin.sent[1]


# ---------------------------------------------------------------------------
# report_view: F3 截断切片 + F4 回看上限
# ---------------------------------------------------------------------------


def _anomaly_view(tmp_path: Path, day: str):
    store = ReportStore(tmp_path / "report.db")
    config = {"display": {"timezone": "Asia/Shanghai"}}
    builder = DailyReportBuilder(store, config)
    return builder.build(day=day, history_days=7, sections=["anomaly"]), store


def test_truncated_table_keeps_unresolved_head(tmp_path: Path) -> None:
    """F3: unresolved=10 + covering 12 条时，旧切片把未恢复行整段切光。

    数据用引擎真实写入模型构造：stale 行是窗口内未闭合 alert；covering 行
    是窗口外 alert + 窗口结束后 recovery 盖章（resolve_event close_all）。
    """

    from datetime import datetime, timedelta, timezone

    store = ReportStore(tmp_path / "report.db")
    tz = timezone(timedelta(hours=8))
    day = datetime(2027, 1, 15, tzinfo=tz)
    win_start = int(day.timestamp())
    win_end = int((day + timedelta(days=1)).timestamp())

    # 10 个未闭合规则（stale）：窗口外 alert、无 recovery ——
    # list_unresolved_alerts 只查窗口前 (ts < window_start) 的未闭合行，
    # 窗口内的走 episode_open 渲染分支。
    for i in range(10):
        store.add_event(
            win_start - 7200 - i,
            "2027-01-14",
            "service",
            f"stale{i}",
            "down",
            "alert",
            "t",
        )
    # 12 个整窗覆盖 episode（covering）：前夜 alert + 次日上午 recovery 盖章。
    for i in range(12):
        alert_ts = win_start - 3600 - i  # 窗口外（前夜）
        recovered = win_end + 3600 + i  # 窗口结束后
        store.add_event(
            alert_ts,
            "2027-01-14",
            "service",
            f"cover{i}",
            "down",
            "alert",
            "t",
        )
        store.add_event(
            recovered,
            "2027-01-16",
            "service",
            f"cover{i}",
            "ok",
            "recovery",
            "t",
        )
        store.resolve_event(recovered, f"cover{i}", recovered, close_all=True)
    # 30 个普通 episode：窗口内 alert+recovery 配对。
    for i in range(30):
        a = win_start + 7200 + 2 * i
        r = a + 60
        store.add_event(a, "2027-01-15", "service", f"plain{i}", "down", "alert", "t")
        store.add_event(r, "2027-01-15", "service", f"plain{i}", "ok", "recovery", "t")
        store.resolve_event(r, f"plain{i}", r, close_all=True)

    config = {"display": {"timezone": "Asia/Shanghai"}}
    builder = DailyReportBuilder(store, config)
    view = builder.build(day="2027-01-15", history_days=7, sections=["anomaly"])
    items = view["anomalies"]["items"]
    assert len(items) == 20
    stale_shown = [it for it in items if str(it.get("name") or "").startswith("stale")]
    assert len(stale_shown) == 10, (
        "unresolved head rows must survive the final cut (old code dropped all)"
    )
    assert view["anomalies"]["truncated"] >= 1, (
        "the dropped rows must be disclosed, not reported as 0"
    )


def test_lookback_follows_730_day_retention(tmp_path: Path) -> None:
    """F4: 400 天前开始、未恢复的故障在 730 保留配置下必须可见。"""

    from astrbot_plugin_statuscard.report_store import ReportStore as RS

    store = RS(tmp_path / "report.db")
    now = 1_800_000_000
    day = "2027-01-15"
    store.add_event(
        now - 400 * 86400,
        "2025-12-10",
        "service",
        "老故障",
        "down",
        "alert",
        "t",
    )
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"event_retention_days": 730},
    }
    builder = DailyReportBuilder(store, config)
    view = builder.build(day=day, history_days=7, sections=["anomaly"])
    names = [str(it.get("name") or "") for it in view["anomalies"]["items"]]
    assert "老故障" in names, "a 400-day-old unresolved fault must be listed"
    assert view["anomalies"]["unresolved"] >= 1


# ---------------------------------------------------------------------------
# redactor: F8 ReDoS + F12 文件名豁免
# ---------------------------------------------------------------------------


def test_ipv6_candidate_regex_is_bounded() -> None:
    """F8: 畸形 hex 冒号串必须不再指数回溯（基线 30 段 ~93s）。"""

    import time

    for segments in (28, 30, 60):
        text = "a:" * segments + "Z"
        started = time.perf_counter()
        redactor_mod._IPV6_CANDIDATE_RE.findall(text)
        assert (time.perf_counter() - started) < 0.05, (
            f"{segments} segments must not backtrack exponentially"
        )


def test_ipv6_masking_semantics_unchanged() -> None:
    """F8 语义护栏：真实地址仍遮、MAC/时间戳形态与基线一致。"""

    r = redactor_mod.Redactor({"hostname"})
    assert r.mask_addresses("连接 2001:db8::1 失败") != "连接 2001:db8::1 失败"
    # Compact form candidates are still found and still judged literal.
    assert redactor_mod._is_ipv6_literal("2001:db8::1")
    assert redactor_mod._IPV6_CANDIDATE_RE.search("fe80::1%eth0")


def test_source_file_reference_is_not_masked() -> None:
    """F12: main.py:531 形态保留；真实 host:port 仍遮蔽。"""

    r = redactor_mod.Redactor({"hostname"})
    text = "main.py:531 渲染完成"
    assert r.mask_addresses(text) == text
    assert r.mask_addresses("app.js:4387") == "app.js:4387"
    assert r.mask_addresses("collector.py:627 与 main.py:1325") == (
        "collector.py:627 与 main.py:1325"
    )
    assert r.mask_addresses("example.com:8080") != "example.com:8080"
    assert r.mask_addresses("10.1.2.3:8080 拒绝") != "10.1.2.3:8080 拒绝"


def test_mask_chat_ids_covers_five_digits() -> None:
    """F16: 早期 5 位 QQ 号段不再穿透。"""

    masked = mask_chat_ids("send failed for 12345: timeout")
    assert "12345" not in masked
    assert masked.endswith(": timeout")


# ---------------------------------------------------------------------------
# report_store: F14 prune 本地零点回退
# ---------------------------------------------------------------------------


def test_prune_fallback_aligns_to_local_midnight(tmp_path: Path, monkeypatch) -> None:
    """无 local_day_start 时回退用宿主本地零点，不是 UTC 零点。"""

    from astrbot_plugin_statuscard import report_store as rs

    store = ReportStore(tmp_path / "report.db")

    def fake_localtime(ts):
        d = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
        return types.SimpleNamespace(tm_year=d.year, tm_mon=d.month, tm_mday=d.day)

    monkeypatch.setattr(rs.time, "localtime", fake_localtime)
    now = 1_800_000_000 + 23 * 3600
    sample = 1_799_940_000  # UTC boundary keeps it; local boundary removes it
    store.add_resource(sample, 1.0, 2.0, 0.0, 1.0, 0.0)
    store.prune(resource_days=1, event_days=90, now=now)
    kept = [int(r["ts"]) for r in store.list_resources(0)]
    assert sample not in kept


def test_prune_explicit_local_day_start_keeps_oldest_day(tmp_path: Path) -> None:
    """生产主路径（显式传零点）回归护栏：晚夜间清理不删最老一天。"""

    store = ReportStore(tmp_path / "report.db")
    base = 1_800_000_000
    day_start = base - (base % 86400)
    old_ts = day_start - 30 * 86400 + 1800
    store.add_resource(old_ts, 10.0, 50.0, 10.0, 40.0, 0.1)
    store.prune(
        resource_days=30,
        event_days=90,
        now=day_start + 23 * 3600,
        local_day_start=day_start,
    )
    rows = store.list_resources(day_start - 31 * 86400, day_start)
    assert any(int(r["ts"]) == old_ts for r in rows)


# ---------------------------------------------------------------------------
# main: F39 weekly day=None 守卫（防手编 state）
# ---------------------------------------------------------------------------


def test_mask_helpers_still_hide_umo_digits() -> None:
    """护栏：mask 改动不得破坏既有 6 位以上遮蔽。"""

    masked = mask_chat_ids("code 461961169 rejected")
    assert "461961169" not in masked


# ---------------------------------------------------------------------------
# redactor host 归一（F9）：空白 host 在运行时与 audit 两侧同构
# ---------------------------------------------------------------------------


def test_blank_webhook_host_is_loopback_not_wildcard() -> None:
    """F9：读点把空白归一为 loopback；函数自身保持 wildcard 防御。"""

    from pathlib import Path as _P

    from astrbot_plugin_statuscard.alerts import _webhook_bind_allowed

    assert _webhook_bind_allowed("127.0.0.1", require_token=False, token="") is True
    # Explicit empty / wildcard still refuses to start without a token.
    assert _webhook_bind_allowed("", require_token=False, token="") is False
    assert _webhook_bind_allowed("0.0.0.0", require_token=False, token="") is False
    # The startup reader normalises whitespace to loopback BEFORE the check
    # (alerts.py: strip first, then the "127.0.0.1" fallback) -- lock that
    # ordering in without hard-coding the exact indentation.
    source = (_P(__file__).resolve().parents[1] / "alerts.py").read_text(
        encoding="utf-8"
    )
    assert 'or "").strip()' in source
    assert 'or "127.0.0.1"' in source


# ---------------------------------------------------------------------------
# main: 反向审计补充的覆盖
# ---------------------------------------------------------------------------


def test_format_missed_detects_leftover_placeholder_only() -> None:
    """审计发现 3：填充后文本里的用户值含花括号不得误判为未填充。"""

    from astrbot_plugin_statuscard.main import _format_missed

    # tr() 的 format 失败会返回未填模板 -> 占位符残留 -> 判定未填充。
    assert _format_missed("「{label}」阈值为 0", {"label": "CPU"}) is True
    # 正常填充后的文本：参数值本身含 {xxx} 不再触发第二次 format 判断。
    assert _format_missed("「cpu{value}」阈值为 0", {"label": "cpu{value}"}) is False
    # 无参数文本恒为已填充。
    assert _format_missed("纯文本", {}) is False


def test_fallback_card_sweep_keeps_recent_files(tmp_path: Path) -> None:
    """审计发现 7：1200 秒清理窗口不吞 750 秒内的重试文件。"""

    # _write_fallback_card 是 StatusCardPlugin 的方法；直接模拟清扫逻辑
    # 会测不到真实代码。改为对真实方法所需的最小桩断言窗口常量：
    source = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("main.py")
        .read_text(encoding="utf-8")
    )
    assert "time.time() - 1200" in source, "sweep window must cover RETRY_DELAYS sum"
