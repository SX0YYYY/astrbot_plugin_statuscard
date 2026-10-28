"""v2.18.0: 统一的「默认推送目标」与旧的分模块目标兼容。

背景：`alerts.targets` 升级为所有通知共用的默认目标；`monitors.targets`
与 `daily_report.targets` 收进高级选项、保留读取兼容（有值时覆盖默认目标）。
这组用例锁住三件事：

1. 只填默认目标时，监测 / 日报 / 每日状态图都不报“缺少目标”；
2. 旧的分模块目标仍然生效（不破坏已有部署），但体检会提示；
3. 通知中心规则依旧优先于两者。
"""

from __future__ import annotations

from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.monitor import ServiceMonitor

DEFAULT = "default:GroupMessage:111111111"
LEGACY = "default:GroupMessage:222222222"
RULE = "default:GroupMessage:333333333"


def _config(**overrides: dict) -> dict:
    config: dict = {
        "alerts": {
            "enabled": False,
            "targets": [],
            "platform_id": "default",
            "offline_notify": False,
        },
        "monitors": {"enabled": False, "targets": [], "items": []},
        "daily_report": {"enabled": False, "weekly_enabled": False, "targets": []},
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


def _codes(config: dict) -> set[str]:
    return {issue.code for issue in run_audit(config)}


def _bare_monitor(config: dict) -> ServiceMonitor:
    monitor = object.__new__(ServiceMonitor)
    monitor.config = config
    monitor.platform_id = str(
        (config.get("alerts") or {}).get("platform_id") or "default"
    )
    raw = (config.get("monitors") or {}).get("targets") or []
    if not raw:
        raw = (config.get("alerts") or {}).get("targets") or []
    monitor.targets = [
        item if ":" in item else f"{monitor.platform_id}:GroupMessage:{item}"
        for item in raw
        if isinstance(item, str) and item.strip()
    ]
    return monitor


def test_default_target_alone_satisfies_every_module() -> None:
    config = _config(
        alerts={
            "enabled": True,
            "targets": [DEFAULT],
            "daily_summary": {"enabled": True},
        },
        monitors={"enabled": True, "targets": [], "items": []},
        daily_report={"enabled": True, "targets": []},
    )
    missing = {
        "alerts_no_targets",
        "monitors_no_targets",
        "daily_report_no_targets",
        "daily_summary_no_targets",
    }
    assert not missing & _codes(config)


def test_legacy_module_targets_still_work() -> None:
    """旧的分模块目标优先于默认目标，行为与升级前一致。"""

    monitor = _bare_monitor(
        _config(
            alerts={"targets": [DEFAULT]},
            monitors={"targets": [LEGACY], "items": []},
        )
    )
    assert monitor._targets_for("service") == [LEGACY]


def test_monitor_falls_back_to_default_target() -> None:
    monitor = _bare_monitor(
        _config(alerts={"targets": [DEFAULT]}, monitors={"targets": []})
    )
    assert monitor._targets_for("service") == [DEFAULT]


def test_notification_rule_beats_default_and_legacy() -> None:
    monitor = _bare_monitor(
        _config(
            alerts={"targets": [DEFAULT]},
            monitors={"targets": [LEGACY], "items": []},
        )
    )
    monitor.config = {
        **monitor.config,
        "notify": {"rules": [{"enabled": True, "target": RULE, "kinds": ["service"]}]},
    }
    assert monitor._targets_for("service") == [RULE]


def test_legacy_module_targets_are_surfaced_by_audit() -> None:
    config = _config(
        alerts={"enabled": True, "targets": [DEFAULT]},
        monitors={"enabled": True, "targets": [LEGACY], "items": []},
        daily_report={"enabled": True, "targets": [LEGACY]},
    )
    issues = [
        issue for issue in run_audit(config) if issue.code == "legacy_module_targets"
    ]
    assert len(issues) == 1
    assert "服务监测" in issues[0].message
    assert "日报/周报" in issues[0].message


def test_no_legacy_warning_when_lists_are_empty() -> None:
    config = _config(alerts={"enabled": True, "targets": [DEFAULT]})
    assert "legacy_module_targets" not in _codes(config)
