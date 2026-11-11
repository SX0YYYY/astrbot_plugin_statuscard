"""Regression tests for the v2.20.23 audit batch.

Every test drives the production path that changed. The batch focuses on:

* a queued alert retry swallowed by the cooldown branch (silent for 30 min,
  one duplicate event row per sample, no recovery notice);
* dirty ``alerts``/``provider_watch`` state values aborting the sampling loop;
* a queued LLM notice surviving a rule shutdown without being saved;
* the RAM alert text putting the unit word on the wrong figure;
* TCP duplicates that only differ in spelling the default host;
* an edited monitor losing its suffixed state (second alert + orphan row);
* DNS/daemon failures on a previously working target being closed as
  "misconfiguration" instead of alerting;
* a localized docker/systemd output decoding to ``None`` and latching a fake
  outage;
* ``redact_url`` truncating free-form monitor details at ``#``/``?``;
* hostname redaction missing common TLDs;
* ``gethostname`` failing and dropping the whole status card.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import collectors as collectors_module
from astrbot_plugin_statuscard import local_card, provider_quality
from astrbot_plugin_statuscard import monitor as monitor_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.collectors import format_bytes, now_in_timezone
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import (
    PROBE_ERROR_PREFIX,
    ServiceMonitor,
)
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.utils import redact_url


class MemoryState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.saves = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def setdefault(self, key, default):
        return self.data.setdefault(key, default)

    @contextmanager
    def section(self, key, default=None):
        section = self.data.setdefault(key, default if default is not None else {})
        if not isinstance(section, dict):
            section = {}
            self.data[key] = section
        yield section

    def save(self) -> None:
        self.saves += 1


class ScriptedPlugin:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.context = None
        self.state = None

    async def send_text(self, targets, text, source="unknown"):
        self.calls.append(text)
        return DeliveryResult(ok=True, ok_targets=list(targets))


def _make_engine(tmp_path, config=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {
        "alerts": {
            "targets": ["default:GroupMessage:1"],
            "sustain_samples": 2,
            "cooldown_seconds": 1800,
        }
    }
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    engine = AlertEngine(plugin, state, cfg, store)
    return engine, state, store, plugin


def _make_monitor(tmp_path, items, config=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {
        "display": {"timezone": "Asia/Shanghai"},
        "monitors": {"enabled": True, "fail_threshold": 1, "items": items},
    }
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    monitor = ServiceMonitor(plugin, state, cfg, store, None)
    return monitor, state, store, plugin


# ----------------------------------------------------------------------
# 1) a due retry must not be swallowed by the cooldown branch
# ----------------------------------------------------------------------


def test_pending_retry_runs_when_due_inside_cooldown(tmp_path) -> None:
    engine, state, store, plugin = _make_engine(tmp_path)
    now = time.time()
    state.data["alerts"] = {
        "rules": {
            "cpu": {
                "alerting": True,
                "alerted": False,
                "label": "CPU 使用率",
                "last_sent": now - 10,
                "retry_count": 1,
                "next_attempt": now - 1,
                "alert_since": now - 120,
                "count": 2,
            }
        }
    }

    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))

    assert len(plugin.calls) == 1
    assert "系统告警" in plugin.calls[0]
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["alerted"] is True
    assert rule["retry_count"] == 0
    assert rule["next_attempt"] == 0
    day = now_in_timezone(engine.config).strftime("%Y-%m-%d")
    assert [row["status"] for row in store.list_events(day)] == ["alert"]


def test_first_episode_inside_cooldown_is_still_suppressed(tmp_path) -> None:
    """A brand-new episode inside the cooldown keeps the v2.18.1 behaviour."""

    engine, state, store, plugin = _make_engine(tmp_path)
    now = time.time()
    state.data["alerts"] = {
        "rules": {
            "cpu": {
                "alerting": False,
                "alerted": False,
                "label": "CPU 使用率",
                "last_sent": now - 10,
                "retry_count": 0,
                "next_attempt": 0,
                "count": 2,
            }
        }
    }

    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95.0%"))

    assert plugin.calls == []
    rule = state.data["alerts"]["rules"]["cpu"]
    assert rule["alerting"] is True
    day = now_in_timezone(engine.config).strftime("%Y-%m-%d")
    rows = store.list_events(day)
    assert len(rows) == 1
    assert "冷却窗口内仅记录" in rows[0]["detail"]


# ----------------------------------------------------------------------
# 2) dirty nested state must not break sampling or start-up
# ----------------------------------------------------------------------


def test_provider_watch_scalar_does_not_abort_sampling(tmp_path, monkeypatch) -> None:
    engine, state, _store, _plugin = _make_engine(
        tmp_path, {"alerts": {"provider_watch": {"enabled": True, "threshold": 3}}}
    )
    state.data["alerts"] = {"provider_watch": "on"}
    engine._provider_last_check = 0
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda *a, **k: ((0, "completed", 1, None)),
    )

    asyncio.run(engine._check_provider_quality())

    assert isinstance(state.data["alerts"]["provider_watch"], dict)


def test_alerts_list_does_not_break_close_provider_watch(tmp_path) -> None:
    engine, state, _store, _plugin = _make_engine(tmp_path)
    state.data["alerts"] = []

    # start() calls _close_provider_watch when the watch is off.
    engine._close_provider_watch("LLM 服务监控已关闭")

    assert state.data["alerts"] == []


def test_close_provider_watch_persists_queue_cleanup(tmp_path) -> None:
    engine, state, _store, _plugin = _make_engine(tmp_path)
    state.data["alerts"] = {
        "provider_watch": {
            "pending_recovery": "已恢复",
            "recovery_retry_count": 2,
            "pending_alert_text": "告警",
        }
    }

    engine._close_provider_watch("LLM 服务监控已关闭")

    watch = state.data["alerts"]["provider_watch"]
    assert "pending_recovery" not in watch
    assert "recovery_retry_count" not in watch
    assert "pending_alert_text" not in watch
    assert state.saves >= 1


# ----------------------------------------------------------------------
# 3) RAM alert text: "available" must own the unit word
# ----------------------------------------------------------------------


def test_ram_value_text_puts_available_before_total() -> None:
    mem = SimpleNamespace(
        percent=93.0,
        available=259.15 * 1024 * 1024,
        total=3.64 * 1024**3,
        used=3.39 * 1024**3,
    )

    text = alerts_module._ram_value_text(mem)

    assert text.startswith("93.0% ")
    assert f"({format_bytes(mem.available)} 可用 / {format_bytes(mem.total)})" in text
    assert "MB 可用 / 3.64 GB" in text


# ----------------------------------------------------------------------
# 4) TCP duplicates: an omitted host is 127.0.0.1
# ----------------------------------------------------------------------


def test_tcp_signature_normalises_the_default_host() -> None:
    implicit = {"__template_key": "tcp", "name": "Redis", "port": 6379}
    explicit = {
        "__template_key": "tcp",
        "name": "Redis",
        "host": "127.0.0.1",
        "port": 6379,
    }

    assert ServiceMonitor._probe_signature(implicit) == ServiceMonitor._probe_signature(
        explicit
    )


# ----------------------------------------------------------------------
# 5) editing a monitor that kept a suffixed state must migrate it
# ----------------------------------------------------------------------


def test_edited_monitor_migrates_its_suffixed_state(tmp_path) -> None:
    item = {
        "__template_key": "http",
        "name": "API",
        "url": "http://127.0.0.1:9/health",
        "keyword": "ok",
    }
    base = ServiceMonitor._item_key(item)
    old_key = f"{base}#ab12cd34"
    state = MemoryState()
    state.data["monitors"] = {
        "items": {
            old_key: {
                "alerting": True,
                "alerted": True,
                "since": 123.0,
                "name": "API",
                "target": "http://127.0.0.1:9/health",
            }
        }
    }
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")

    monitor = ServiceMonitor(
        plugin,
        state,
        {
            "display": {"timezone": "Asia/Shanghai"},
            "monitors": {"enabled": True, "items": [item]},
        },
        store,
        None,
    )

    assert len(monitor.items) == 1
    assert monitor.items[0].get("__key_suffix") is None
    items = state.data["monitors"]["items"]
    assert old_key not in items
    assert items[base]["alerting"] is True
    assert items[base]["since"] == 123.0


# ----------------------------------------------------------------------
# 6) DNS failure after a successful probe is an outage, not a typo
# ----------------------------------------------------------------------


def test_dns_failure_after_success_alerts_instead_of_closing(tmp_path) -> None:
    item = {
        "__template_key": "http",
        "name": "商城",
        "url": "http://shop.example/",
        "keyword": "ok",
    }
    monitor, state, _store, _plugin = _make_monitor(tmp_path, [item])
    key = ServiceMonitor._item_key(monitor.items[0])
    state.data["monitors"] = {
        "items": {key: {"ever_ok": True, "alerting": False, "fails": 0}}
    }

    async def fake_probe(_item):
        return (
            False,
            "配置错误: ConnectError: [Errno -2] Name or service not known",
            "http://shop.example/",
        )

    monitor._probe = fake_probe  # type: ignore[assignment]
    asyncio.run(monitor._check_item(monitor.items[0]))

    st = state.data["monitors"]["items"][key]
    assert not st["last_error"].startswith("配置错误")
    assert st["alerting"] is True


def test_dns_failure_on_a_new_target_stays_a_config_error(tmp_path) -> None:
    item = {
        "__template_key": "http",
        "name": "错拼",
        "url": "http://shopp.example/",
        "keyword": "ok",
    }
    monitor, state, _store, _plugin = _make_monitor(tmp_path, [item])
    key = ServiceMonitor._item_key(monitor.items[0])
    state.data["monitors"] = {"items": {key: {"alerting": False, "fails": 0}}}

    async def fake_probe(_item):
        return (
            False,
            "配置错误: ConnectError: [Errno -2] Name or service not known",
            "http://shopp.example/",
        )

    monitor._probe = fake_probe  # type: ignore[assignment]
    asyncio.run(monitor._check_item(monitor.items[0]))

    st = state.data["monitors"]["items"][key]
    assert st["last_error"].startswith("配置错误")
    assert not st.get("alerting")


# ----------------------------------------------------------------------
# 7) a decoded-to-None docker output must not become a fake outage
# ----------------------------------------------------------------------


def test_docker_probe_without_output_is_inconclusive(tmp_path, monkeypatch) -> None:
    monitor, _state, _store, _plugin = _make_monitor(
        tmp_path,
        [
            {
                "__template_key": "docker",
                "name": "web",
                "container": "web",
            }
        ],
    )
    monkeypatch.setattr(
        monitor_module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=None, stderr=None),
    )

    ok, detail, _target = asyncio.run(monitor._probe(monitor.items[0]))

    assert ok is False
    assert detail.startswith(PROBE_ERROR_PREFIX)


def test_systemd_probe_without_output_is_inconclusive(tmp_path, monkeypatch) -> None:
    monitor, _state, _store, _plugin = _make_monitor(
        tmp_path,
        [
            {
                "__template_key": "systemd",
                "name": "nginx",
                "unit": "nginx.service",
            }
        ],
    )
    monkeypatch.setattr(
        monitor_module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=None, stderr=None),
    )

    ok, detail, _target = asyncio.run(monitor._probe(monitor.items[0]))

    assert ok is False
    assert detail.startswith(PROBE_ERROR_PREFIX)


# ----------------------------------------------------------------------
# 8) redact_url keeps free-form monitor details intact
# ----------------------------------------------------------------------


def test_redact_url_keeps_non_url_text() -> None:
    assert redact_url("未找到匹配进程: python #1") == "未找到匹配进程: python #1"
    assert redact_url("python?") == "python?"
    assert redact_url("配置错误: python #2 未找到") == "配置错误: python #2 未找到"


def test_redact_url_still_strips_url_tokens() -> None:
    assert redact_url("example.com/api?token=abc") == "example.com/api"
    assert redact_url("http://user:pass@example.com/x?q=1") == "http://example.com/x"
    assert redact_url("internal:8080/health?token=x") == "internal:8080/health"
    assert redact_url("user:pass@host/api") == "host/api"


def test_redact_url_strips_a_host_only_query_or_fragment() -> None:
    # No path at all: these used to survive into state.json keys, report.db,
    # the dashboard and the audit log (2026-11-06 reverse audit).
    assert redact_url("example.com?token=abc") == "example.com"
    assert redact_url("internal:8080?token=x") == "internal:8080"
    assert redact_url("host#access_token=abc") == "host"


def test_redacted_process_key_does_not_steal_a_query_state(tmp_path) -> None:
    """A new item without a query must not adopt another item's ``#q`` slot."""

    plain = {"__template_key": "http", "name": "API", "url": "http://h/p"}
    base = ServiceMonitor._item_key(plain)
    other_key = f"{base}#qab12cd34"
    state = MemoryState()
    state.data["monitors"] = {
        "items": {
            other_key: {
                "alerting": True,
                "name": "API",
                "target": "http://h/p",
            }
        }
    }
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "r.db")

    ServiceMonitor(
        plugin,
        state,
        {"display": {}, "monitors": {"enabled": True, "items": [plain]}},
        store,
        None,
    )

    assert other_key in state.data["monitors"]["items"]
    assert base not in state.data["monitors"]["items"]


