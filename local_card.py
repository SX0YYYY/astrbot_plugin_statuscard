"""A local Pillow fallback card for when the remote T2I service is down.

The status card, the daily report and the weekly report all depend on the
remote HTML-to-image endpoint; when that fails the operator used to get a
plain text summary. This module draws a deliberately simple status card
(rings, disk bar, network figures, top processes) straight onto a Pillow
canvas instead, so a T2I outage degrades to a plain-looking image rather
than no image at all.

Scope is intentionally narrow: it is a reliability net, not a second
template system. Pillow is already a hard dependency of AstrBot itself, so
this adds no new requirement; everything drawn here must stay readable on
the fonts a bare system provides.
"""

from __future__ import annotations

import os
from datetime import datetime
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - tests stub astrbot.api
    import logging

    logger = logging.getLogger("astrbot")

WIDTH = 1170
PADDING = 48
BG = (15, 23, 42)  # slate-900
PANEL = (30, 41, 59)  # slate-800
TEXT = (226, 232, 240)  # slate-200
MUTED = (148, 163, 184)  # slate-400
ACCENT = (79, 140, 255)  # blue
GOOD = (46, 204, 113)
WARN = (245, 158, 11)
BAD = (239, 68, 68)
TRACK = (51, 65, 85)  # ring background

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

# Discovery order matches AstrBot's own local T2I strategy: a user-provided
# font first (AstrBot data dir), then the common CJK system fonts, then
# PIL's fallback. DejaVuSans is deliberately absent: it ships on most bare
# Linux servers but has no CJK glyphs, so "finding" it would render every
# Chinese label as tofu boxes.
_FONT_CANDIDATES = (
    "msyh.ttc",  # Windows
    "NotoSansCJK-Regular.ttc",  # Linux
    "NotoSansSC-Regular.otf",  # Linux (distro dependent name)
    "NotoSansSC-Regular.ttf",  # Linux (seen on Ubuntu with fonts-noto-sc)
    "wqy-zenhei.ttc",  # Linux (fonts-wqy-zenhei, Debian/Ubuntu default CJK)
    "msyhbd.ttc",  # Windows
    "PingFang.ttc",  # macOS
    "Heiti.ttc",  # macOS
)


def _custom_font_path() -> str | None:
    """AstrBot's conventional custom font location: <data dir>/font.ttf."""

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return os.path.join(get_astrbot_data_path(), "font.ttf")
    except Exception:
        return None


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached
    custom = _custom_font_path()
    attempts = [custom] if custom else []
    attempts.extend(_FONT_CANDIDATES)
    for name in attempts:
        try:
            font = ImageFont.truetype(name, size)
            _FONT_CACHE[size] = font
            return font
        except Exception:
            logger.debug("本地简化卡字体 %s 不可用，尝试下一个候选", name)
            continue
    try:
        # Pillow >= 10.1 supports sized default fonts; older bitmapped
        # fallbacks reject the argument, so fall back to the plain call.
        font = ImageFont.load_default(size=size)
    except TypeError:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _percent_color(percent: float) -> tuple[int, int, int]:
    if percent < 70:
        return GOOD
    if percent < 90:
        return WARN
    return BAD


def _text_center(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    color: tuple[int, int, int],
) -> None:
    font = _font(size)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width = right - left
    height = bottom - top
    draw.text(
        (xy[0] - width / 2 - left, xy[1] - height / 2 - top),
        text,
        font=font,
        fill=color,
    )


def _ring(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    radius: int,
    percent: float,
    label: str,
) -> None:
    clamped = max(0.0, min(100.0, float(percent or 0.0)))
    bbox = (
        center[0] - radius,
        center[1] - radius,
        center[0] + radius,
        center[1] + radius,
    )
    width = 18
    draw.arc(bbox, start=0, end=360, fill=TRACK, width=width)
    if clamped > 0:
        draw.arc(
            bbox,
            start=-90,
            end=-90 + 3.6 * clamped,
            fill=_percent_color(clamped),
            width=width,
        )
    _text_center(draw, center, f"{clamped:.1f}%", max(28, int(radius * 0.5)), TEXT)
    _text_center(draw, (center[0], center[1] + radius + 28), label, 24, MUTED)


def _panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=20, fill=PANEL)


