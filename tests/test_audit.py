from __future__ import annotations

from astrbot_plugin_statuscard.audit import run_audit


def _config(**overrides):
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": False,
            "targets": [],
            "offline_notify": False,
            "platform_id": "default",
        },
        "monitors": {"enabled": False, "targets": [], "items": []},
        "daily_report": {
            "enabled": False,
            "weekly_enabled": False,
            "targets": [],
        },
    }
    for section, values in overrides.items():
        config[section].update(values)
    return config


def test_clean_config_has_no_issues() -> None:
    assert run_audit(_config()) == []


def test_monitors_without_targets_is_error() -> None:
    issues = run_audit(_config(monitors={"enabled": True}))
    errors = [issue for issue in issues if issue.code == "monitors_no_targets"]
    assert errors and all(issue.level == "error" for issue in errors)
    assert "推送目标" in errors[0].message


def test_alerts_without_targets_is_error() -> None:
    issues = run_audit(_config(alerts={"enabled": True}))
    assert any(
        issue.code == "alerts_no_targets" and issue.level == "error" for issue in issues
    )


def test_targets_fall_back_to_alerts_targets() -> None:
    config = _config(monitors={"enabled": True}, daily_report={"enabled": True})
    config["alerts"]["targets"] = ["123456"]
    assert run_audit(config) == []


def test_monitor_item_config_problems_are_warnings() -> None:
    issues = run_audit(
        _config(
            monitors={
                "enabled": True,
                "items": [
                    {"__template_key": "http", "name": "博客", "enabled": True},
                    {
                        "__template_key": "tcp",
                        "name": "游戏",
                        "port": 99999,
                        "enabled": True,
                    },
                    {
                        "__template_key": "tcp",
                        "name": "正常",
                        "port": 8765,
                        "enabled": True,
                    },
                ],
            }
        )
    )
    problems = [
        issue.message for issue in issues if issue.code == "monitor_item_config"
    ]
    assert len(problems) == 2
    assert any("博客" in message and "url" in message for message in problems)
    assert any("游戏" in message for message in problems)
    config_issues = [issue for issue in issues if issue.code == "monitor_item_config"]
    assert all(issue.level == "warning" for issue in config_issues)


def test_daily_and_weekly_without_targets_warn() -> None:
    daily = run_audit(_config(daily_report={"enabled": True}))
    assert any(issue.code == "daily_report_no_targets" for issue in daily)
    weekly = run_audit(_config(daily_report={"weekly_enabled": True}))
    assert any(issue.code == "weekly_report_no_targets" for issue in weekly)


def test_monitor_instance_config_errors_are_used() -> None:
    from typing import ClassVar

    class _Monitor:
        items: ClassVar[list[dict[str, str]]] = [
            {"name": "X", "__template_key": "http", "config_error": "缺少 url 配置"},
        ]

    issues = run_audit(_config(monitors={"enabled": True}), monitor=_Monitor())
    messages = [
        issue.message for issue in issues if issue.code == "monitor_item_config"
    ]
    assert messages == ["监测项「X」（HTTP）缺少 url 配置"]


def test_disabled_monitor_items_are_not_reported() -> None:
    issues = run_audit(
        _config(
            monitors={
                "enabled": True,
                "items": [{"__template_key": "http", "name": "停用", "enabled": False}],
            }
        )
    )
    assert [issue for issue in issues if issue.code == "monitor_item_config"] == []