# ----------------------------------------------------------------------
# 9) hostname redaction covers common TLDs, not file names
# ----------------------------------------------------------------------


def test_redactor_hides_common_tlds() -> None:
    redactor = Redactor(["hostname"])

    assert redactor.mask_name("db01.corp.ai") == "已隐藏"
    assert redactor.mask_name("gitlab.company.cc") == "已隐藏"
    assert redactor.mask_name("api.svc.example") == "已隐藏"


def test_redactor_keeps_file_names() -> None:
    redactor = Redactor(["hostname"])

    assert redactor.mask_name("README.md") == "README.md"
    assert redactor.mask_name("alerts.enabled") == "alerts.enabled"


# ----------------------------------------------------------------------
# 10) gethostname failure degrades instead of dropping the card
# ----------------------------------------------------------------------


def test_gethostname_failure_returns_a_placeholder(monkeypatch) -> None:
    def boom():
        raise OSError("no host name")

    monkeypatch.setattr(collectors_module.socket, "gethostname", boom)

    assert collectors_module._gethostname_safe() == "--"


# ----------------------------------------------------------------------
# 11) provider conversations: UMO ids must not collapse into one row
# ----------------------------------------------------------------------


def test_provider_quality_separates_umo_conversations() -> None:
    now = int(time.time())
    created = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (created, "completed", "m", "aiocqhttp:GroupMessage:111111", now, now + 1, 0.2),
        (created, "completed", "m", "aiocqhttp:GroupMessage:222222", now, now + 1, 0.2),
    ]

    out = provider_quality.aggregate(rows)

    labels = {item["conversation"] for item in out["by_conversation"]}
    assert labels == {
        "aiocqhttp:GroupMessage:…1111",
        "aiocqhttp:GroupMessage:…2222",
    }


