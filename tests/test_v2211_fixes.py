"""v2.21.1：全流程找茬修复批的行为回归测试。

覆盖本轮确认并修复的问题：聚合缓冲覆盖告警起点、快照冻结后仍重试、
验证按钮并发重复发送、平台体检漏检、双平台数字群号误报、通知规则类型
容错、脱敏类型误报、redact_url 截断自由文本、unique_session 回执、
已知群去重、alert_since=0 的恢复时长、每日状态图空目标、配置类型容错。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard import report as report_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import DiskUsage, resolve_targets
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.main import StatusCardPlugin
from astrbot_plugin_statuscard.onebot_receipt import send_direct
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.platform_support import known_group_session_ids
from astrbot_plugin_statuscard.report import ReportCollector
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import redact_url

TZ = UTC


class _Plugin:
    context = None

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown", kind="resource"):
        self.sent.append(str(text))
        if self.ok:
            return DeliveryResult(ok=True, ok_targets=list(targets))
        return DeliveryResult(ok=False, failed_targets=list(targets))


def _engine(
    tmp_path: Path,
    rules: dict | None = None,
    config: dict | None = None,
    plugin: _Plugin | None = None,
) -> AlertEngine:
    state = StateStore(tmp_path / "state.json")
    if rules is not None:
        state.data.setdefault("alerts", {})["rules"] = rules
    store = ReportStore(tmp_path / "report.db")
    engine = AlertEngine(plugin or _Plugin(), state, config or {}, report_store=store)
    engine.targets = ["default:GroupMessage:123456"]
    return engine


# ---------------------------------------------------------------------------
# 聚合缓冲：首次告警必须保留 run-up 起点
# ---------------------------------------------------------------------------


def test_buffered_first_alert_keeps_run_up_start(tmp_path: Path) -> None:
    """A buffered first push must not overwrite the first over-threshold time.

    The old branch used ``if not alerting: alert_since = now``, so every
    buffered episode reported (sustain-1)*interval less than the delivered
    path and the report's ``started_ts`` was late too.
    """

    started = time.time() - 120
    engine = _engine(
        tmp_path,
        {
            "cpu": {
                "count": 0,
                "alert_since": started,
                "alerting": False,
                "alerted": False,
            }
        },
        {"alerts": {"sustain_samples": 1}},
    )

    async def buffered(*_args, **_kwargs):
        return None

    engine._send = buffered  # type: ignore[method-assign]
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = engine.state.get("alerts")["rules"]["cpu"]
    assert rule["alerting"] is True
    assert rule["alert_since"] == pytest.approx(started)


# ---------------------------------------------------------------------------
# 官方快照冻结：recv_total 缺失时也要停止当天重试
# ---------------------------------------------------------------------------


def test_snapshot_freeze_stops_retries_without_recv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    collector = ReportCollector(
        SimpleNamespace(context=None),
        state,
        {"display": {"timezone": "Asia/Shanghai"}},
        store,
    )
    day = "2026-11-14"
    collector._snapshot_attempts[day] = 12
    fixed = datetime(2026, 11, 14, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(report_module, "now_in_timezone", lambda _cfg: fixed)
    monkeypatch.setattr(report_module, "read_message_stats_total", lambda: (None, None))

    async def fake_bot(*_args, **_kwargs):
        return {
            "platform_name": "qq_official",
            "friends": None,
            "groups": None,
            "members": None,
        }

    monkeypatch.setattr(report_module, "collect_bot_info", fake_bot)
    writes: list[tuple] = []
    monkeypatch.setattr(store, "record_snapshot", lambda *args: writes.append(args))

    asyncio.run(collector._maybe_snapshot())

    # complete=True alone does not satisfy snapshot_ready (recv_total is
    # still NULL), so the loop would retry 288 times a day without this.
    assert collector._snapshot_attempts[day] == 288
    assert writes, "the frozen snapshot should still be written once"


# ---------------------------------------------------------------------------
# 看板验证：并发/慢请求不能重复发送真实消息
# ---------------------------------------------------------------------------


def _install_verify_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        page_api_module, "_request", SimpleNamespace(query={"kind": "targets"})
    )
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)


def test_verify_blocks_concurrent_duplicate_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowPlugin:
        def __init__(self) -> None:
            self.config = {
                "alerts": {"platform_id": "default", "targets": ["123456"]},
            }
            self.calls = 0

        async def send_text(self, targets, text, source="unknown"):
            self.calls += 1
            await asyncio.sleep(0.2)
            return DeliveryResult(ok=True, ok_targets=list(targets))

    plugin = _SlowPlugin()
    api = PageApi(plugin)
    _install_verify_request(monkeypatch)

    async def run_both():
        return await asyncio.gather(api.verify(), api.verify())

    first, second = asyncio.run(run_both())
    assert plugin.calls == 1, "the same real message must be sent only once"
    assert {first.get("ok"), second.get("ok")} == {True, False}
    rejected = first if not first.get("ok") else second
    assert rejected["error_code"] == "verify_too_frequent"


def test_verify_success_hides_stale_direct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "default:GroupMessage:123456"

    class _Plugin2:
        def __init__(self) -> None:
            self.config = {"alerts": {"platform_id": "default", "targets": ["123456"]}}

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(
                ok=True,
                ok_targets=[target],
                receipts={
                    target: {
                        "ok": True,
                        "via": "framework",
                        "error": "会话 ID 不是数字: 123456",
                    }
                },
            )

    api = PageApi(_Plugin2())
    _install_verify_request(monkeypatch)
    payload = asyncio.run(api.verify())
    row = payload["results"][0]
    assert row["state"] == "ok"
    assert row["error"] == ""


# ---------------------------------------------------------------------------
# 配置体检：平台检查覆盖每日状态图/LLM 告警，双平台不误报
# ---------------------------------------------------------------------------


def test_audit_checks_platform_for_daily_summary_only() -> None:
    issues = run_audit(
        {
            "alerts": {
                "enabled": False,
                "daily_summary": {"enabled": True, "time": "20:00"},
                "platform_id": "default",
            },
            "daily_report": {},
        },
        platform_instances={"Q官弗洛洛": "qq_official"},
    )
    assert any(issue.code == "platform_id_mismatch" for issue in issues)


def test_audit_checks_platform_for_provider_watch_only() -> None:
    issues = run_audit(
        {
            "alerts": {
                "enabled": False,
                "provider_watch": {"enabled": True},
                "platform_id": "default",
            },
            "daily_report": {},
        },
        platform_instances={"Q官弗洛洛": "qq_official"},
    )
    assert any(issue.code == "platform_id_mismatch" for issue in issues)


def test_audit_numeric_group_is_valid_on_onebot_in_dual_platform() -> None:
    issues = run_audit(
        {
            "alerts": {
                "enabled": True,
                "platform_id": "default",
                "targets": ["123456789"],
            },
            "daily_report": {},
        },
        platform_instances={
            "Q官弗洛洛": "qq_official",
            "default": "aiocqhttp",
        },
    )
    assert not any(issue.code == "official_group_id_numeric" for issue in issues)


def test_audit_numeric_group_flags_on_official_instance() -> None:
    issues = run_audit(
        {
            "alerts": {
                "enabled": True,
                "platform_id": "Q官弗洛洛",
                "targets": ["123456789"],
            },
            "daily_report": {},
        },
        platform_instances={"Q官弗洛洛": "qq_official"},
    )
    assert any(issue.code == "official_group_id_numeric" for issue in issues)


def test_audit_bare_openid_is_a_valid_official_target() -> None:
    issues = run_audit(
        {
            "alerts": {
                "enabled": True,
                "platform_id": "Q官弗洛洛",
                "targets": ["676E32B250F0B4886D90B722EF1F57EA"],
            },
            "daily_report": {},
        },
        platform_instances={"Q官弗洛洛": "qq_official"},
    )
    assert not any(issue.code == "target_format_suspect" for issue in issues)


def test_audit_flags_non_list_redact_value() -> None:
    issues = run_audit(
        {"display": {"redact": {"hostname": True}}, "daily_report": {}},
        platform_instances={},
    )
    assert any(issue.code == "redact_rules_invalid" for issue in issues)


def test_audit_ignores_disabled_rule_with_string_false() -> None:
    issues = run_audit(
        {
            "notify": {
                "rules": [
                    {
                        "enabled": "false",
                        "kinds": ["resource"],
                        "target": "bad target",
                    }
                ]
            },
            "daily_report": {},
        },
        platform_instances={},
    )
    assert not any(issue.code == "target_format_suspect" for issue in issues)


def test_audit_rejects_lowercase_message_type() -> None:
    issues = run_audit(
        {
            "alerts": {"enabled": True, "targets": ["default:groupmessage:123"]},
            "daily_report": {},
        },
        platform_instances={"default": "aiocqhttp"},
    )
    assert any(issue.code == "target_format_suspect" for issue in issues)


# ---------------------------------------------------------------------------
# 通知中心：一条坏规则不能拖垮监测/日报启动
# ---------------------------------------------------------------------------


def test_resolve_targets_tolerates_non_iterable_kinds() -> None:
    config = {
        "notify": {
            "rules": [{"enabled": True, "kinds": 5, "target": "123"}],
        }
    }
    assert resolve_targets(config, "resource") is None


# ---------------------------------------------------------------------------
# redact_url：自由文本只重写 URL 片段
# ---------------------------------------------------------------------------


def test_redact_url_keeps_prose_around_a_url() -> None:
    text = "连接 http://10.0.0.8:8080/health?x=1 超时"
    cleaned = redact_url(text)
    assert "超时" in cleaned
    assert "x=1" not in cleaned


def test_redact_url_does_not_treat_at_name_as_credentials() -> None:
    assert redact_url("提醒 @运维 查看") == "提醒 @运维 查看"
    assert redact_url("邮箱 admin@example.com 报警") == "邮箱 admin@example.com 报警"


# ---------------------------------------------------------------------------
# OneBot：unique_session 的 sender_group 仍走直发并拿到回执
# ---------------------------------------------------------------------------


def test_send_direct_unique_session_group_id() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_action(self, action, **kwargs):
            self.calls.append((action, kwargs))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 9}}

    client = _Client()
    receipt = asyncio.run(
        send_direct(
            client,
            "default:GroupMessage:123456_987654",
            [{"type": "text", "data": {"text": "x"}}],
            platform_id="default",
        )
    )
    assert receipt.ok is True
    assert client.calls[0][0] == "send_group_msg"
    assert client.calls[0][1]["group_id"] == 987654


# ---------------------------------------------------------------------------
# 已知群：unique_session 下一个群的多个发送者不能挤掉其它群
# ---------------------------------------------------------------------------


def test_known_groups_dedupe_after_unique_session(tmp_path: Path) -> None:
    db = tmp_path / "data_v4.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute("CREATE TABLE umo_senders (umo TEXT)")
        for index in range(210):
            connection.execute(
                "INSERT INTO umo_senders (umo) VALUES (?)",
                (f"default:GroupMessage:sender{index}_GROUPA",),
            )
        connection.execute(
            "INSERT INTO umo_senders (umo) VALUES (?)",
            ("default:GroupMessage:GROUPB",),
        )
        connection.commit()
    finally:
        connection.close()

    groups = known_group_session_ids(db, "default")
    assert "GROUPA" in groups
    assert "GROUPB" in groups


# ---------------------------------------------------------------------------
# alert_since=0：恢复通知不能报出 19675 天
# ---------------------------------------------------------------------------


def test_recovery_duration_with_zero_alert_since(tmp_path: Path) -> None:
    plugin = _Plugin()
    engine = _engine(
        tmp_path,
        {
            "cpu": {
                "alerting": True,
                "alerted": True,
                "alert_since": 0,
                "last_sent": 0,
                "count": 0,
                "last_error": "",
            }
        },
        {"alerts": {"sustain_samples": 1}},
        plugin=plugin,
    )
    asyncio.run(engine._evaluate("cpu", "CPU", 1.0, 85.0, "%", "1.0%"))
    assert plugin.sent, "a recovery notice should be sent"
    assert "19675" not in plugin.sent[-1]
    assert "天" not in plugin.sent[-1]


# ---------------------------------------------------------------------------
# 每日定时状态图：目标为空不消费当天的尝试标记
# ---------------------------------------------------------------------------


def test_daily_summary_without_targets_does_not_consume_attempt(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, config={})
    engine.targets = []
    today = page_api_module.now_in_timezone(engine.config).strftime("%Y-%m-%d")
    assert asyncio.run(engine._send_daily_summary()) is True
    assert engine._daily_skip_day == today
    assert engine._daily_catch_up_allowed(today) is False
    assert engine._daily_last_attempt_day is None


# ---------------------------------------------------------------------------
# 配置类型容错：非可迭代的 official_admin_ids / 超长 range
# ---------------------------------------------------------------------------


class _Event:
    def __init__(self, platform_name="qq_official", sender="AAA", admin=False):
        self._platform = platform_name
        self._sender = sender
        self._admin = admin

    def is_admin(self) -> bool:
        return self._admin

    def get_platform_name(self) -> str:
        return self._platform

    def get_sender_id(self) -> str:
        return self._sender


def test_is_operator_tolerates_non_iterable_admin_ids() -> None:
    plugin = object.__new__(StatusCardPlugin)
    plugin.config = {"access": {"official_admin_ids": 5}}
    assert plugin._is_operator(_Event()) is False


def test_normalize_range_tolerates_huge_number() -> None:
    assert page_api_module._normalize_range("9" * 5000 + "d", "24h") == "24h"


# ---------------------------------------------------------------------------
# 磁盘：暂时读不到（超时/权限）不能关闭仍在成立的告警
# ---------------------------------------------------------------------------


def test_disk_timeout_keeps_alerting_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk_path = "/mnt/verify"
    plugin = _Plugin()
    engine = _engine(
        tmp_path,
        {
            f"disk:{disk_path}": {
                "alerting": True,
                "alerted": True,
                "alert_since": time.time() - 60,
                "count": 0,
            }
        },
        {
            "alerts": {"disk_threshold": 85},
            "display": {"disk_paths": [disk_path]},
        },
        plugin=plugin,
    )
    monkeypatch.setattr(
        alerts_module,
        "disk_usage_bounded",
        lambda paths, timeout=8.0: [
            DiskUsage(
                name=paths[0],
                used=None,
                total=None,
                percent=None,
                exception="读取超时",
            )
        ],
    )
    asyncio.run(engine._sample_once())
    rule = engine.state.get("alerts")["rules"][f"disk:{disk_path}"]
    assert rule["alerting"] is True, "a timeout is not proof the mount is gone"
    assert not any("已拔出" in text for text in plugin.sent)


def test_disk_missing_path_closes_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk_path = "/mnt/gone"
    plugin = _Plugin()
    engine = _engine(
        tmp_path,
        {
            f"disk:{disk_path}": {
                "alerting": True,
                "alerted": True,
                "alert_since": time.time() - 60,
                "count": 0,
            }
        },
        {
            "alerts": {"disk_threshold": 85},
            "display": {"disk_paths": [disk_path]},
        },
        plugin=plugin,
    )
    monkeypatch.setattr(
        alerts_module,
        "disk_usage_bounded",
        lambda paths, timeout=8.0: [
            DiskUsage(
                name=paths[0],
                used=None,
                total=None,
                percent=None,
                exception="[Errno 2] No such file or directory",
            )
        ],
    )
    asyncio.run(engine._sample_once())
    rule = engine.state.get("alerts")["rules"][f"disk:{disk_path}"]
    assert rule["alerting"] is False
    assert any("已拔出" in text for text in plugin.sent)


# ---------------------------------------------------------------------------
# 前端：慢 bridge 注入后不能因 typeof null.ready 中断初始化
# ---------------------------------------------------------------------------


def test_web_dashboard_uses_active_bridge() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "pages" / "status" / "app.js"
    ).read_text(encoding="utf-8")
    assert "const activeBridge = bridge || getBridge();" in source
    assert "typeof activeBridge.ready" in source
    assert "typeof bridge.ready" not in source


# ---------------------------------------------------------------------------
# 预览日报：拒绝早于 30 年的日期（与看板 API 同一口径）
# ---------------------------------------------------------------------------


def test_preview_report_rejects_ancient_date() -> None:
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert "parsed >= today - timedelta(days=365 * 30)" in source


# ---------------------------------------------------------------------------
# i18n：新增体检键必须中英齐全
# ---------------------------------------------------------------------------


def test_new_audit_keys_exist_in_both_languages() -> None:
    from astrbot_plugin_statuscard.i18n import MESSAGES

    keys = (
        "audit_platform_id_mismatch",
        "audit_target_platform_missing",
        "audit_official_group_id_numeric",
        "audit_redact_rules_invalid",
    )
    for key in keys:
        assert key in MESSAGES["zh-CN"]
        assert key in MESSAGES["en-US"]


# ---------------------------------------------------------------------------
# 反向审计回归：自由文本里的 userinfo 密码必须仍然被去掉
# ---------------------------------------------------------------------------


def test_redact_url_strips_userinfo_in_prose() -> None:
    for text, secret in (
        ("连接 user:pass@host/x?token=abc 失败", "pass"),
        ("连接 user:pass@example.com/api?token=abc 失败", "pass"),
        ("监控 user:secret@10.0.0.5/metrics 超时", "secret"),
    ):
        cleaned = redact_url(text)
        assert secret not in cleaned
        assert "token=abc" not in cleaned
        assert cleaned.endswith(("失败", "超时"))


def test_redact_url_keeps_a_plain_email() -> None:
    assert redact_url("邮箱 admin@example.com 报警") == "邮箱 admin@example.com 报警"


# ---------------------------------------------------------------------------
# 反向审计回归：非法下划线 session id 不能被截断成数字直发
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("session", ["foo_123456", "123_456_789"])
def test_send_direct_rejects_malformed_underscore_id(session: str) -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_action(self, action, **kwargs):
            self.calls.append((action, kwargs))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 9}}

    client = _Client()
    receipt = asyncio.run(
        send_direct(
            client,
            f"default:GroupMessage:{session}",
            [{"type": "text", "data": {"text": "x"}}],
            platform_id="default",
        )
    )
    assert receipt.ok is False
    assert not client.calls


# ---------------------------------------------------------------------------
# 反向审计回归：取消探测请求必须释放验证互斥
# ---------------------------------------------------------------------------


def test_cancelled_monitor_verify_clears_inflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Plugin3:
        def __init__(self) -> None:
            self.config = {"monitors": {"enabled": True}}

            async def probe(item):
                await asyncio.sleep(10)
                return True, "ok", ""

            self.monitor = SimpleNamespace(
                items=[{"name": "slow", "kind": "http"}],
                _probe=probe,
                _targets_for=lambda kind: ["default:GroupMessage:1"],
            )

    api = PageApi(_Plugin3())
    monkeypatch.setattr(
        page_api_module, "_request", SimpleNamespace(query={"kind": "monitors"})
    )
    monkeypatch.setattr(page_api_module, "_json_response", lambda payload: payload)

    async def run() -> None:
        task = asyncio.create_task(api.verify())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert "monitors" not in api._verify_inflight
