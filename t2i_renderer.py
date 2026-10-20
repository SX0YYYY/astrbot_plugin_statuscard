from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

import jinja2
from markupsafe import Markup, escape


ROOT = Path(__file__).parent
TPL_DIR = ROOT / "templates" / "default" / "res" / "templates"
CSS_FILE = ROOT / "templates" / "default" / "res" / "css" / "index.css"
REPORT_DIR = ROOT / "templates" / "report"
DEFAULT_AVATAR = ROOT / "res" / "assets" / "default_avatar.webp"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _detect_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    return "image/png"


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


def build_default_html(
    collected: dict[str, Any],
    bg_bytes: bytes,
    bg_mime: str = "image/jpeg",
    avatar_bytes: Optional[bytes] = None,
    render_width: int = 650,
) -> str:
    view = collected.setdefault("view", {})
    view["background_data_url"] = _data_url(bg_bytes, bg_mime)
    width = max(650, min(1600, int(render_width or 650)))

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
    for script in (
        '<script src="/js/init-global.js"></script>',
        '<script src="/js/lazy-load.js"></script>',
        '<script src="/js/load-plugin.js"></script>',
    ):
        index = index.replace(script, "")

    css = css.replace("width: 650px", f"width: {width}px")
    page_fix = f"html,body{{margin:0;padding:0;width:{width}px;}}"
    index = index.replace(
        '<link rel="stylesheet" href="/default/res/css/index.css" />',
        f"<style>\n{css}\n{page_fix}\n</style>",
    )

    tmpl = macros + "\n" + index
    env = jinja2.Environment(autoescape=jinja2.select_autoescape(["html", "xml"]))
    env.filters.update(
        percent_to_color=_percent_to_color,
        auto_convert_unit=_auto_convert_unit,
        br=_br_filter,
    )
    template = env.from_string(tmpl)
    return template.render(d=view)


def build_report_html(view: dict[str, Any], render_width: int = 650) -> str:
    """Render the daily-report template without any JavaScript dependency."""

    width = max(650, min(1600, int(render_width or 650)))
    css = _read_text(REPORT_DIR / "report.css").replace(
        "width: 650px", f"width: {width}px"
    )
    template_text = _read_text(REPORT_DIR / "index.html.jinja")
    template_text = template_text.replace("__REPORT_CSS__", css)
    template_text = template_text.replace(
        'content="width=650, initial-scale=1.0"',
        f'content="width={width}, initial-scale=1.0"',
    )
    env = jinja2.Environment(
        autoescape=jinja2.select_autoescape(["html", "xml"])
    )
    return env.from_string(template_text).render(d=view)
