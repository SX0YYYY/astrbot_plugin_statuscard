from __future__ import annotations

from astrbot_plugin_statuscard.page_api import PageApi


def test_downsample_keeps_limit_and_edges() -> None:
    rows = [{"ts": index} for index in range(300)]
    sampled = PageApi._downsample(rows, 120)
    assert len(sampled) == 120
    assert sampled[0]["ts"] == 0
    assert sampled[-1]["ts"] == 299


def test_downsample_keeps_short_series() -> None:
    rows = [{"ts": index} for index in range(10)]
    assert PageApi._downsample(rows, 120) == rows


def test_network_view_redacts_rates_and_month() -> None:
    view = PageApi._network_view(
        True,
        {"up_rate": 1.5, "down_rate": 2.5, "month_up": 10, "month_down": 20},
    )
    assert view == {"redacted": True}


def test_network_view_keeps_fields_when_not_redacted() -> None:
    view = PageApi._network_view(
        False,
        {"up_rate": 1.5, "down_rate": 2.5, "month_up": 10, "month": "2026-10"},
    )
    assert view["up_rate"] == 1.5
    assert view["down_rate"] == 2.5
    assert view["month"] == "2026-10"


class _ConfigPlugin:
    def __init__(self, config: dict) -> None:
        self.config = config


def test_page_redact_uses_real_schema_defaults() -> None:
    """The fallback must work with the defaults AstrBot writes out.

    ``display.page_redact`` defaults to an empty list, not ``None``, so a
    check for ``None`` would never fall back to the card rules.
    """

    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    display_defaults = {
        key: value.get("default") for key, value in schema["display"]["items"].items()
    }
    assert display_defaults["page_redact"] == []
    assert display_defaults["redact"] == []

    api = PageApi(
        _ConfigPlugin(
            {
                "display": {
                    "redact": ["hostname", "process", "qq_stats"],
                    "page_redact": display_defaults["page_redact"],
                }
            }
        )
    )
    assert api._redact() == {"hostname", "process", "qq_stats"}
    assert api._host() == "已隐藏"


def test_page_redact_overrides_card_redact() -> None:
    api = PageApi(
        _ConfigPlugin(
            {
                "display": {
                    "redact": ["hostname", "process"],
                    "page_redact": ["network"],
                }
            }
        )
    )
    assert api._redact() == {"network"}


def test_page_redact_falls_back_to_card_redact() -> None:
    api = PageApi(_ConfigPlugin({"display": {"redact": ["hostname", "process"]}}))
    assert api._redact() == {"hostname", "process"}


def test_page_redact_empty_list_falls_back_to_card_redact() -> None:
    """An empty dashboard list means "follow the card", as the README says."""

    api = PageApi(
        _ConfigPlugin({"display": {"redact": ["hostname"], "page_redact": []}})
    )
    assert api._redact() == {"hostname"}
    assert api._host() == "已隐藏"


def test_page_redact_explicit_rules_win() -> None:
    """A non-empty dashboard list overrides the card rules."""

    api = PageApi(
        _ConfigPlugin(
            {"display": {"redact": ["hostname", "process"], "page_redact": ["network"]}}
        )
    )
    assert api._redact() == {"network"}
    # The card rules must not leak into the dashboard.
    assert api._host() != "已隐藏"
