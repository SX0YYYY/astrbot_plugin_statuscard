"""v2.20.0: 两轮独立找茬修复批的回归用例。

覆盖：背景渐变宽度适配与缓存（test_v2194 已有补充用例）、EXIF 转正、
像素上限、透明图白底合成、孤儿告警收尾（监测配置错误 / 磁盘挂载点消失）、
summary_buffered 生命周期、周报 attempt 标记与重试清理落盘、verify 错误
文本脱敏、OneBot retcode=1、clamp NaN、cfg_bool 空串、磁盘 IO 重启基线、
媒体迁移标记失败重试、共享 mask_umo、本地卡文本裁剪。
"""

from __future__ import annotations

import asyncio
import io
import math
from pathlib import Path

import astrbot_plugin_statuscard.collectors as collectors_module
import pytest
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.bg_provider import (
    MAX_IMAGE_PIXELS,
    fade_color_from_bytes,
    shrink_image_bytes,
)
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import _mask_umo
from astrbot_plugin_statuscard.sampler import Sampler
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import clamp_config, mask_umo
from PIL import Image

# ---------------------------------------------------------------------------
# 背景：EXIF / 像素上限 / 透明合成
# ---------------------------------------------------------------------------


def _jpeg_bytes(size: tuple[int, int], color, exif_orientation: int | None = None):
    img = Image.new("RGB", size, color)
    if exif_orientation is not None:
        exif = img.getexif()
        exif[274] = exif_orientation  # Orientation tag
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=exif.tobytes())
        return buf.getvalue()
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def test_shrink_honours_exif_orientation() -> None:
    # 40x20 横图 + Orientation=6（拍摄时旋转 90°）：正确输出应为 20 宽的竖图。
    raw = _jpeg_bytes((40, 20), (10, 20, 30), exif_orientation=6)
    shrunk, mime = shrink_image_bytes(raw, target_width=1170)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(shrunk)) as out:
        assert out.size[0] <= 1170
        assert out.size[1] > out.size[0]  # 竖图：高 > 宽


def test_shrink_rejects_huge_pixel_counts() -> None:
    # 一个尺寸声明超限的最小 PNG 即可触发预检（预检只看头部声明的尺寸）。
    class _FakeHeader(bytes):
        pass

    # 直接用 Pillow 造一张尺寸在声明上超限的图太耗时；用 monkeypatch 把
    # 上限临时压到 100 像素来验证预检路径。
    import astrbot_plugin_statuscard.bg_provider as bg

    data = _jpeg_bytes((20, 20), (1, 2, 3))
    original = bg.MAX_IMAGE_PIXELS
    try:
        bg.MAX_IMAGE_PIXELS = 100
        with pytest.raises(ValueError):
            shrink_image_bytes(data)
    finally:
        bg.MAX_IMAGE_PIXELS = original
    assert MAX_IMAGE_PIXELS == 60_000_000


def test_fade_color_composites_transparent_onto_white() -> None:
    # 全透明图：平均色应为白色（浏览器合成后的视觉色），而不是未合成的 0。
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert fade_color_from_bytes(buf.getvalue()) == (255, 255, 255)


def test_shrink_flattens_transparency_onto_white() -> None:
    img = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    shrunk, _ = shrink_image_bytes(buf.getvalue())
    with Image.open(io.BytesIO(shrunk)) as out:
        assert out.getpixel((0, 0)) == (255, 255, 255)


# ---------------------------------------------------------------------------
# 共享 mask_umo
# ---------------------------------------------------------------------------


def test_shared_mask_umo_matches_page_api_behaviour() -> None:
    assert (
        mask_umo("default:GroupMessage:461961169") == "default:GroupMessage:*******69"
    )
    assert _mask_umo("default:GroupMessage:461961169") == mask_umo(
        "default:GroupMessage:461961169"
    )


# ---------------------------------------------------------------------------
# 监测项：配置错误两个分支都要关闭在途告警
# ---------------------------------------------------------------------------


class _MonitorPlugin:
    def __init__(self) -> None:
        self.context = None
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown"):
        self.sent.append(text)
        return True


def _monitor(tmp_path: Path, items: list[dict], **cfg):
    state = StateStore(tmp_path / "state.json")
    config = {
        "monitors": {
            "enabled": True,
            "targets": ["123456"],
            "fail_threshold": 1,
            "cooldown_seconds": 0,
            "items": items,
            **cfg,
        }
    }
    plugin = _MonitorPlugin()
    mon = ServiceMonitor(plugin, state, config)
    return mon, state, plugin


