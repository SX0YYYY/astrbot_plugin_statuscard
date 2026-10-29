"""v2.18.4：磁盘百分比口径统一 + 状态卡流量窗口标注。

Two display bugs found while reviewing a real 2.18.3 status card:

* The disk bar printed ``43.25 GB / 68.79 GB`` next to ``65.7%``.  psutil's
  ``percent`` divides by (used + avail) so it skips the filesystem's
  root-reserved blocks; the card therefore showed a percentage that did not
  match the two numbers beside it.  Both now use ``total``.
* ``上传量 / 下载量`` came from ``psutil.net_io_counters()`` (since boot) while
  ``本月流量`` accumulates across reboots (since the plugin started).  After a
  reboot the month figure can exceed the "total" one, which reads as a bug.
  The labels and the caption now state the two windows.
"""

from __future__ import annotations

import types

import jinja2
import pytest
from astrbot_plugin_statuscard import collectors, t2i_renderer

TEMPLATES = (
    __file__.rsplit("tests", 1)[0] + "templates/default/res/templates/macros.html.jinja"
)


def _load_macros() -> jinja2.Template:
    with open(TEMPLATES, encoding="utf-8") as fh:
        source = fh.read()
    env = jinja2.Environment(autoescape=False)
    # The same filters t2i_renderer registers for the real card render.
    env.filters.update(
        percent_to_color=t2i_renderer._percent_to_color,
        auto_convert_unit=t2i_renderer._auto_convert_unit,
        br=t2i_renderer._br_filter,
    )
    return env.from_string(source).make_module()


def _disk_context() -> dict:
    """A disk section as the collector builds it."""

    return {
        "disks": [
            {
                "path": "/",
                "used": "43.25 GB",
                "total": "68.79 GB",
                "percent": 62.9,
                "percent_text": "62.9%",
            }
        ],
        "disk_io": {"read": "1 KB/s", "write": "1 KB/s"},
    }


def _network_context() -> dict:
    return {
        "network": {
            "up": "34.9 KB/s",
            "down": "34.5 KB/s",
            "up_total": "39.01 GB",
            "down_total": "42.17 GB",
            "month": "10月",
            "month_up": "41.42 GB",
            "month_down": "47.18 GB",
            "interface": "eth0",
            "chart": {
                "width": 600,
                "height": 160,
                "left": 40,
                "right": 10,
                "y_labels": [{"y": 20, "text": "1 KB/s"}],
                "x_labels": [{"x": 100, "text": "19:00"}],
                "up_area": "",
                "down_area": "",
                "up_points": "1,2 3,4",
                "down_points": "1,2 3,4",
                "peak_up": None,
                "peak_down": None,
            },
        }
    }


def _header_context() -> dict:
    return {
        "bot": {
            "avatar_data_url": "",
            "nick": "bot",
            "astrbot_version": "4.29.3",
            "protocol_name": "NapCat",
            "protocol_version": "4.18.41",
            "run_time": "1 小时",
            "online_text": "在线",
            "friends": 1,
            "groups": 2,
            "members": 3,
            "recv": 4,
            "sent": 5,
            "recv_scope": "message_stats 跟踪群",
            "sent_scope": "platform_stats 全量",
        }
    }


# ---------------------------------------------------------------------------
# disk percentage
# ---------------------------------------------------------------------------


def test_disk_percent_matches_used_over_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """43.25 / 68.79 is 62.9%, not psutil's 65.7% (which drops reserved blocks)."""

    fake = types.SimpleNamespace(
        used=43_250_000_000,
        total=68_790_000_000,
        percent=65.7,
        free=22_600_000_000,
    )
    monkeypatch.setattr(collectors.psutil, "disk_usage", lambda _path: fake)

    disks = collectors.disk_usage(["/"])

    assert len(disks) == 1
    assert disks[0].percent == pytest.approx(62.9, abs=0.05)


def test_disk_percent_zero_total_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero-sized pseudo filesystem must not divide by zero."""

    fake = types.SimpleNamespace(used=0, total=0, percent=0.0, free=0)
    monkeypatch.setattr(collectors.psutil, "disk_usage", lambda _path: fake)

    disks = collectors.disk_usage(["/"])

    assert disks[0].percent == 0.0


# ---------------------------------------------------------------------------
# traffic window labels
# ---------------------------------------------------------------------------


def test_network_labels_say_cumulative_not_total() -> None:
    macros = _load_macros()
    html = macros.network(_network_context())

    assert "累计上传" in html
    assert "累计下载" in html
    # The old wording implied a lifetime total next to a month figure.
    assert "📤 上传量" not in html
    assert "📥 下载量" not in html


def test_scope_note_states_both_windows() -> None:
    macros = _load_macros()
    html = macros.header(_header_context())

    assert "累计流量按本次开机以来" in html
    assert "月流量按自然月累计（重启保留）" in html


def test_disk_row_prints_consistent_percentage() -> None:
    """The bar, the GB numbers and the label must describe the same ratio."""

    macros = _load_macros()
    html = macros.disk(_disk_context())

    assert "43.25 GB / 68.79 GB" in html
    assert "62.9%" in html