def test_provider_quality_keeps_the_uuid_prefix() -> None:
    now = int(time.time())
    created = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        (
            created,
            "completed",
            "m",
            "473972cd-1111-2222-3333-444455556666",
            now,
            now + 1,
            0.2,
        )
    ]

    out = provider_quality.aggregate(rows)

    assert out["by_conversation"][0]["conversation"] == "473972cd"


# ----------------------------------------------------------------------
# 12) a mistyped monitor type must reach the audit
# ----------------------------------------------------------------------


def test_audit_reports_an_unsupported_monitor_type() -> None:
    config = {
        "monitors": {
            "enabled": True,
            "targets": ["default:GroupMessage:1"],
            "items": [
                {"__template_key": "http", "name": "ok", "url": "http://127.0.0.1:9/"},
                {"__template_key": "htttp", "name": "typo", "url": "http://x/"},
            ],
        }
    }

    issues = run_audit(config)

    assert any("不受支持" in issue.message for issue in issues)


def test_audit_reports_items_skipped_by_the_monitor(tmp_path) -> None:
    items = [
        {"__template_key": "http", "name": "ok", "url": "http://127.0.0.1:9/"},
        {"__template_key": "htttp", "name": "typo", "url": "http://x/"},
    ]
    monitor, _state, _store, _plugin = _make_monitor(tmp_path, items)
    config = {
        "monitors": {
            "enabled": True,
            "targets": ["default:GroupMessage:1"],
            "items": items,
        }
    }

    issues = run_audit(config, monitor=monitor)

    assert any("不受支持" in issue.message for issue in issues)


