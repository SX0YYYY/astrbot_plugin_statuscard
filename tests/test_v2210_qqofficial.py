"""v2.21.0: QQ 官方机器人适配（双平台共存）。

覆盖平台白名单、openid UMO/群会话解析、scene 注入、无 OneBot 客户端的
采集回退、官方平台快照完整性、统计 SQL 平台列表、审计提示与命令权限。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import collectors, platform_support
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.main import StatusCardPlugin
from astrbot_plugin_statuscard.utils import mask_chat_ids, mask_umo


class _Adapter:
    def __init__(self, name: str = "qq_official", inst_id: str = "Q官弗洛洛"):
        self._name = name
        self._id = inst_id
        self.scenes: list[tuple[str, str]] = []
        self.status = SimpleNamespace(value="running")

    def meta(self):
        return SimpleNamespace(name=self._name, id=self._id)

    def remember_session_scene(self, session_id: str, scene: str) -> None:
        self.scenes.append((session_id, scene))


class _Manager:
    def __init__(self, insts):
        self._insts = insts

    def get_insts(self):
        return list(self._insts)


class _Context:
    def __init__(self, insts):
        self.platform_manager = _Manager(insts)


class _Event:
    def __init__(
        self,
        platform_name: str = "qq_official",
        platform_id: str = "Q官弗洛洛",
        sender: str = "E30A1E7F00000000000000000000AAAA",
        admin: bool = False,
    ):
        self._platform_name = platform_name
        self._platform_id = platform_id
        self._sender = sender
        self._admin = admin

    def get_platform_name(self):
        return self._platform_name

    def get_platform_id(self):
        return self._platform_id

    def get_self_id(self):
        return "qq_official"

    def get_sender_id(self):
        return self._sender

    def is_admin(self):
        return self._admin


# ---------------------------------------------------------------------------
# 平台白名单与标签
# ---------------------------------------------------------------------------


def test_supported_platforms_include_official_bot() -> None:
    assert platform_support.is_supported_platform("aiocqhttp")
    assert platform_support.is_supported_platform("qq_official")
    assert platform_support.is_supported_platform("qq_official_webhook")
    assert not platform_support.is_supported_platform("webchat")
    assert not platform_support.is_supported_platform("telegram")
    assert platform_support.platform_label("qq_official") == "QQ 官方机器人"
    assert platform_support.platform_label("aiocqhttp") == "OneBot"


# ---------------------------------------------------------------------------
# UMO / 群会话解析
# ---------------------------------------------------------------------------


def test_group_session_id_strips_unique_session_prefix() -> None:
    assert platform_support.parse_umo("Q官:X:abc") == ("Q官", "X", "abc")
    assert platform_support.parse_umo("bad") is None
    assert platform_support.group_session_id("sender_GROUPOPENID") == "GROUPOPENID"
    assert platform_support.group_session_id("GROUPOPENID") == "GROUPOPENID"


def test_known_group_session_ids_dedupes_and_unwraps(tmp_path: Path) -> None:
    db_path = tmp_path / "data_v4.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE umo_senders (id INTEGER PRIMARY KEY, umo TEXT, sender_id TEXT)"
    )
    rows = [
        ("Q官:GroupMessage:AAA111", "s1"),
        ("Q官:GroupMessage:BBB222", "s2"),
        ("Q官:GroupMessage:s3_CCC333", "s3"),
        ("default:GroupMessage:461961169", "s4"),
        ("Q官:FriendMessage:DDD444", "s5"),
    ]
    connection.executemany(
        "INSERT INTO umo_senders (umo, sender_id) VALUES (?, ?)", rows
    )
    connection.commit()
    connection.close()

    ids = platform_support.known_group_session_ids(db_path, "Q官")
    assert ids == ["CCC333", "BBB222", "AAA111"]


def test_known_group_session_ids_dedupes_before_limit_and_escapes_like(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "data_v4.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE umo_senders (id INTEGER PRIMARY KEY, umo TEXT, sender_id TEXT)"
    )
    rows = [("qq_official:GroupMessage:BUSY", f"s{i}") for i in range(600)]
    rows.append(("qq_official:GroupMessage:OTHER", "other"))
    # The underscore in "qq_official" is a LIKE wildcard; an unescaped
    # pattern would also match this other instance.
    rows.append(("qqXofficial:GroupMessage:WRONG", "wrong"))
    connection.executemany(
        "INSERT INTO umo_senders (umo, sender_id) VALUES (?, ?)", rows
    )
    connection.commit()
    connection.close()

    ids = platform_support.known_group_session_ids(db_path, "qq_official")
    # One busy group must not crowd out the rest (LIMIT after GROUP BY), and
    # the foreign instance must not leak in.
    assert set(ids) == {"BUSY", "OTHER"}


# ---------------------------------------------------------------------------
# scene 注入
# ---------------------------------------------------------------------------


def test_ensure_group_scene_uses_group_openid() -> None:
    adapter = _Adapter()
    ok, failed = platform_support.ensure_group_scene(
        adapter, ["Q官:GroupMessage:sender_GROUPID"]
    )
    assert ok and failed == []
    assert adapter.scenes == [("GROUPID", "group")]


def test_ensure_group_scene_skips_private_and_non_official() -> None:
    adapter = _Adapter()
    assert platform_support.ensure_group_scene(adapter, ["Q官:FriendMessage:USER"]) == (
        True,
        [],
    )
    assert adapter.scenes == []
    onebot = _Adapter(name="aiocqhttp")
    assert platform_support.ensure_group_scene(onebot, ["Q官:GroupMessage:G"]) == (
        True,
        [],
    )
    # No adapter at all: the caller must report failure instead of pretending.
    ok, failed = platform_support.ensure_group_scene(None, ["Q官:GroupMessage:G"])
    assert not ok and failed == ["Q官:GroupMessage:G"]


# ---------------------------------------------------------------------------
# 采集：官方分支不需要 OneBot 客户端
# ---------------------------------------------------------------------------


def test_collect_bot_info_official_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _Adapter()
    event = _Event()
    config = {"alerts": {"platform_id": "Q官弗洛洛"}}

    monkeypatch.setattr(collectors, "_astrbot_data_db", lambda: tmp_path / "x.db")
    monkeypatch.setattr(
        collectors, "known_group_session_ids", lambda db, pid: ["G1", "G2"]
    )

    async def fake_member_total(adapter_arg, ids):
        assert ids == ["G1", "G2"]
        return 123

    monkeypatch.setattr(collectors, "group_member_total", fake_member_total)

    bot = asyncio.run(
        collectors.collect_bot_info(
            _Context([adapter]), event, config, {"available": False}
        )
    )
    assert bot["has_client"] is False
    assert bot["friends"] is None
    assert bot["groups"] == 2
    assert bot["members"] == 123
    assert bot["online"] is True
    assert bot["protocol_name"] == "QQ 官方机器人"
    assert bot["official_platform"] is True
    assert bot["avatar_url"] == ""
    assert bot["adapter"] == "qq_official"


def test_collect_bot_info_official_without_known_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _Adapter(inst_id="Q官2")
    event = _Event(platform_id="Q官2")
    config = {"alerts": {"platform_id": "Q官2"}}
    monkeypatch.setattr(collectors, "_astrbot_data_db", lambda: None)
    bot = asyncio.run(
        collectors.collect_bot_info(
            _Context([adapter]), event, config, {"available": False}
        )
    )
    # Unknown, not zero: a fresh install has simply not seen a group yet.
    assert bot["groups"] is None
    assert bot["members"] is None


def test_collect_bot_info_without_event_uses_configured_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduled card / daily snapshot has no event to carry the platform."""

    adapter = _Adapter(inst_id="Q官3")
    config = {"alerts": {"platform_id": "Q官3"}}
    monkeypatch.setattr(collectors, "_astrbot_data_db", lambda: None)
    bot = asyncio.run(
        collectors.collect_bot_info(
            _Context([adapter]), None, config, {"available": False}
        )
    )
    assert bot["official_platform"] is True
    assert bot["protocol_name"] == "QQ 官方机器人"


