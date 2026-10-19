from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from astrbot_plugin_statuscard import collectors
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.collectors import (
    _normalize_website_targets,
    build_network_chart,
    cfg_bool,
    cfg_int,
    uploaded_file_path,
)
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.state import SCHEMA_VERSION, StateStore
from astrbot_plugin_statuscard.t2i_renderer import (
    _auto_convert_unit,
    _br_filter,
    _percent_to_color,
    build_default_html,
)


# ---------------------------------------------------------------------------
# state store
# ---------------------------------------------------------------------------


def test_state_store_roundtrip_and_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.data["net"] = {"up_rate": 1.5}
    store.save()

    reloaded = StateStore(path)
    assert reloaded.data["schema_version"] == SCHEMA_VERSION
    assert reloaded.data["net"]["up_rate"] == 1.5
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600


def test_state_store_backs_up_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")

    store = StateStore(path)

    assert store.data.get("schema_version") == SCHEMA_VERSION
    backups = list(tmp_path.glob("state.json.bad.*"))
    assert backups, "损坏的状态文件应被备份"
    assert path.is_file(), "迁移后应重新生成可用的状态文件"


def test_state_store_migrates_legacy_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"net": {"month": "2026-10"}}), encoding="utf-8")

    store = StateStore(path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION
    assert store.data["net"]["month"] == "2026-10"


# ---------------------------------------------------------------------------
# collectors helpers
# ---------------------------------------------------------------------------


def test_build_network_chart_empty() -> None:
    chart = build_network_chart([])
    assert chart["empty"] is True
    assert chart["up_points"] == ""
    assert chart["x_labels"] == []


def test_build_network_chart_pads_to_one_hour() -> None:
    points = [[1_700_000_000 + i * 60, 1024.0, 2048.0] for i in range(10)]
    chart = build_network_chart(points)
    assert chart["empty"] is False
    # 10 samples are padded with 50 zero samples so the chart keeps a full
    # one-hour axis on a fresh install.
    assert len(chart["up_points"].split()) == 60
    assert chart["peak_up"] is not None


def test_build_network_chart_trims_to_last_sixty_points() -> None:
    points = [[1_700_000_000 + i * 60, float(i), float(i)] for i in range(180)]
    chart = build_network_chart(points)
    assert chart["empty"] is False
    assert len(chart["up_points"].split()) == 60
    # The last sample has the highest value in this fixture.
    assert chart["peak_up"]["text"].startswith("179")


def test_normalize_website_targets_defaults_and_clamping() -> None:
    defaults = _normalize_website_targets(None)
    assert [(x.name, x.url) for x in defaults] == [
        ("Baidu", "https://www.baidu.com/")
    ]

    sites = _normalize_website_targets(
        [
            {"name": "A", "url": "https://a.example", "timeout_seconds": 99},
            "https://b.example",
            {"name": "ignored", "url": ""},
        ]
    )
    assert [x.name for x in sites] == ["A", "https://b.example"]
    assert sites[0].timeout == 30.0
    assert sites[1].timeout == 3.0


def test_uploaded_file_path_only_accepts_plugin_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin_data"
    target = data_dir / "files" / "avatar" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"png")

    config = {"avatar": {"avatar_upload_files": ["files/avatar/a.png"]}}
    assert uploaded_file_path(config, "avatar.avatar_upload_files", data_dir) == target

    escaped = {"avatar": {"avatar_upload_files": ["files/../secret.png"]}}
    assert uploaded_file_path(escaped, "avatar.avatar_upload_files", data_dir) is None

    absolute = {"avatar": {"avatar_upload_files": ["/etc/passwd"]}}
    assert uploaded_file_path(absolute, "avatar.avatar_upload_files", data_dir) is None

    missing = {"avatar": {"avatar_upload_files": ["files/avatar/nope.png"]}}
    assert uploaded_file_path(missing, "avatar.avatar_upload_files", data_dir) is None


def test_cfg_helpers_ignore_bool_for_int() -> None:
    assert cfg_int({"a": {"b": "7"}}, "a.b", 0) == 7
    assert cfg_int({"a": {"b": True}}, "a.b", 5) == 5
    assert cfg_bool({"a": {"b": "yes"}}, "a.b", False) is True
    assert cfg_bool({"a": {"b": "off"}}, "a.b", True) is False