def test_monitor_test_all_exposes_the_kind_key(tmp_path) -> None:
    monitor, _state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"__template_key": "http", "name": "ok", "url": "http://127.0.0.1:9/"}],
    )

    async def fake_probe(_item):
        return True, "HTTP 200", "http://127.0.0.1:9/"

    monitor._probe = fake_probe  # type: ignore[assignment]
    results = asyncio.run(monitor.test_all())

    assert results[0]["kind_key"] == "http"


# ----------------------------------------------------------------------
# 13) the local fallback card tolerates a dirty ring percentage
# ----------------------------------------------------------------------


def test_local_card_percent_value_tolerates_dirty_values() -> None:
    assert local_card._percent_value("abc") == 0.0
    assert local_card._percent_value([1]) == 0.0
    assert local_card._percent_value(None) == 0.0
    assert local_card._percent_value("42.5") == 42.5


# ----------------------------------------------------------------------
# 14) report: a same-second "failed" row must not hide the covering row
# ----------------------------------------------------------------------


def test_covering_row_survives_a_same_second_failed_row(tmp_path) -> None:
    tz = ZoneInfo("Asia/Shanghai")
    store = ReportStore(tmp_path / "cover.db")

    def ts(text: str) -> int:
        return int(
            datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp()
        )

    for status in ("alert", "failed"):
        when = "2026-11-03 12:53:43"
        store.add_event(
            ts(when), "2026-11-03", "service", "小辞API 域名", "detail", status, ""
        )
    recovery = ts("2026-11-05 04:53:43")
    store.add_event(
        recovery, "2026-11-05", "service", "小辞API 域名", "recovery", "recovery", ""
    )
    store.resolve_event(
        recovery, "小辞API 域名", recovery, kind="service", target="", close_all=True
    )

    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(
        day="2026-11-04", history_days=7, span_days=1, sections=["anomaly"]
    )

    assert view["anomalies"]["count"] == 1


