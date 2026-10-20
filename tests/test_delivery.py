from __future__ import annotations

import asyncio

from astrbot_plugin_statuscard.delivery import deliver_targets


def test_deliver_targets_reports_partial_failure() -> None:
    async def sender(target: str) -> bool:
        return target != "failed"

    result = asyncio.run(deliver_targets(["ok-1", "failed", "ok-2"], sender))
    assert result.ok is True
    assert result.ok_targets == ["ok-1", "ok-2"]
    assert result.failed_targets == ["failed"]


def test_deliver_targets_reports_all_failed() -> None:
    async def sender(target: str) -> bool:
        return False

    result = asyncio.run(deliver_targets(["a", "b"], sender))
    assert result.ok is False
    assert result.ok_targets == []
    assert result.failed_targets == ["a", "b"]
