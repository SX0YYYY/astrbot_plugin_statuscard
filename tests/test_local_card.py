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


def test_font_prefers_custom_font_path(monkeypatch) -> None:
    """The AstrBot data-dir font must be tried BEFORE system candidates.

    On bare Linux servers DejaVu used to win the race while having no CJK
    glyphs, rendering every Chinese label as tofu boxes.
    """

    from astrbot_plugin_statuscard import local_card

    local_card._FONT_CACHE.clear()
    calls: list[str] = []

    class _FakeFont:
        pass

    def fake_truetype(name, size):
        calls.append(str(name))
        if "custom-font.ttf" in str(name):
            return _FakeFont()
        raise OSError("miss")

    monkeypatch.setattr(local_card, "_custom_font_path", lambda: "/tmp/custom-font.ttf")
    monkeypatch.setattr(local_card.ImageFont, "truetype", fake_truetype)
    font = local_card._font(16)
    assert calls and calls[0] == "/tmp/custom-font.ttf"
    assert isinstance(font, _FakeFont)
    local_card._FONT_CACHE.clear()


def test_font_skips_missing_custom_path(monkeypatch) -> None:
    """A missing custom font must fall through to load_default, not crash."""

    from astrbot_plugin_statuscard import local_card

    local_card._FONT_CACHE.clear()
    monkeypatch.setattr(local_card, "_custom_font_path", lambda: None)

    def always_miss(name, size):
        raise OSError("miss")

    monkeypatch.setattr(local_card.ImageFont, "truetype", always_miss)
    monkeypatch.setattr(
        local_card.ImageFont,
        "load_default",
        lambda size=None: "default-font",
    )
    assert local_card._font(16) == "default-font"
    local_card._FONT_CACHE.clear()