def render_fallback_card(view: dict[str, Any]) -> bytes:
    """Draw the simplified status card for a collected view; return PNG bytes."""

    height = 980
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)

    generated = str(view.get("generated_at") or "")
    if generated:
        try:
            generated = datetime.fromisoformat(generated).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    draw.text(
        (PADDING, 40),
        "服务器状态 · 简化版",
        font=_font(40),
        fill=TEXT,
    )
    if generated:
        note = f"{generated} · 本地渲染（T2I 服务不可用时的兜底图）"
    else:
        note = "本地渲染（T2I 服务不可用时的兜底图）"
    draw.text((PADDING, 96), note, font=_font(22), fill=MUTED)

    resources = view.get("resources") or []
    ring_y = 240
    ring_radius = 96
    ring_gap = 300
    ring_start_x = PADDING + ring_radius + 40
    for index, item in enumerate(resources[:3]):
        center = (ring_start_x + index * ring_gap, ring_y)
        _ring(
            draw,
            center,
            ring_radius,
            float(item.get("percent") or 0.0),
            str(item.get("title") or ""),
        )

    disk_y = 470
    _panel(draw, (PADDING, disk_y, WIDTH - PADDING, disk_y + 90))
    draw.text((PADDING + 24, disk_y + 18), "磁盘", font=_font(24), fill=MUTED)
    disks = view.get("disks") or []
    if disks:
        first = disks[0]
        percent = float(first.get("percent") or 0.0)
        bar_left = PADDING + 120
        bar_right = WIDTH - PADDING - 320
        draw.rounded_rectangle(
            (bar_left, disk_y + 30, bar_right, disk_y + 60),
            radius=15,
            fill=TRACK,
        )
        fill_right = int(
            bar_left + (bar_right - bar_left) * max(0.0, min(1.0, percent / 100))
        )
        if fill_right > bar_left:
            draw.rounded_rectangle(
                (bar_left, disk_y + 30, fill_right, disk_y + 60),
                radius=15,
                fill=_percent_color(percent),
            )
        label = f"{first.get('path', '')}  {first.get('percent_text', '--')}"
        detail = f"{first.get('used', '--')} / {first.get('total', '--')}"
        label_font = _font(22)
        detail_font = _font(18)
        _, _, label_right, _ = draw.textbbox((0, 0), label, font=label_font)
        _, _, detail_right, _ = draw.textbbox((0, 0), detail, font=detail_font)
        right_edge = WIDTH - PADDING - 24
        draw.text(
            (right_edge - label_right, disk_y + 14),
            label,
            font=label_font,
            fill=TEXT,
        )
        draw.text(
            (right_edge - detail_right, disk_y + 50),
            detail,
            font=detail_font,
            fill=MUTED,
        )
    else:
        draw.text(
            (PADDING + 120, disk_y + 30), "暂无磁盘数据", font=_font(22), fill=MUTED
        )

    network = view.get("network") or {}
    net_y = 590
    _panel(draw, (PADDING, net_y, WIDTH - PADDING, net_y + 90))
    draw.text((PADDING + 24, net_y + 18), "网络", font=_font(24), fill=MUTED)
    net_text = (
        f"↑ {network.get('up', '--')}   ↓ {network.get('down', '--')}"
        f"    月流量 ↑ {network.get('month_up', '--')} ↓ {network.get('month_down', '--')}"
    )
    draw.text((PADDING + 120, net_y + 28), net_text, font=_font(24), fill=TEXT)

    proc_y = 710
    _panel(draw, (PADDING, proc_y, WIDTH - PADDING, proc_y + 170))
    draw.text((PADDING + 24, proc_y + 18), "Top 进程", font=_font(24), fill=MUTED)
    top = (view.get("process") or {}).get("top") or []
    if top:
        for index, item in enumerate(top[:5]):
            row_y = proc_y + 60 + index * 22
            name = str(item.get("name") or "--")
            if len(name) > 40:
                name = name[:37] + "…"
            draw.text(
                (PADDING + 24, row_y),
                f"{index + 1}. {name}",
                font=_font(18),
                fill=TEXT,
            )
            draw.text(
                (WIDTH - PADDING - 140, row_y),
                str(item.get("cpu") or "--"),
                font=_font(18),
                fill=MUTED,
            )
    else:
        draw.text(
            (PADDING + 24, proc_y + 60), "暂无进程数据", font=_font(20), fill=MUTED
        )

    footer_y = height - 56
    draw.text(
        (PADDING, footer_y),
        "内容口径与脱敏规则与状态卡一致 · 由本地 Pillow 渲染生成",
        font=_font(20),
        fill=MUTED,
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
