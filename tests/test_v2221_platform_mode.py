"""v2.22.1: platform_mode=auto and bare-group targets."""

from __future__ import annotations

from pathlib import Path

from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import resolve_targets
from astrbot_plugin_statuscard.platform_support import (
    pick_platform_id,
    resolve_platform_id,
)
from astrbot_plugin_statuscard.state import StateStore


class _Meta:
    def __init__(self, inst_id: str, name: str) -> None:
        self.id = inst_id
        self.name = name


class _Inst:
    def __init__(self, inst_id: str, name: str, status: str = "running") -> None:
        self._meta = _Meta(inst_id, name)
        self.status = status

    def meta(self) -> _Meta:
        return self._meta


class _Manager:
    def __init__(self, insts: list[_Inst]) -> None:
        self._insts = insts

    def get_insts(self) -> list[_Inst]:
        return self._insts


class _Context:
    def __init__(self, insts: list[_Inst]) -> None:
        self.platform_manager = _Manager(insts)


class _Plugin:
    context = None


def _config(**alerts) -> dict:
    base = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": True,
            "targets": ["461961169"],
            "offline_notify": False,
            "platform_id": "default",
        },
        "monitors": {"enabled": False, "targets": [], "items": []},
        "daily_report": {
            "enabled": False,
            "weekly_enabled": False,
            "targets": [],
            "time": "21:00",
        },
    }
    base["alerts"].update(alerts)
    return base


# ----------------------------------------------------------------------
# pick_platform_id
# ----------------------------------------------------------------------


def test_pick_auto_keeps_configured_when_alive() -> None:
    assert pick_platform_id("default", "auto", {"default": "aiocqhttp"}) == "default"


def test_pick_auto_switches_to_live_onebot() -> None:
    assert pick_platform_id("Q官弗洛洛", "auto", {"default": "aiocqhttp"}) == "default"


def test_pick_auto_switches_to_live_official() -> None:
    assert (
        pick_platform_id("default", "auto", {"Q官弗洛洛": "qq_official"}) == "Q官弗洛洛"
    )


def test_pick_forced_modes() -> None:
    instances = {"default": "aiocqhttp", "Q官弗洛洛": "qq_official"}
    assert pick_platform_id("Q官弗洛洛", "onebot", instances) == "default"
    assert pick_platform_id("default", "official", instances) == "Q官弗洛洛"


def test_pick_without_instances_keeps_configured() -> None:
    assert pick_platform_id("default", "auto", {}) == "default"
    assert pick_platform_id("Q官弗洛洛", "auto", None) == "Q官弗洛洛"


# ----------------------------------------------------------------------
# resolve_platform_id
# ----------------------------------------------------------------------


def test_resolve_platform_id_auto_uses_live_adapter() -> None:
    context = _Context([_Inst("default", "aiocqhttp")])
    config = _config(platform_id="Q官弗洛洛")
    assert resolve_platform_id(context, config) == "default"


def test_resolve_platform_id_prefers_configured_when_alive() -> None:
    context = _Context(
        [_Inst("default", "aiocqhttp"), _Inst("Q官弗洛洛", "qq_official")]
    )
    config = _config(platform_id="Q官弗洛洛")
    assert resolve_platform_id(context, config) == "Q官弗洛洛"


def test_resolve_platform_id_forced_mode() -> None:
    context = _Context(
        [_Inst("default", "aiocqhttp"), _Inst("Q官弗洛洛", "qq_official")]
    )
    assert resolve_platform_id(context, _config(platform_mode="onebot")) == "default"
    assert (
        resolve_platform_id(context, _config(platform_mode="official")) == "Q官弗洛洛"
    )


# ----------------------------------------------------------------------
# targets
# ----------------------------------------------------------------------


def test_resolve_targets_bare_group_follows_platform_id() -> None:
    config = _config()
    config["notify"] = {
        "rules": [{"enabled": True, "target": "461961169", "kinds": ["resource"]}]
    }
    assert resolve_targets(config, "resource", "Q官弗洛洛") == [
        "Q官弗洛洛:GroupMessage:461961169"
    ]


def test_resolve_targets_full_umo_is_preserved() -> None:
    config = _config()
    config["notify"] = {
        "rules": [
            {
                "enabled": True,
                "target": "default:GroupMessage:123456",
                "kinds": ["resource"],
            }
        ]
    }
    assert resolve_targets(config, "resource", "Q官弗洛洛") == [
        "default:GroupMessage:123456"
    ]


# ----------------------------------------------------------------------
# AlertEngine.refresh_platform
# ----------------------------------------------------------------------


def test_alert_engine_refresh_platform_rewrites_bare_group(tmp_path: Path) -> None:
    config = _config(platform_id="default")
    engine = AlertEngine(_Plugin(), StateStore(tmp_path / "state.json"), config)
    assert engine.targets == ["default:GroupMessage:461961169"]
    assert engine.refresh_platform("Q官弗洛洛") is True
    assert engine.targets == ["Q官弗洛洛:GroupMessage:461961169"]
    assert engine.refresh_platform("Q官弗洛洛") is False


def test_alert_engine_refresh_keeps_full_umo(tmp_path: Path) -> None:
    config = _config(platform_id="default", targets=["default:GroupMessage:123456"])
    engine = AlertEngine(_Plugin(), StateStore(tmp_path / "state.json"), config)
    assert engine.refresh_platform("Q官弗洛洛") is True
    assert engine.targets == ["default:GroupMessage:123456"]


# ----------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------


def test_audit_auto_mode_does_not_warn_about_stale_id() -> None:
    config = _config(platform_id="Q官弗洛洛", platform_mode="auto")
    issues = run_audit(config, platform_instances={"default": "aiocqhttp"})
    assert not [item for item in issues if item.code == "platform_id_mismatch"]


def test_audit_still_warns_without_matching_instance() -> None:
    config = _config(platform_id="Q官弗洛洛", platform_mode="auto")
    issues = run_audit(config, platform_instances={"webchat": "webchat"})
    assert [item for item in issues if item.code == "platform_id_mismatch"]