# ---------------------------------------------------------------------------
# 脱敏：openid 不能原样透出
# ---------------------------------------------------------------------------


def test_mask_umo_hides_openid_tail() -> None:
    masked = mask_umo("Q官:GroupMessage:676E32B2A1C4D5E6F708192A3B4C5D6E")
    assert masked.startswith("Q官:GroupMessage:")
    assert "676E32B2" not in masked
    assert masked.endswith("5D6E")


def test_mask_chat_ids_hides_hex_openid() -> None:
    text = mask_chat_ids("send failed for 676E32B2A1C4D5E6F708192A3B4C5D6E now")
    assert "676E32B2" not in text
    assert "5D6E" in text


# ---------------------------------------------------------------------------
# 审计提示
# ---------------------------------------------------------------------------


def test_audit_reports_platform_mismatch_and_missing_prefix() -> None:
    config = {
        "alerts": {
            "enabled": True,
            "platform_id": "default",
            "targets": ["default:GroupMessage:461961169"],
        }
    }
    issues = run_audit(config, platform_instances={"Q官弗洛洛": "qq_official"})
    codes = {issue.code for issue in issues}
    assert "platform_id_mismatch" in codes
    # The target prefix "default" is not a live instance either.
    assert "target_platform_missing" in codes


def test_audit_flags_numeric_group_on_official_prefix() -> None:
    config = {
        "alerts": {
            "enabled": True,
            "platform_id": "Q官弗洛洛",
            "targets": ["Q官弗洛洛:GroupMessage:461961169"],
        }
    }
    issues = run_audit(config, platform_instances={"Q官弗洛洛": "qq_official"})
    codes = {issue.code for issue in issues}
    assert "official_group_id_numeric" in codes
    assert "platform_id_mismatch" not in codes


