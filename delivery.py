from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class DeliveryResult:
    """Per-target outcome of one logical delivery."""

    ok: bool = False
    ok_targets: list[str] = field(default_factory=list)
    failed_targets: list[str] = field(default_factory=list)


async def deliver_targets(
    targets: list[str],
    sender: Callable[[str], Awaitable[bool]],
) -> DeliveryResult:
    """Call ``sender`` for every target and collect per-target results."""

    result = DeliveryResult()
    for target in targets:
        if await sender(target):
            result.ok_targets.append(target)
        else:
            result.failed_targets.append(target)
    result.ok = bool(result.ok_targets)
    return result
