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
    """The default dashboard switch hides nothing, whatever the card does.

    ``display.page_redact`` defaults to an empty list. That empty list is a
    real setting - it means the dashboard is fully visible - so the two
    surfaces never inherit each other's rules.
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
    assert api._redact() == set()
    assert api._host() != "已隐藏"


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


def test_page_redact_missing_key_hides_nothing() -> None:
    """Without a dashboard switch the dashboard stays fully visible."""

    api = PageApi(_ConfigPlugin({"display": {"redact": ["hostname", "process"]}}))
    assert api._redact() == set()


def test_page_redact_empty_list_hides_nothing() -> None:
    """An empty dashboard list means "hide nothing", not "follow the card"."""

    api = PageApi(
        _ConfigPlugin({"display": {"redact": ["hostname"], "page_redact": []}})
    )
    assert api._redact() == set()
    assert api._host() != "已隐藏"


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


def test_latest_processes_uses_live_snapshot(monkeypatch) -> None:
    """The dashboard must show the same ten-second snapshot as the card."""

    import asyncio

    from astrbot_plugin_statuscard import page_api

    monkeypatch.setattr(
        page_api,
        "process_info",
        lambda _top_n: {
            "top": [
                {"name": "python", "cpu_raw": 3.9, "mem_mb": 582.4},
                {"name": "qq", "cpu_raw": 199.1, "mem_mb": 201.0},
            ]
        },
    )
    api = PageApi(_ConfigPlugin({"display": {}}))
    items = asyncio.run(api._latest_processes(8))
    assert [item["name"] for item in items] == ["python", "qq"]
    assert [item["rank"] for item in items] == [1, 2]
    assert items[0]["cpu"] == 3.9
    assert items[0]["mem_mb"] == 582.4
    assert items[1]["cpu"] == 199.1


def test_latest_processes_redacts_names(monkeypatch) -> None:
    import asyncio

    from astrbot_plugin_statuscard import page_api

    monkeypatch.setattr(
        page_api,
        "process_info",
        lambda _top_n: {"top": [{"name": "qq", "cpu_raw": 1.0, "mem_mb": 2.0}]},
    )
    api = PageApi(_ConfigPlugin({"display": {"page_redact": ["process"]}}))
    items = asyncio.run(api._latest_processes(8))
    assert items[0]["name"] == "已隐藏"