def test_collect_bot_info_runs_onebot_calls_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_call(client, action: str, timeout: float = 5.0):
        calls.append(action)
        await asyncio.sleep(0.2)
        return {
            "get_login_info": {"user_id": 10001, "nickname": "测试昵称"},
            "get_version_info": {"app_name": "NapCat.Onebot", "app_version": "4.0"},
            "get_friend_list": [{"user_id": 1}, {"user_id": 2}],
            "get_group_list": [{"group_id": 1, "member_count": 10}],
            "get_status": {"online": True},
        }[action]

    monkeypatch.setattr(
        collectors, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(collectors, "_call_action", fake_call)

    started = time.perf_counter()
    bot = asyncio.run(
        collectors.collect_bot_info(
            context=None,
            event=None,
            config={"alerts": {"platform_id": "test-concurrent"}},
            recv_state={"available": False},
        )
    )
    elapsed = time.perf_counter() - started

    assert set(calls) == {
        "get_login_info",
        "get_version_info",
        "get_friend_list",
        "get_group_list",
        "get_status",
    }
    assert bot["friends"] == 2
    assert bot["groups"] == 1
    assert bot["members"] == 10
    assert bot["online"] is True
    # Five 0.2s calls serialized would take about 1 second.
    assert elapsed < 0.8


# ---------------------------------------------------------------------------
# alert helpers
# ---------------------------------------------------------------------------


def test_normalize_targets_accepts_group_ids_and_umo() -> None:
    targets = AlertEngine._normalize_targets(
        ["123456", "default:GroupMessage:123456", " ", 42], "default"
    )
    assert targets == ["default:GroupMessage:123456"]


def test_format_komari_title_and_message_cleanup() -> None:
    text = AlertEngine._format_komari(
        {
            "title": "offline",
            "message": "节点：server-1\n消息：连接失败",
        }
    )
    assert "🔴" in text
    assert "事件: offline" in text
    assert "连接失败" in text
    assert "节点：" not in text


def test_format_komari_online_icon_and_fallback() -> None:
    assert "🟢" in AlertEngine._format_komari({"title": "online"})
    fallback = AlertEngine._format_komari(["not", "a", "dict"])
    assert "Komari 监控通知" in fallback


# ---------------------------------------------------------------------------
# service monitor
# ---------------------------------------------------------------------------


def _monitor(tmp_path: Path, items: list[dict]) -> ServiceMonitor:
    state = StateStore(tmp_path / "state.json")
    return ServiceMonitor(
        plugin=None,
        state=state,
        config={"monitors": {"enabled": True, "items": items}},
    )


def test_monitor_normalizes_supported_items(tmp_path: Path) -> None:
    monitor = _monitor(
        tmp_path,
        [
            {"__template_key": "http", "name": "站点", "url": "https://a.example"},
            {"__template_key": "tcp", "name": "端口", "host": "127.0.0.1", "port": 80},
            {"__template_key": "docker", "name": "容器", "container": "napcat"},
            {"__template_key": "process", "name": "进程", "pattern": "python"},
            {"__template_key": "systemd", "name": "服务", "unit": "nginx.service"},
        ],
    )
    assert len(monitor.items) == 5
    assert monitor.items[0]["__template_key"] == "http"


def test_monitor_warns_on_unknown_type_and_missing_field(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING", logger="astrbot_plugin_statuscard")
    monitor = _monitor(
        tmp_path,
        [
            {"__template_key": "ping", "name": "未知"},
            {"__template_key": "http", "name": "缺网址"},
            {"__template_key": "docker", "name": "禁用项", "enabled": False},
        ],
    )
    assert len(monitor.items) == 1
    assert monitor.items[0]["name"] == "缺网址"
    assert "不受支持" in caplog.text
    assert "缺少 url" in caplog.text
    assert "禁用项" not in caplog.text


def test_monitor_item_key_matches_target(tmp_path: Path) -> None:
    monitor = _monitor(
        tmp_path,
        [{"__template_key": "http", "name": "站点", "url": "https://a.example"}],
    )
    assert monitor._item_key(monitor.items[0]) == "http:站点:https://a.example"


# ---------------------------------------------------------------------------
# renderer helpers
# ---------------------------------------------------------------------------


def test_percent_to_color_thresholds() -> None:
    assert _percent_to_color(0) == "prog-low"
    assert _percent_to_color(69.9) == "prog-low"
    assert _percent_to_color(70) == "prog-medium"
    assert _percent_to_color(89.9) == "prog-medium"
    assert _percent_to_color(90) == "prog-high"


def test_auto_convert_unit() -> None:
    assert _auto_convert_unit(0) == "0B"
    assert _auto_convert_unit(1536) == "2KB"
    assert _auto_convert_unit(1536, with_space=True) == "2 KB"


def test_br_filter_escapes_html_before_adding_breaks() -> None:
    rendered = str(_br_filter("<script>alert(1)</script>\nnext"))
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert "<br />next" in rendered


def test_build_default_html_inlines_assets() -> None:
    view = {
        "generated_at": "2026-10-19 00:00:00",
        "bot": {
            "nick": "测试",
            "self_id": "10000",
            "friends": 1,
            "groups": 2,
            "members": 3,
            "recv": 4,
            "sent": 5,
            "recv_scope": "测试",
            "sent_scope": "测试",
            "online_text": "在线",
            "adapter": "aiocqhttp",
            "astrbot_version": "v4.29.0",
            "run_time": "00:00:01",
            "avatar_url": "",
        },
        "overview": [],
        "resources": [
            {"percent": 10, "title": "CPU", "desc": "test"},
            {"percent": 20, "title": "RAM", "desc": "test"},
            {"percent": 30, "title": "SWAP", "desc": "test"},
        ],
        "disks": [],
        "disk_io": {"read": "0 B/s", "write": "0 B/s"},
        "network": {
            "up": "0 B/s",
            "down": "0 B/s",
            "up_total": "0 B",
            "down_total": "0 B",
            "month": "Oct",
            "month_up": "0 B",
            "month_down": "0 B",
            "interface": "eth0",
            "chart": build_network_chart([]),
        },
        "websites": [],
        "process": {
            "counts": {
                "all": 1,
                "running": 1,
                "blocked": 0,
                "sleeping": 0,
                "unknown": 0,
            },
            "top": [],
        },
        "system_info": [],
        "uptime": "00:00:01",
        "footer": "test",
        "background_data_url": "",
        "avatar_data_url": "",
    }
    html = build_default_html(
        {"view": view},
        b"\xff\xd8\xff\xe0fakejpeg",
        "image/jpeg",
        avatar_bytes=b"\x89PNG\r\n\x1a\nfakepng",
        render_width=650,
    )
    assert "<style>" in html
    assert "data:image/jpeg;base64," in html
    assert "data:image/png;base64," in html
    assert "init-global.js" not in html
    assert "width=650" in html
