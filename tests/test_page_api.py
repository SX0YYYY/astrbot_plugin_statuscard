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


def test_page_redact_empty_list_overrides_card_redact() -> None:
    api = PageApi(
        _ConfigPlugin({"display": {"redact": ["hostname"], "page_redact": []}})
    )
    assert api._redact() == set()
    assert api._host() != "已隐藏"
