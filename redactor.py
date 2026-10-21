"""One place that decides what a rendered view is allowed to show.

The status card, the daily report and the Web dashboard each have their own
redaction switch, but they all hide the same four things: the host name,
the process list, the network figures and the QQ counters. Keeping the
rules in a single module makes it impossible for one surface to leak what
another one hides.

The rules are plain strings so they can be stored in the configuration and
compared case-insensitively.
"""

from __future__ import annotations

from typing import Any

from .utils import cfg_get

# The redaction rules the plugin understands.
RULES: tuple[str, ...] = ("hostname", "process", "network", "qq_stats")

# Placeholder shown wherever a value was removed.
HIDDEN = "已隐藏"
HIDDEN_NUMBER = "--"


def normalize_rules(raw: Any) -> set[str]:
    """Turn a configuration value into a set of rule names.

    Accepts a list, a single string or ``None`` so callers can hand over
    whatever the configuration happens to hold.
    """

    if raw is None:
        return set()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {
        str(item).strip().lower()
        for item in raw
        if str(item).strip() and str(item).strip().lower() in RULES
    }


def read_rules(config: Any, key: str = "display.redact") -> set[str]:
    """Read the rules of one surface from the plugin configuration.

    ``key`` is a dotted path such as ``display.redact`` or
    ``display.page_redact``. Every surface owns its switch outright: a
    missing or empty value means "hide nothing here", never "use another
    surface's rules".
    """

    return normalize_rules(cfg_get(config, key))


class Redactor:
    """Apply the redaction rules of one surface to the data it exposes."""

    def __init__(self, rules: Any = None):
        self.rules = normalize_rules(rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __contains__(self, rule: str) -> bool:
        return str(rule).strip().lower() in self.rules

    @property
    def hostname(self) -> bool:
        return "hostname" in self.rules

    @property
    def process(self) -> bool:
        return "process" in self.rules

    @property
    def network(self) -> bool:
        return "network" in self.rules

    @property
    def qq_stats(self) -> bool:
        return "qq_stats" in self.rules

    @property
    def sorted_rules(self) -> list[str]:
        return sorted(self.rules)

    # ------------------------------------------------------------------
    # status card
    # ------------------------------------------------------------------

    def apply_card(self, view: dict[str, Any]) -> dict[str, Any]:
        """Redact a collected status-card view in place."""

        if not self.rules:
            return view
        if self.hostname:
            for card in view.get("overview", []):
                if card.get("label") == "主机名":
                    card["value"] = HIDDEN
            for row in view.get("system_info", []):
                if row.get("label") == "Host":
                    row["value"] = HIDDEN
        if self.process:
            for proc in view.get("process", {}).get("top", []):
                proc["name"] = HIDDEN
            view.setdefault("process", {})["redacted"] = True
        if self.network:
            network = view.setdefault("network", {})
            for key in (
                "up",
                "down",
                "up_total",
                "down_total",
                "month_up",
                "month_down",
            ):
                network[key] = HIDDEN_NUMBER
            network["interface"] = HIDDEN
            network["chart"] = _empty_chart()
        if self.qq_stats:
            bot = view.setdefault("bot", {})
            for key in ("friends", "groups", "members", "recv", "sent"):
                bot[key] = None
        view["redacted"] = self.sorted_rules
        return view

    # ------------------------------------------------------------------
    # daily report
    # ------------------------------------------------------------------

    def apply_report(self, section: dict[str, Any], name: str) -> dict[str, Any]:
        """Redact one section of a daily-report view in place.

        The report renders its own sections, so the report knows which
        section it is and can be redacted precisely.
        """

        if not self.rules:
            return section
        if name == "qq" and self.qq_stats:
            section["available"] = False
            section["empty_reason"] = "已按脱敏配置隐藏 QQ 统计"
            for key in ("days", "labels"):
                section[key] = []
            for key in ("yesterday", "previous"):
                section[key] = {}
            section["reset_days"] = []
            section["sent_available"] = False
            section["snapshot_note"] = None
            section["recv_note"] = None
            section["charts"] = {}
            section["note"] = ""
        if name == "process" and self.process:
            for item in section.get("top", []) or []:
                if isinstance(item, dict):
                    item["name"] = HIDDEN
        return section

    # ------------------------------------------------------------------
    # web dashboard
    # ------------------------------------------------------------------

    def dashboard_host(self, hostname: str) -> str:
        """Return the host name the dashboard may show."""

        return HIDDEN if self.hostname else hostname


def _empty_chart() -> dict[str, Any]:
    """An empty network chart with the same shape as a filled one.

    The values mirror the empty chart the status-card collector produces,
    so the template can render both without special cases.
    """

    width = 560
    height = 150
    left = 54
    right = 10
    top = 12
    bottom = 24
    return {
        "width": width,
        "height": height,
        "left": left,
        "right": right,
        "top": top,
        "bottom": bottom,
        "plot_w": width - left - right,
        "plot_h": height - top - bottom,
        "up_points": "",
        "down_points": "",
        "up_area": "",
        "down_area": "",
        "y_labels": [],
        "x_labels": [],
        "peak_up": None,
        "peak_down": None,
        "empty": True,
    }