def test_config_error_branch_closes_alerting_episode(tmp_path: Path) -> None:
    item = {"__template_key": "docker", "name": "容器", "container": "x"}
    mon, state, _plugin = _monitor(tmp_path, [item], recover_notify=True)
    # 预置在途告警
    with state.section("monitors") as monitors:
        monitors["items"] = {
            mon._item_key(item): {
                "name": "容器",
                "alerting": True,
                "alerted": True,
                "since": 1000.0,
            }
        }

    # 探测返回配置错误
    async def fake_probe(item):
        return False, "配置错误: Error: No such object", "容器"

    mon._probe = fake_probe  # type: ignore[method-assign]
    asyncio.run(mon._check_item(item))
    st = state.get("monitors")["items"][mon._item_key(item)]
    assert st["alerting"] is False
    # The episode was notified, so the close path pops the flag entirely.
    assert not st.get("alerted")
    assert not st.get("awaiting_summary")
    assert not st.get("summary_buffered")


def test_check_all_skipped_config_error_items_also_close(tmp_path: Path) -> None:
    item = {"__template_key": "docker", "name": "容器", "container": "x"}
    mon, state, _plugin = _monitor(tmp_path, [item])
    # _normalize_items copies the dict; flag the copy the monitor actually holds.
    mon.items[0]["config_error"] = "容器名未填"
    with state.section("monitors") as monitors:
        monitors["items"] = {
            mon._item_key(item): {
                "name": "容器",
                "alerting": True,
                "alerted": True,
                "since": 1000.0,
            }
        }
    asyncio.run(mon.check_all())
    st = state.get("monitors")["items"][mon._item_key(item)]
    assert st["alerting"] is False


def test_direct_send_success_clears_summary_buffered(tmp_path: Path) -> None:
    item = {"__template_key": "docker", "name": "同名", "container": "x"}
    mon, state, _plugin = _monitor(tmp_path, [item])

    async def ok_probe(item):
        return False, "容器 running=False", "同名"

    mon._probe = ok_probe  # type: ignore[method-assign]

    async def ok_send(text):
        return True

    mon._send = ok_send  # type: ignore[method-assign]
    with state.section("monitors") as monitors:
        monitors["items"] = {
            mon._item_key(item): {"name": "同名", "summary_buffered": True}
        }
    asyncio.run(mon._check_item(item))
    st = state.get("monitors")["items"][mon._item_key(item)]
    assert st.get("alerting") is True
    assert "summary_buffered" not in st  # 直发成功：残留标记必须被清掉


# ---------------------------------------------------------------------------
# 告警引擎：磁盘挂载点消失关闭在途告警
# ---------------------------------------------------------------------------


class _AlertsPlugin:
    def __init__(self) -> None:
        self.context = None
        self.sent: list[str] = []

    async def send_text(self, targets, text, source="unknown", kind=None):
        self.sent.append(text)
        return True

    def _hostname(self):
        return "test-host"


def test_disk_unreadable_closes_alerting_rule(tmp_path: Path, monkeypatch) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"disk_paths": ["/mnt/gone"]},
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "disk_threshold": 80,
            "sustain_samples": 1,
            "cooldown_seconds": 0,
        },
    }
    plugin = _AlertsPlugin()
    engine = AlertEngine(plugin, state, config)
    rules = state.setdefault("alerts", {}).setdefault("rules", {})
    # Single-mount config: the first mount keeps the historical "disk" key.
    rules["disk"] = {
        "alerting": True,
        "alerted": True,
        "alert_since": 1000.0,
        "count": 3,
    }
    import astrbot_plugin_statuscard.alerts as alerts_mod

    async def boom(paths):
        raise RuntimeError("挂载点不存在")

    monkeypatch.setattr(alerts_mod, "disk_usage_bounded", boom)
    asyncio.run(engine._sample_once())
    st = state.get("alerts")["rules"]["disk"]
    assert st["alerting"] is False
    assert st["count"] == 0
    assert not st.get("awaiting_summary")


# ---------------------------------------------------------------------------
# 周报：attempt 标记 + 重试清理落盘
# ---------------------------------------------------------------------------


