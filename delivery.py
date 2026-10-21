from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeliveryResult:
    """Per-target outcome of one logical delivery."""

    ok: bool = False
    ok_targets: list[str] = field(default_factory=list)
    failed_targets: list[str] = field(default_factory=list)
