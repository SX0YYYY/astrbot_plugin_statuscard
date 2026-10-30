"""v2.19.4 背景「顶部一次 + 渐隐主色底色」：主色提取与渐变 overlay。

模板注入的集成断言在 test_core.py 的 test_build_default_html_inlines_assets
（那里有模板渲染所需的完整 view）。
"""

from __future__ import annotations

from io import BytesIO

from astrbot_plugin_statuscard.bg_provider import fade_color_from_bytes
from astrbot_plugin_statuscard.t2i_renderer import _fade_overlay
from PIL import Image


def _png_bytes(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_fade_color_from_bytes_averages_image() -> None:
    # 单一纯色图的平均色即该色。
    assert fade_color_from_bytes(_png_bytes((78, 64, 60), (10, 10))) == (78, 64, 60)


def test_fade_color_from_bytes_rejects_garbage() -> None:
    assert fade_color_from_bytes(b"\xff\xd8\xff\xe0fakejpeg") is None
    assert fade_color_from_bytes(b"") is None


def test_fade_overlay_scales_with_display_height() -> None:
    # 650x812 的图按卡片宽 650 显示时高 812：起点 812*0.86=698、终点 812*1.21=983。
    html_overlay = _fade_overlay(_png_bytes((78, 64, 60), (650, 812)))
    assert "linear-gradient(180deg, rgba(78, 64, 60, 0) 698px" in html_overlay
    assert "rgb(78, 64, 60) 983px" in html_overlay


def test_fade_overlay_falls_back_on_undecodable_image() -> None:
    overlay = _fade_overlay(b"\xff\xd8\xff\xe0fakejpeg")
    # 无法解析时用中性兜底色，并按 650px 显示高度取渐变位置。
    assert "rgba(90, 84, 80, 0) 559px" in overlay
    assert "rgb(90, 84, 80) 786px" in overlay