def test_weekly_catch_up_accepts_attempt_marker(tmp_path: Path) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from astrbot_plugin_statuscard.report import DailyReporter

    tz = ZoneInfo("Asia/Shanghai")
    sunday = datetime(2026, 11, 1, 21, 40, tzinfo=tz)
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}
    reporter._weekly_last_sent_week = None
    reporter._weekly_attempt_week = "2026-W44"

    import astrbot_plugin_statuscard.report as report_mod

    original = report_mod.now_in_timezone
    report_mod.now_in_timezone = lambda _cfg: sunday
    try:
        assert reporter._weekly_catch_up_allowed() is True
    finally:
        report_mod.now_in_timezone = original


def test_clear_retry_counts_weekly_fields(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.report import DailyReporter

    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = {}
    reporter.state = StateStore(tmp_path / "state.json")
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_image_retry_report_day = None
    reporter._pending_image_retry_send_day = None
    reporter._weekly_pending = 123.0  # 只有 weekly 字段脏
    reporter._weekly_pending_report_day = None
    reporter._weekly_image_pending = None
    reporter._weekly_image_report_day = None
    reporter._save_delivery_state = lambda: None
    reporter._clear_retry(clear_weekly=True)
    assert reporter._weekly_pending is None


def test_clear_retry_default_leaves_weekly_schedule_alone(tmp_path: Path) -> None:
    from astrbot_plugin_statuscard.report import DailyReporter

    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = {}
    reporter.state = StateStore(tmp_path / "state2.json")
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    reporter._pending_image_retry_report_day = None
    reporter._pending_image_retry_send_day = None
    reporter._weekly_pending = 123.0  # 周报重试在排队
    reporter._weekly_pending_report_day = None
    reporter._weekly_image_pending = None
    reporter._weekly_image_report_day = None
    reporter._save_delivery_state = lambda: None
    reporter._clear_retry()  # 日报成功路径：不得动周报计划
    assert reporter._weekly_pending == 123.0


# ---------------------------------------------------------------------------
# OneBot 回执 / 投递分类 / 配置边界
# ---------------------------------------------------------------------------


def test_onebot_retcode_one_is_not_a_hard_failure() -> None:
    from astrbot_plugin_statuscard.onebot_receipt import extract_receipt

    receipt = extract_receipt({"status": "async", "retcode": 1, "data": {}})
    assert receipt.ok is True
    receipt2 = extract_receipt({"retcode": 1, "data": {}})
    assert receipt2.ok is True
    receipt3 = extract_receipt({"status": "failed", "retcode": 1})
    assert receipt3.ok is False


def test_clamp_config_rejects_nan_and_inf() -> None:
    assert clamp_config(float("nan"), 0, 100, "x") == 0
    assert clamp_config(float("inf"), 0, 100, "x") == 100
    assert clamp_config(float("-inf"), 0, 100, "x") == 0
    assert math.isnan(clamp_config(float("nan"), 0, 100, "x")) is False


def test_cfg_bool_empty_string_falls_back_to_default() -> None:
    assert collectors_module.cfg_bool({"y": ""}, "y", True) is True
    assert collectors_module.cfg_bool({"y": ""}, "y", False) is False
    assert collectors_module.cfg_bool({"y": "true"}, "y", False) is True


# ---------------------------------------------------------------------------
# 采样与本地卡
# ---------------------------------------------------------------------------


def test_disk_io_rebuilds_baseline_after_restart(tmp_path: Path) -> None:
    sampler = Sampler(StateStore(tmp_path / "state.json"), interval_seconds=15)
    with sampler.state.section("disk_io") as disk:
        disk["last_ts"] = 1000.0  # 远古基线（模拟重启后首轮）
        disk["last_read"] = 1
        disk["last_write"] = 1
    import time as _time

    sampler._sample_disk_io(_time.time())
    disk = sampler.state.get("disk_io")
    assert disk["read_rate"] == 0 and disk["write_rate"] == 0
    assert disk["last_ts"] > 1000.0


def test_clip_text_trims_to_width() -> None:
    from astrbot_plugin_statuscard.local_card import _clip_text

    img = Image.new("RGB", (400, 40), (255, 255, 255))
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    long_text = "月流量 ↑ 999999.99 GB ↓ 999999.99 GB 月流量 ↑ 999999.99 GB"
    clipped = _clip_text(draw, long_text, font, 100)
    assert clipped.endswith("…")
    assert len(clipped) < len(long_text)


# ---------------------------------------------------------------------------
# 媒体迁移：save_config 失败时必须回滚标记
# ---------------------------------------------------------------------------
