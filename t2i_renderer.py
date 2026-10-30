from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import jinja2
from markupsafe import Markup, escape

from .bg_provider import _detect_image_mime, fade_color_from_bytes

ROOT = Path(__file__).parent
TPL_DIR = ROOT / "templates" / "default" / "res" / "templates"
CSS_FILE = ROOT / "templates" / "default" / "res" / "css" / "index.css"
REPORT_DIR = ROOT / "templates" / "report"
DEFAULT_AVATAR = ROOT / "res" / "assets" / "default_avatar.webp"

# Neutral fallback when the background's average colour cannot be read.
FADE_FALLBACK_COLOR = (90, 84, 80)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _data_url(data: bytes, mime: str | None = None) -> str:
    mime = mime or _detect_image_mime(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _percent_to_color(percent: float) -> str:
    if percent < 70:
        return "prog-low"
    if percent < 90:
        return "prog-medium"
    return "prog-high"


def _auto_convert_unit(
    value: float,
    suffix: str = "",
    with_space: bool = False,
    unit_index: int | None = None,
) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    v = float(value or 0)
    while (unit_index is None) and v >= 1024 and idx < len(units) - 1:
        v /= 1024
        idx += 1
    if unit_index is not None:
        idx = unit_index
    sp = " " if with_space else ""
    return f"{v:.0f}{sp}{units[idx]}{suffix}"


def _br_filter(value: Any) -> Markup:
    if value is None:
        return Markup("")
    return Markup(str(escape(str(value))).replace("\n", "<br />"))


# The same background bytes are rendered on every card while the config
# stays unchanged; decoding + averaging the photo each time blocks the
# event loop twice per render (v2.19.4 regression). Cache the computed
# overlay keyed on a cheap fingerprint of the bytes.
_FADE_CACHE: tuple[tuple[tuple[int, int], int], str, str] | None = None


def _fade_fingerprint(bg_bytes: bytes, card_width: int) -> tuple[tuple[int, int], int]:
    # card_width is part of the key: the same photo must re-derive its
    # gradient when the user changes the configured output width.
    return ((len(bg_bytes), hash(bytes(bg_bytes[:512]))), card_width)


def _fade_overlay(bg_bytes: bytes, card_width: int = 650) -> tuple[str, str]:
    """Gradient that dissolves the single background photo into its own
    dominant tone below the fold (v2.19.4).

    The photo is shown once at full card width (no vertical tiling --
    tiling repeated portrait photos down the long page); under it a
    linear gradient fades to the image's average colour so the lower
    half of the card keeps the photo's palette without repeating it.
    Fade bounds scale with the photo's displayed height at *card_width*
    (the configured render width), not a hardcoded 650: without this the
    gradient ended mid-photo whenever the user raised the output width.

    Returns ``(overlay_gradient, solid_background_color)`` -- the colour
    doubles as the CSS ``background-color`` so the area below the photo
    never shows the renderer's default white even if the gradient math
    changes.
    """

    from io import BytesIO

    from PIL import Image

    global _FADE_CACHE
    fingerprint = _fade_fingerprint(bg_bytes, card_width)
    cached = _FADE_CACHE
    if cached is not None and cached[0] == fingerprint:
        gradient, color_text = cached[1], cached[2]
    else:
        color = fade_color_from_bytes(bg_bytes) or FADE_FALLBACK_COLOR
        try:
            with Image.open(BytesIO(bg_bytes)) as image:
                img_w, img_h = image.size
            display_h = max(1, round(card_width * img_h / max(1, img_w)))
        except Exception:
            display_h = card_width
        start = round(display_h * 0.86)
        end = round(display_h * 1.21)
        r, g, b = color
        gradient = (
            f"linear-gradient(180deg, rgba({r}, {g}, {b}, 0) {start}px, "
            f"rgb({r}, {g}, {b}) {end}px)"
        )
        color_text = f"rgb({r}, {g}, {b})"
        _FADE_CACHE = (fingerprint, gradient, color_text)
    return gradient, color_text


def build_default_html(
    collected: dict[str, Any],
    bg_bytes: bytes,
    bg_mime: str = "image/jpeg",
    avatar_bytes: bytes | None = None,
    render_width: int = 650,
) -> str:
    view = collected.setdefault("view", {})
    view["background_data_url"] = _data_url(bg_bytes, bg_mime)
    width = max(650, min(1600, int(render_width or 650)))
    overlay, bg_color = _fade_overlay(bg_bytes, width)
    view["background_overlay"] = overlay
    view["background_color"] = bg_color

    if avatar_bytes is None:
        try:
            avatar_bytes = DEFAULT_AVATAR.read_bytes()
        except Exception:
            avatar_bytes = None
    if avatar_bytes:
        view["avatar_data_url"] = _data_url(avatar_bytes)
        bot = view.setdefault("bot", {})
        bot["avatar_data_url"] = view["avatar_data_url"]
    else:
        view["avatar_data_url"] = ""
        view.setdefault("bot", {})["avatar_data_url"] = ""

    macros = _read_text(TPL_DIR / "macros.html.jinja")
    index = _read_text(TPL_DIR / "index.html.jinja")
    css = _read_text(CSS_FILE)

    lines = index.splitlines()
    if lines and lines[0].lstrip().startswith("{% from"):
        lines = lines[1:]
    index = "\n".join(lines)

    index = index.replace(
        'content="width=device-width, initial-scale=1.0"',
        f'content="width={width}, initial-scale=1.0"',
    )

    css = css.replace("width: 650px", f"width: {width}px")
    page_fix = f"html,body{{margin:0;padding:0;width:{width}px;}}"

    tmpl = macros + "\n" + index
    env = jinja2.Environment(autoescape=jinja2.select_autoescape(["html", "xml"]))
    env.filters.update(
        percent_to_color=_percent_to_color,
        auto_convert_unit=_auto_convert_unit,
        br=_br_filter,
    )
    template = env.from_string(tmpl)
    # CSS goes in as a template variable, not through a string replace: a
    # future rule containing ``{{`` or ``{#`` would otherwise be parsed as
    # Jinja syntax and break the render (same pattern as the report CSS).
    return template.render(d=view, page_css=css + "\n" + page_fix)


def build_report_html(
    view: dict[str, Any], render_width: int = 650, theme: str = "light"
) -> str:
    """Render the daily-report template without any JavaScript dependency."""

    width = max(650, min(1600, int(render_width or 650)))
    theme = "dark" if str(theme or "").strip().lower() == "dark" else "light"
    css = _read_text(REPORT_DIR / "report.css").replace(
        "width: 650px", f"width: {width}px"
    )
    template_text = _read_text(REPORT_DIR / "index.html.jinja")
    template_text = template_text.replace(
        '<html lang="zh-CN">',
        f'<html lang="zh-CN" data-theme="{theme}">',
    )
    template_text = template_text.replace(
        'content="width=650, initial-scale=1.0"',
        f'content="width={width}, initial-scale=1.0"',
    )
    env = jinja2.Environment(autoescape=jinja2.select_autoescape(["html", "xml"]))
    # CSS goes in as a template variable, not through a string replace: a
    # future rule containing ``{{`` or ``{#`` would otherwise be parsed as
    # Jinja syntax and break the render.
    return env.from_string(template_text).render(d=view, report_css=css)