def test_audit_flags_bare_numeric_target_on_official() -> None:
    config = {
        "alerts": {
            "enabled": True,
            "platform_id": "Q官弗洛洛",
            "targets": ["461961169"],
        }
    }
    issues = run_audit(config, platform_instances={"Q官弗洛洛": "qq_official"})
    assert "official_group_id_numeric" in {issue.code for issue in issues}


def test_audit_skips_platform_mismatch_when_push_disabled() -> None:
    config = {
        "alerts": {"enabled": False, "platform_id": "default", "targets": []},
        "monitors": {"enabled": False},
        "daily_report": {"enabled": False, "weekly_enabled": False},
    }
    issues = run_audit(config, platform_instances={"Q官弗洛洛": "qq_official"})
    assert "platform_id_mismatch" not in {issue.code for issue in issues}


def test_audit_accepts_chinese_platform_prefix() -> None:
    config = {
        "alerts": {
            "enabled": True,
            "platform_id": "Q官弗洛洛",
            "targets": ["Q官弗洛洛:GroupMessage:676E32B2A1C4D5E6F708192A3B4C5D6E"],
        }
    }
    issues = run_audit(config, platform_instances={"Q官弗洛洛": "qq_official"})
    assert "target_format_suspect" not in {issue.code for issue in issues}


# ---------------------------------------------------------------------------
# 命令权限：官方 openid 白名单
# ---------------------------------------------------------------------------


def _plugin(config: dict) -> StatusCardPlugin:
    plugin = object.__new__(StatusCardPlugin)
    plugin.config = config
    return plugin


def test_is_operator_aiocqhttp_keeps_admin_rule() -> None:
    plugin = _plugin({"access": {"official_admin_ids": ["AAA"]}})
    assert plugin._is_operator(_Event(platform_name="aiocqhttp", admin=True))
    # The openid allowlist must never apply to OneBot senders.
    assert not plugin._is_operator(
        _Event(platform_name="aiocqhttp", sender="AAA", admin=False)
    )


def test_is_operator_official_uses_openid_allowlist() -> None:
    plugin = _plugin({"access": {"official_admin_ids": ["AAA"]}})
    assert plugin._is_operator(_Event(sender="AAA"))
    assert not plugin._is_operator(_Event(sender="BBB"))
    # AstrBot admin still passes even without an allowlist entry.
    assert plugin._is_operator(_Event(sender="BBB", admin=True))


def test_non_operator_refusal_keeps_the_default_llm_out() -> None:
    """The MEMBER filter lets the event through; the refusal must not reach the LLM."""

    plugin = _plugin({})
    event = _Event(platform_name="aiocqhttp", sender="NOPE", admin=False)
    event.call_llm = False
    results = []

    def plain_result(text):
        return SimpleNamespace(chain=[text])

    event.plain_result = plain_result
    event.should_call_llm = lambda value: setattr(event, "call_llm", value)

    async def drive():
        agen = StatusCardPlugin.cmd_status(plugin, event)
        async for ret in agen:
            results.append(ret)

    asyncio.run(drive())
    assert event.call_llm is True
    assert results and "仅限管理员" in str(results[0].chain[0])


# ---------------------------------------------------------------------------
# 群员汇总：部分失败必须按未知处理
# ---------------------------------------------------------------------------


def test_group_member_total_returns_none_when_any_group_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types as pytypes

    astrbot = sys.modules["astrbot"]
    for name in ("astrbot.core", "astrbot.core.platform"):
        if name not in sys.modules:
            module = pytypes.ModuleType(name)
            module.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = module
            setattr(sys.modules[name.rsplit(".", 1)[0]], name.rsplit(".", 1)[1], module)
    message_mod = pytypes.ModuleType("astrbot.core.platform.astrbot_message")
    message_mod.Group = lambda group_id: SimpleNamespace(group_id=group_id)
    sys.modules["astrbot.core.platform.astrbot_message"] = message_mod
    group_info = pytypes.ModuleType(
        "astrbot.core.platform.sources.qqofficial.qqofficial_group_info"
    )

    async def fake_get_group(bot, group):
        if group.group_id == "BAD":
            raise RuntimeError("api down")
        return SimpleNamespace(member_count=10)

    group_info.get_qq_group = fake_get_group
    sys.modules["astrbot.core.platform.sources.qqofficial.qqofficial_group_info"] = (
        group_info
    )

    adapter = _Adapter()
    adapter.get_client = lambda: object()
    result = asyncio.run(
        platform_support.group_member_total(adapter, ["G1", "BAD", "G3"])
    )
    # A partial sum would be frozen into today's snapshot as complete.
    assert result is None
    assert astrbot is not None
