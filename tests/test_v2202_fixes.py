"""v2.20.2 修复批：新一轮独立找茬（告警投递 / 命令配置 / Web）的回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_statuscard.delivery import DeliveryService

# ---------------------------------------------------------------------------
# 投递：框架路径的明确拒绝
# ---------------------------------------------------------------------------


class _StateStub:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def set(self, key: str, value: object) -> None:
        self.data[key] = value

    def save(self) -> None:
        pass


def test_framework_action_failed_is_definitely_not_sent() -> None:
    class _ActionFailed(Exception):
        pass

    class _Context:
        async def send_message(self, target, chain):
            raise _ActionFailed("blocked by admin")

    service = DeliveryService(None, _StateStub(), lambda: None, lambda payload: payload)
    service.context = _Context()
    sent, uncertain = asyncio.run(
        service._send_via_astrbot("default:GroupMessage:1", {})
    )
    assert sent is False
    # ActionFailed means the action was refused: nothing reached the chat, so
    # this must not be treated as "maybe delivered" (v2.20.2).
    assert uncertain is False


def test_framework_timeout_stays_uncertain() -> None:
    class _Context:
        async def send_message(self, target, chain):
            await asyncio.sleep(1)
            return True

    service = DeliveryService(None, _StateStub(), lambda: None, lambda payload: payload)
    service.context = _Context()
    original = asyncio.wait_for

    async def short_wait(coro, timeout):
        return await original(coro, 0.05)

    asyncio.wait_for = short_wait  # type: ignore[assignment]
    try:
        sent, uncertain = asyncio.run(
            service._send_via_astrbot("default:GroupMessage:1", {})
        )
    finally:
        asyncio.wait_for = original  # type: ignore[assignment]
    assert sent is False
    assert uncertain is True


# ---------------------------------------------------------------------------
# 体检：坏配置不再中断初始化
# ---------------------------------------------------------------------------


def test_audit_survives_scalar_notify_rules() -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    issues = run_audit({"notify": {"rules": 5}})
    assert isinstance(issues, list)


def test_audit_accepts_missing_schedule_time() -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    issues = run_audit(
        {"daily_report": {"enabled": True}, "alerts": {"enabled": False}}
    )
    assert not [issue for issue in issues if issue.code == "invalid_schedule_time"]


def test_audit_flags_unknown_timezone() -> None:
    from astrbot_plugin_statuscard.audit import run_audit

    issues = run_audit({"display": {"timezone": "Asia/Shangai"}})
    assert any(issue.code == "display_timezone_invalid" for issue in issues)


# ---------------------------------------------------------------------------
# state：主文件缺失时从 .bak 恢复
# ---------------------------------------------------------------------------


def test_state_restores_backup_when_main_file_missing(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.state import StateStore

    path = tmp_path / "state.json"
    state = StateStore(path)
    state.set("net", {"month_up": 123})
    state.save()
    # The .bak is refreshed on the next save (it copies the previous file).
    state.save()
    assert (tmp_path / "state.json.bak").is_file()
    path.unlink()
    restored = StateStore(path)
    assert restored.get("net", {}).get("month_up") == 123


# ---------------------------------------------------------------------------
# 脱敏：URL 名称
# ---------------------------------------------------------------------------


def test_redact_url_strips_query_and_credentials() -> None:
    from astrbot_plugin_statuscard.utils import redact_url

    cleaned = redact_url("https://api.example.com/health?token=secret")
    assert "secret" not in cleaned
    assert cleaned.startswith("https://api.example.com/health")
