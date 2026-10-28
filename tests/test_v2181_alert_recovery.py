"""v2.18.1: 冷却窗口内被抑制的故障不再单独发恢复通知。

2026-10-29 05:00 线上实测：内存 05:00:26 告警（已发）、05:00:56 恢复（已发）、
05:03:26 再次越阈值但在 30 分钟冷却内（只记事件、不发告警）、05:03:56 恢复时
又发了一条「已恢复」——用户看到两条恢复，但没有对应的第二条告警。

修复：规则增加 `alerted` 标记，只有真正通知过用户的故障才发恢复消息；
冷却抑制的故障仍然写入 alert/recovery 事件（日报口径不变）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.state import StateStore


class _RecordingPlugin:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown") -> bool:
        self.sent.append(text)
        return True


def _engine(tmp_path: Path, plugin: _RecordingPlugin) -> tuple[AlertEngine, StateStore]:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        }
    }
    return AlertEngine(plugin, state, config), state


def _sample(engine: AlertEngine, value: float) -> None:
    asyncio.run(engine._evaluate("mem", "内存", value, 85.0, "%", f"{value}%"))


def test_suppressed_episode_does_not_send_recovery(tmp_path: Path) -> None:
    plugin = _RecordingPlugin()
    engine, _state = _engine(tmp_path, plugin)

    _sample(engine, 91.0)  # 告警：发送
    _sample(engine, 41.0)  # 恢复：发送
    assert len(plugin.sent) == 2
    assert "已恢复" in plugin.sent[1]

    # 冷却窗口内再次越阈值：只记录事件，不发告警
    _sample(engine, 92.0)
    assert len(plugin.sent) == 2

    # 这次故障从未通知过用户，恢复时也不应该发「已恢复」
    _sample(engine, 42.0)
    assert len(plugin.sent) == 2
    rule = _state.data["alerts"]["rules"]["mem"]
    assert rule["alerting"] is False
    assert "alerted" not in rule


def test_normal_recovery_still_sends_after_new_episode(tmp_path: Path) -> None:
    """冷却过后再发生的故障仍按原逻辑发送告警与恢复。"""

    plugin = _RecordingPlugin()
    engine, state = _engine(tmp_path, plugin)

    _sample(engine, 91.0)
    _sample(engine, 41.0)
    # 手动把冷却时间推到过去，模拟冷却结束
    rule = state.data["alerts"]["rules"]["mem"]
    rule["last_sent"] = 0

    _sample(engine, 93.0)  # 新故障：发送告警
    assert len(plugin.sent) == 3
    _sample(engine, 40.0)  # 恢复：发送
    assert len(plugin.sent) == 4
    assert "已恢复" in plugin.sent[3]


def test_legacy_state_without_alerted_still_sends_recovery(tmp_path: Path) -> None:
    """升级前已经在 alerting 的规则（无 alerted 字段）恢复时仍应通知。"""

    plugin = _RecordingPlugin()
    engine, state = _engine(tmp_path, plugin)
    rule = (
        state.data.setdefault("alerts", {})
        .setdefault("rules", {})
        .setdefault("mem", {})
    )
    rule.update({"alerting": True, "alert_since": 0, "count": 0})

    _sample(engine, 40.0)
    assert len(plugin.sent) == 1
    assert "已恢复" in plugin.sent[0]
