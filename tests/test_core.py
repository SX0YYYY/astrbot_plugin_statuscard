from __future__ import annotations

import asyncio
import json
import os
import time
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from astrbot_plugin_statuscard import collectors
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.collectors import (
    _load_sysinfo_cache,
    _normalize_website_targets,
    _save_sysinfo_cache,
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
from astrbot_plugin_statuscard.utils import (
    clean_report_sections,
    is_supported_platform,
    normalize_targets,
    retry_delay,
)

# ---------------------------------------------------------------------------
# state store
# ---------------------------------------------------------------------------

TZINFO = ZoneInfo("Asia/Shanghai")


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


def test_sampler_resets_month_traffic_on_rollover(tmp_path: Path, monkeypatch) -> None:
    """Monthly traffic is a natural-month counter, not a since-install one."""

    from datetime import datetime

    from astrbot_plugin_statuscard import sampler as sampler_module
    from astrbot_plugin_statuscard.sampler import Sampler

    store = StateStore(tmp_path / "state.json")
    now = time.time()
    store.data["net"] = {
        "month": "2026-09",
        "month_up": 12345,
        "month_down": 54321,
        "last_ts": now - 60,
        "last_sent": 1_000_000,
        "last_recv": 2_000_000,
    }
    sampler = Sampler(store, interval_seconds=60, config={"display": {}})
    monkeypatch.setattr(
        sampler_module,
        "now_in_timezone",
        lambda _config: datetime(2026, 10, 1, 0, 0, 30, tzinfo=TZINFO),
    )
    counters = types.SimpleNamespace(bytes_sent=1_050_000, bytes_recv=2_100_000)
    monkeypatch.setattr(sampler_module.psutil, "net_io_counters", lambda: counters)

    sampler._sample_network(now)

    net = store.get("net")
    assert net["month"] == "2026-10"
    assert net["month_up"] == 50_000
    assert net["month_down"] == 100_000


def test_normalize_website_targets_defaults_and_clamping() -> None:
    defaults = _normalize_website_targets(None)
    assert [(x.name, x.url) for x in defaults] == [("Baidu", "https://www.baidu.com/")]

    sites = _normalize_website_targets(
        [
            {"name": "A", "url": "https://a.example", "timeout_seconds": 99},
            "https://b.example",
            {"name": "ignored", "url": ""},
        ]
    )
    assert [x.name for x in sites] == ["A", "https://b.example"]
    assert sites[0].timeout == 15.0
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


def test_sysinfo_cache_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    _save_sysinfo_cache(store, {"ts": 123.0, "dpkg": 10, "pip": 20})
    assert _load_sysinfo_cache(store)["dpkg"] == 10
    assert _load_sysinfo_cache(store)["ts"] == 123.0
    assert _load_sysinfo_cache(None) == {}


def test_supported_platform_only_aiocqhttp() -> None:
    assert is_supported_platform("aiocqhttp")
    assert is_supported_platform("")
    assert is_supported_platform(None)
    assert not is_supported_platform("webchat")
    assert not is_supported_platform("telegram")


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
    targets = normalize_targets(
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


def _monitor(
    tmp_path: Path,
    items: list[dict],
    monitors_extra: dict | None = None,
) -> ServiceMonitor:
    state = StateStore(tmp_path / "state.json")
    monitors = {"enabled": True, "items": items}
    monitors.update(monitors_extra or {})
    return ServiceMonitor(plugin=None, state=state, config={"monitors": monitors})


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


def test_monitor_warns_on_invalid_tcp_port(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING", logger="astrbot_plugin_statuscard")
    monitor = _monitor(
        tmp_path,
        [{"__template_key": "tcp", "name": "坏端口", "port": 70000}],
    )
    assert len(monitor.items) == 1
    assert "1–65535" in caplog.text


def test_monitor_item_key_matches_target(tmp_path: Path) -> None:
    monitor = _monitor(
        tmp_path,
        [{"__template_key": "http", "name": "站点", "url": "https://a.example"}],
    )
    assert monitor._item_key(monitor.items[0]) == "http:站点:https://a.example"


def test_monitor_silence_window_parsing(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [], {"silence_enabled": True})
    monitor.config["monitors"]["silence_until"] = "2099-01-01 00:00"
    assert monitor._is_silenced() is True

    monitor.config["monitors"]["silence_until"] = "2000-01-01 00:00"
    assert monitor._is_silenced() is False

    monitor.config["monitors"]["silence_until"] = "not-a-date"
    assert monitor._silence_until() is None


def test_monitor_silence_freezes_alert_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = {
        "__template_key": "http",
        "name": "站点",
        "url": "https://a.example",
    }
    monitor = _monitor(
        tmp_path,
        [item],
        {"silence_enabled": True, "silence_until": "2099-01-01 00:00"},
    )

    async def fake_probe(_item):
        return False, "boom", "https://a.example"

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    asyncio.run(monitor._check_item(monitor.items[0]))

    state = monitor.state.get("monitors")["items"][monitor._item_key(item)]
    assert state.get("last_error") == "boom"
    assert state.get("fails", 0) == 0
    assert not state.get("alerting")


def test_monitor_suppresses_http_when_dependency_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = {
        "__template_key": "docker",
        "name": "NapCat 容器",
        "container": "napcat",
    }
    http = {
        "__template_key": "http",
        "name": "NapCat 端口",
        "url": "http://127.0.0.1:1/",
        "suppress_when_down": "NapCat 容器",
    }
    monitor = _monitor(tmp_path, [docker, http], {"suppress_duplicates": True})
    dep_key = monitor._item_key(docker)
    monitor.state.data.setdefault("monitors", {}).setdefault("items", {})[dep_key] = {
        "alerting": True
    }
    assert monitor._suppress_alert(monitor.items[1]) == "NapCat 容器"

    async def fake_probe(_item):
        return False, "connection refused", "http://127.0.0.1:1/"

    monkeypatch.setattr(monitor, "_probe", fake_probe)
    asyncio.run(monitor._check_item(monitor.items[1]))

    state = monitor.state.get("monitors")["items"][monitor._item_key(http)]
    assert not state.get("alerting")
    assert state.get("fails", 0) == 0


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


def _sample_view(
    compact: bool = False,
    system_info: list[dict] | None = None,
) -> dict:
    return {
        "generated_at": "2026-10-19 00:00:00",
        "compact": compact,
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
        "system_info": system_info or [],
        "uptime": "00:00:01",
        "footer": "test",
        "background_data_url": "",
        "avatar_data_url": "",
    }


def test_build_default_html_inlines_assets() -> None:
    view = _sample_view()
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


def test_build_default_html_compact_hides_system_info() -> None:
    view = _sample_view(
        compact=True,
        system_info=[{"label": "OS", "value": "Linux"}],
    )
    html = build_default_html(
        {"view": view},
        b"\xff\xd8\xff\xe0fakejpeg",
        "image/jpeg",
        render_width=650,
    )
    assert 'class="compact"' in html
    assert "系统信息" not in html
    assert "Linux" not in html


def test_clean_report_sections_removes_retired_todo() -> None:
    assert clean_report_sections(["resource", "todo", "qq"]) == [
        "resource",
        "qq",
    ]
    assert clean_report_sections(["resource", "qq"]) is None
    assert clean_report_sections(["todo"]) == []
    assert clean_report_sections(None) is None
    assert clean_report_sections("todo") is None


def test_retry_delay_short_backoff() -> None:
    assert retry_delay(1) == 30.0
    assert retry_delay(2) == 120.0
    assert retry_delay(3) == 600.0
    assert retry_delay(99) == 600.0


class _FailingPlugin:
    async def send_text(
        self, targets: list[str], text: str, source: str = "unknown"
    ) -> bool:
        return False


def test_alert_failure_uses_short_retry(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        }
    }
    engine = AlertEngine(_FailingPlugin(), state, config)
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["retry_count"] == 1
    assert 0 < rule["next_attempt"] - time.time() <= 31


def test_pending_recovery_survives_renewed_threshold(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.json")
    state.data["alerts"] = {
        "rules": {
            "cpu": {
                "pending_recovery": "✅ recovered",
                "recovery_retry_count": 1,
            }
        }
    }
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        }
    }
    engine = AlertEngine(_FailingPlugin(), state, config)
    asyncio.run(engine._evaluate("cpu", "CPU", 99.0, 85.0, "%", "99.0%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["pending_recovery"] == "✅ recovered"


class _DailyPlugin:
    def __init__(self) -> None:
        self.context = None
        self.images = 0
        self.texts = 0

    async def build_status_image(self) -> str:
        return "https://example.invalid/status.jpg"

    async def send_image(
        self, targets: list[str], url: str, source: str = "unknown"
    ) -> bool:
        self.images += 1
        return True

    async def send_text(
        self, targets: list[str], text: str, source: str = "unknown"
    ) -> bool:
        self.texts += 1
        return True

    async def build_summary_text(self) -> str:
        return "summary"


def test_daily_summary_sends_once_per_day(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "daily_summary": {"enabled": True, "with_image": True},
        },
        "display": {"timezone": "Asia/Shanghai"},
    }
    plugin = _DailyPlugin()
    engine = AlertEngine(plugin, state, config)
    assert asyncio.run(engine._send_daily_summary()) is True
    assert asyncio.run(engine._send_daily_summary()) is True
    assert plugin.images == 1


class _FakeReportStore:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def add_event(self, *args: object) -> None:
        self.events.append(args)


def test_monitor_url_query_redacted(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    store = _FakeReportStore()
    monitor = ServiceMonitor(None, state, {}, store)
    monitor._record_event(
        "New API",
        "http://example.com/health?token=secret#frag",
        "HTTP 200",
        "alert",
    )
    assert store.events
    _ts, _day, _kind, _name, _detail, _status, target = store.events[0]
    assert target == "http://example.com/health"
    assert "secret" not in target


def test_komari_message_truncated() -> None:
    text = AlertEngine._format_komari({"title": "alert", "message": "x" * 5000})
    assert len(text) <= 2000
    assert "已截断" in text


class _ReceiptClient:
    """OneBot client that answers like ``aiocqhttp`` (unwrapped data)."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0

    async def call_action(self, action: str, **kwargs: object) -> object:
        self.calls += 1
        return self.payload


class _SenderHarness:
    """Mirror of the plugin's real send-with-receipt decision logic."""

    def __init__(self, client: object) -> None:
        self.client = client
        self.fallback_calls = 0

    async def _send_chain(self, target: str, build_chain, label: str) -> bool:
        self.fallback_calls += 1
        return True

    async def run(self, targets: list[str]) -> tuple[bool, dict]:
        from astrbot_plugin_statuscard.onebot_receipt import send_direct

        ok_targets: list[str] = []
        receipts: dict[str, object] = {}
        for target in targets:
            receipt = await send_direct(
                self.client,
                target,
                [{"type": "text", "data": {"text": "x"}}],
            )
            receipts[target] = receipt
            if receipt.ok:
                ok_targets.append(target)
                continue
            if receipt.attempted:
                # OneBot answered with an error: never re-send.
                continue
            await self._send_chain(target, None, "消息")
            ok_targets.append(target)
        return bool(ok_targets), receipts


def test_direct_send_success_does_not_fall_back() -> None:
    client = _ReceiptClient({"message_id": 777})
    harness = _SenderHarness(client)
    ok, receipts = asyncio.run(harness.run(["default:GroupMessage:123456"]))
    assert ok is True
    assert client.calls == 1
    assert harness.fallback_calls == 0
    assert receipts["default:GroupMessage:123456"].message_id == 777


def test_direct_send_protocol_error_is_not_resent() -> None:
    class _FailingClient:
        async def call_action(self, action: str, **kwargs: object) -> object:
            class _ActionFailed(Exception):
                def __init__(self) -> None:
                    super().__init__("ActionFailed")
                    self.result = {
                        "status": "failed",
                        "retcode": 100,
                        "message": "blocked",
                    }

            raise _ActionFailed()

    harness = _SenderHarness(_FailingClient())
    ok, receipts = asyncio.run(harness.run(["default:GroupMessage:123456"]))
    assert ok is False
    # The message may already be in the chat, so it must not be sent again.
    assert harness.fallback_calls == 0
    assert receipts["default:GroupMessage:123456"].retcode == 100


def test_format_komari_without_message_keeps_known_fields_only() -> None:
    """An empty message must not dump the raw JSON payload into the group."""

    text = AlertEngine._format_komari(
        {"title": "alert", "node_name": "server-1", "ip": "10.0.0.8"}
    )
    assert "节点: server-1" in text
    assert "10.0.0.8" not in text
    assert "{" not in text


def test_disk_io_first_call_only_primes_baseline(monkeypatch) -> None:
    """The first disk_io() call must not average over the process lifetime."""

    from astrbot_plugin_statuscard import collectors

    monkeypatch.setattr(collectors, "_disk_io_primed", False)
    monkeypatch.setattr(collectors, "_last_disk_io", (time.time() - 3600, {}))
    assert collectors.disk_io() == []
    # Second call has a real baseline and returns whatever psutil reports.
    assert isinstance(collectors.disk_io(), list)


def test_monitor_flags_non_numeric_tcp_port(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-numeric port is a config error, not a recurring outage."""

    caplog.set_level("WARNING", logger="astrbot_plugin_statuscard")
    monitor = _monitor(
        tmp_path,
        [{"__template_key": "tcp", "name": "字符串端口", "port": "abc"}],
    )
    assert len(monitor.items) == 1
    assert monitor.items[0].get("config_error")
    assert "1–65535" in caplog.text


def test_alert_threshold_zero_disables_rule(tmp_path: Path) -> None:
    """threshold=0 must not fire on every sample nor stick in alerting."""

    state = StateStore(tmp_path / "state.json")
    state.data["alerts"] = {"rules": {"cpu": {"count": 5, "alerting": True}}}
    config = {
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        }
    }
    engine = AlertEngine(_FailingPlugin(), state, config)
    asyncio.run(engine._evaluate("cpu", "CPU", 3.2, 0.0, "%", "3.2%"))
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["count"] == 0
    assert rule["alerting"] is False