# ----------------------------------------------------------------------
# 15) resource empty reason names the real first sample
# ----------------------------------------------------------------------


def test_resource_empty_reason_uses_the_real_first_sample(tmp_path) -> None:
    tz = ZoneInfo("Asia/Shanghai")
    store = ReportStore(tmp_path / "resources.db")
    early = int(datetime(2026, 10, 20, 0, 5, tzinfo=tz).timestamp())
    store.add_resource(early, 20.0, 50.0, 5.0, 60.0, 0.5)
    inside = int(datetime(2026, 11, 5, 12, 0, tzinfo=tz).timestamp())
    store.add_resource(inside, 20.0, 50.0, 5.0, 60.0, 0.5)

    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(
        day="2026-11-05", history_days=7, span_days=1, sections=["resource"]
    )

    assert "2026-10-20" in (view["resources"].get("empty_reason") or "")


# ----------------------------------------------------------------------
# 16) a far-future / malformed last_sent_day is discarded
# ----------------------------------------------------------------------


def test_future_last_sent_day_is_discarded(tmp_path) -> None:
    from astrbot_plugin_statuscard.report import DailyReporter

    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = {"display": {"timezone": "Asia/Shanghai"}}

    assert reporter._sanitize_sent_day("2030-12-31") is None
    assert reporter._sanitize_sent_day("not-a-date") is None
    assert reporter._sanitize_sent_day("2026-13-45") is None
    # A normal NTP rollback (one day ahead) keeps the guard.
    today = date.fromisoformat(reporter._today())
    assert (
        reporter._sanitize_sent_day((today + timedelta(days=1)).isoformat())
        == (today + timedelta(days=1)).isoformat()
    )
    assert (
        reporter._sanitize_sent_day((today - timedelta(days=3)).isoformat())
        == (today - timedelta(days=3)).isoformat()
    )
