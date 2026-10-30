"""v2.19.4 背景「顶部一次 + 渐隐主色底色」：主色提取与渐变 overlay。

v2.20.0：渐变位置随配置的卡片宽度缩放（此前硬编码 650px，用户调高
输出宽度时渐变会盖住图片下半段）；overlay/主色带缓存，重复渲染不再
重复解码；背景色同时作为 CSS background-color 兜底。模板注入的集成
断言在 test_core.py 的 test_build_default_html_inlines_assets（那里有
模板渲染所需的完整 view）。
"""

from __future__ import annotations

from io import BytesIO

from astrbot_plugin_statuscard.bg_provider import fade_color_from_bytes
from astrbot_plugin_statuscard.t2i_renderer import (
    FADE_FALLBACK_COLOR,
    _fade_overlay,
)
from PIL import Image


def _png_bytes(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_fade_color_from_bytes_averages_image() -> None:
    # 单一纯色图的平均色即该色。
    assert fade_color_from_bytes(_png_bytes((78, 64, 60), (10, 10))) == (78, 64, 60)


def test_fade_color_from_bytes_rejects_garbage() -> None:
    assert fade_color_from_bytes(b"\\xff\\xd8\\xff\\xe0fakejpeg") is None
    assert fade_color_from_bytes(b"") is None


def test_fade_overlay_scales_with_display_height() -> None:
    # 650x812 的图按卡片宽 650 显示时高 812：起点 812*0.86=698、终点 812*1.21=983。
    overlay, color = _fade_overlay(_png_bytes((78, 64, 60), (650, 812)), 650)
    assert "linear-gradient(180deg, rgba(78, 64, 60, 0) 698px" in overlay
    assert "rgb(78, 64, 60) 983px" in overlay
    assert color == "rgb(78, 64, 60)"


def test_fade_overlay_follows_card_width() -> None:
    # v2.20.0 修复：渐变位置按传入的卡片宽度缩放。同一张 650x812 图在
    # 1300 宽的卡片上显示高 1624，起点 1624*0.86=1397、终点 1624*1.21=1965。
    overlay, _ = _fade_overlay(_png_bytes((78, 64, 60), (650, 812)), 1300)
    assert "rgba(78, 64, 60, 0) 1397px" in overlay
    assert "rgb(78, 64, 60) 1965px" in overlay


def test_fade_overlay_falls_back_on_undecodable_image() -> None:
    overlay, color = _fade_overlay(b"\\xff\\xd8\\xff\\xe0fakejpeg", 650)
    # 无法解析时用中性兜底色，并按卡片宽度取渐变位置。
    assert "rgba(90, 84, 80, 0) 559px" in overlay
    assert "rgb(90, 84, 80) 786px" in overlay
    assert color == f"rgb{FADE_FALLBACK_COLOR}"


def test_fade_overlay_cache_hits_same_bytes() -> None:
    data = _png_bytes((10, 20, 30), (64, 64))
    first = _fade_overlay(data, 650)
    second = _fade_overlay(data, 650)
    assert first == second
