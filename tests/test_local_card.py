from __future__ import annotations

from astrbot_plugin_statuscard.local_card import render_fallback_card


def _sample_view() -> dict:
    return {
        "generated_at": "2026-10-25 18:30:00",
        "resources": [
            {"percent": 42.0, "title": "CPU"},
            {"percent": 57.4, "title": "RAM"},
            {"percent": 1.2, "title": "SWAP"},
        ],
        "disks": [
            {
                "path": "/",
                "percent": 42.0,
                "percent_text": "42.0%",
                "used": "80 GB",
                "total": "200 GB",
            }
        ],
        "network": {
            "up": "1.2 MB/s",
            "down": "8.4 MB/s",
            "month_up": "12.3 GB",
            "month_down": "45.6 GB",
        },
        "process": {
            "top": [
                {"name": "python", "cpu": "12.3%"},
                {"name": "nginx", "cpu": "3.1%"},
            ]
        },
    }


def test_renders_valid_png() -> None:
    png = render_fallback_card(_sample_view())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 5000


def test_renders_with_empty_view() -> None:
    """The fallback must survive missing sections; it is a degraded path."""

    png = render_fallback_card({})
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_renders_redacted_view() -> None:
    view = _sample_view()
    for proc in view["process"]["top"]:
        proc["name"] = "已隐藏"
    png = render_fallback_card(view)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
