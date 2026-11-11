"""Regression tests for the v2.20.25 audit batch.

Every test drives the production path that changed. The batch focuses on:

* the provider watch keeping its offline backoff across episodes (the
  resource side already cleared it);
* a provider/offline recovery notice burning its retry budget while the
  channel was down and then giving up;
* an offline-blocked resource episode leaving a recovery event with no
  matching start row;
* the monitor push text leaking internal addresses while the hostname
  rule was on;
* the first monitor alert dating the outage at the push moment;
* a GBK page without a charset header failing a Chinese keyword forever;
* MAC addresses and CJK-glued host names confusing the redactor;
* several dashboard paths that turned a component error into a bare 500
  or charged a cooldown without sending;
* the report hiding a rule that recovered yesterday and broke again
  afterwards (the recovered_keys filter);
* the text report cutting a monitor name mid-word.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import astrbot_plugin_statuscard.alerts as alerts_module
import astrbot_plugin_statuscard.monitor as monitor_module
import astrbot_plugin_statuscard.page_api as page_api_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder, build_report_text

FIXED_DAY = "2026-11-05"


def _fixed_now(config):
    return dt.datetime(2026, 11, 5, 12, 0, 0)


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
        self.results: list = []
        self.sent: list[tuple[list[str], str, str]] = []
        # The offline check reads plugin.context before it can even ask for
        # a client; a plain attribute (None included) keeps that path alive.
        self.context = None

    def _pop(self):
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return DeliveryResult(ok=True)

    async def send_text(self, targets, text, source="unknown"):
        self.sent.append((list(targets), text, source))
        return self._pop()


def _offline() -> DeliveryResult:
    return DeliveryResult(ok=False, failed_targets=["t"], transport_offline=True)


def _ok() -> DeliveryResult:
    return DeliveryResult(ok=True)


def _make_engine(tmp_path, config=None, day_patch=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {
        "alerts": {"targets": ["default:GroupMessage:1"], "sustain_samples": 1}
    }
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    engine = AlertEngine(plugin, state, cfg, store)
    if day_patch is not None:
        day_patch.setattr(alerts_module, "now_in_timezone", _fixed_now)
    return engine, state, store, plugin


def _make_monitor(tmp_path, items, config=None, day_patch=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg: dict = {
        "display": {"timezone": "Asia/Shanghai"},
        "monitors": {
            "enabled": True,
            "fail_threshold": 1,
            "items": items,
            "targets": ["aiocqhttp:GroupMessage:1"],
        },
    }
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    monitor = ServiceMonitor(plugin, state, cfg, store, None)
    if day_patch is not None:
        day_patch.setattr(monitor_module, "now_in_timezone", _fixed_now)
    return monitor, state, store, plugin


def _event_statuses(store) -> list[str]:
    return [row.get("status") for row in store.list_events(FIXED_DAY)]


# ---------------------------------------------------------------------------
# F1: the provider watch clears its offline backoff when the episode ends.
# ---------------------------------------------------------------------------


def test_provider_episode_clears_offline_backoff(tmp_path, monkeypatch):
    """After the episode ends the next outage starts at the fast step."""

    engine, state, _store, _plugin = _make_engine(
        tmp_path,
        {"alerts": {"provider_watch": {"enabled": True, "threshold": 1}}},
    )
    streak = {"value": (1, "failed", True, None)}
    monkeypatch.setattr(
        alerts_module, "provider_recent_streak", lambda *a, **k: streak["value"]
    )
    engine.plugin.results = [_offline()] * 10
    state.data["alerts"] = {"provider_watch": {"alerting": False, "alerted": False}}

    async def run():
        engine._provider_last_check = 0.0
        await engine._check_provider_quality()  # alert fails offline
        watch = state.data["alerts"]["provider_watch"]
        assert watch.get("offline_retry_count") == 1
        # episode ends: the latest LLM call succeeded
        streak["value"] = (0, "completed", True, None)
        engine.plugin.results = [_ok()]
        engine._provider_last_check = 0.0
        await engine._check_provider_quality()
        watch = state.data["alerts"]["provider_watch"]
        assert watch.get("alerting") is False
        assert "offline_retry_count" not in watch

    asyncio.run(run())


def test_close_provider_watch_clears_offline_backoff(tmp_path):
    engine, state, _store, _plugin = _make_engine(
        tmp_path,
        {"alerts": {"provider_watch": {"enabled": True, "threshold": 1}}},
    )
    state.data["alerts"] = {
        "provider_watch": {
            "offline_retry_count": 5,
            "next_attempt": 1.0,
            "alerting": True,
            "alerted": True,
        }
    }
    engine._close_provider_watch("测试关闭")
    watch = state.data["alerts"]["provider_watch"]
    assert watch.get("alerting") is False
    assert "offline_retry_count" not in watch


# ---------------------------------------------------------------------------
# F2: a provider recovery notice survives a channel outage.
# ---------------------------------------------------------------------------


def test_provider_recovery_retry_survives_offline(tmp_path, monkeypatch):
    engine, state, _store, _plugin = _make_engine(
        tmp_path,
        {"alerts": {"provider_watch": {"enabled": True, "threshold": 1}}},
    )
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda *a, **k: ((0, "completed", True, None)),
    )
    watch = state.data.setdefault("alerts", {})["provider_watch"] = {
        "alerting": False,
        "pending_recovery": "✅ 已恢复 · LLM 服务",
        "recovery_retry_count": 1,
        "recovery_next_attempt": 0.0,
    }
    engine.plugin.results = [_offline()] * 10

    async def run():
        for _ in range(8):
            # The offline ladder set a future next_attempt; a retry only
            # happens once it is due.
            watch["recovery_next_attempt"] = 0.0
            engine._provider_last_check = 0.0
            await engine._check_provider_quality()

    asyncio.run(run())
    assert watch.get("pending_recovery"), "notice must stay queued"
    assert watch.get("recovery_offline_count", 0) >= 5
    assert watch.get("recovery_retry_count", 1) <= 2


# ---------------------------------------------------------------------------
# F3: an offline-blocked episode writes its start row.
# ---------------------------------------------------------------------------


def test_offline_first_failure_writes_failed_event(tmp_path, monkeypatch):
    engine, state, store, _plugin = _make_engine(tmp_path, day_patch=monkeypatch)
    state.set("alerts", {"rules": {}})
    engine.plugin.results = [_offline()] * 5

    async def run():
        await engine._evaluate("ram", "内存使用率", 95.0, 90.0, "%", "95.0%")

    asyncio.run(run())
    assert _event_statuses(store) == ["failed"]
    # And the value clearing during the outage now pairs with it.
    engine.plugin.results = [_ok()] * 3

    async def run2():
        await engine._evaluate("ram", "内存使用率", 10.0, 90.0, "%", "10.0%")

    asyncio.run(run2())
    assert _event_statuses(store) == ["failed", "recovery"]


# ---------------------------------------------------------------------------
# F4/F5: boundary branches clear the offline backoff too.
# ---------------------------------------------------------------------------


def test_threshold_zero_clears_offline_backoff(tmp_path):
    engine, state, _store, _plugin = _make_engine(tmp_path)
    state.set("alerts", {"rules": {}})
    engine.plugin.results = [_offline()] * 8

    async def run():
        await engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95%")
        for _ in range(4):
            state.data["alerts"]["rules"]["cpu"]["next_attempt"] = 0
            await engine._evaluate("cpu", "CPU 使用率", 95.0, 85.0, "%", "95%")
        await engine._evaluate("cpu", "CPU 使用率", 95.0, 0.0, "%", "95%")

    asyncio.run(run())
    st = state.data["alerts"]["rules"]["cpu"]
    assert "offline_retry_count" not in st


def test_mount_close_clears_offline_backoff(tmp_path, monkeypatch):
    engine, state, _store, _plugin = _make_engine(
        tmp_path, {"display": {"disk_paths": ["/__definitely_missing_mount__"]}}
    )
    state.set("alerts", {"rules": {}})
    key = "disk:/__definitely_missing_mount__"
    engine.plugin.results = [_offline()] * 5
    state.data["alerts"]["rules"][key] = {
        "alerting": True,
        "alerted": True,
        "alert_since": time.time() - 60,
        "offline_retry_count": 3,
        "next_attempt": 0.0,
        "count": 0,
        "retry_count": 0,
    }

    async def run():
        # The mount-unreadable close lives in the sampling pass, not in
        # _evaluate for a single rule. The online-state probe finds no
        # OneBot client and treats that as "reachable" (same as production).
        monkeypatch.setattr(
            alerts_module, "_get_aiocqhttp_client", lambda *a, **k: None
        )
        await engine._sample_once()

    asyncio.run(run())
    st = state.data["alerts"]["rules"][key]
    assert st.get("alerting") is False
    assert "offline_retry_count" not in st


# ---------------------------------------------------------------------------
# F6: the monitor push text obeys the hostname rule for target/detail.
# ---------------------------------------------------------------------------


def test_monitor_push_masks_target_and_detail(tmp_path, monkeypatch):
    monitor, _state, store, _plugin = _make_monitor(
        tmp_path,
        [{"type": "http", "name": "内部服务", "url": "http://192.168.1.5:8080/health"}],
        config={"display": {"redact": ["hostname"]}},
        day_patch=monkeypatch,
    )

    async def fake_probe(item):
        return False, "ConnectError: 192.168.1.5:8080 连接被拒绝", item.get("url")

    monitor._probe = fake_probe

    async def run():
        await monitor._check_item(monitor.items[0])

    asyncio.run(run())
    assert monitor.plugin.sent, "alert must be pushed"
    body = "\n".join(text for _t, text, _s in monitor.plugin.sent)
    assert "192.168.1.5" not in body
    assert "已隐藏" in body
    # The event log keeps the raw values (pairing + report masks on output).
    rows = store.list_events(FIXED_DAY)
    assert any("192.168.1.5" in str(row.get("detail") or "") for row in rows)


# ---------------------------------------------------------------------------
# F7: the config-error close still closes and writes (now outside the lock).
# ---------------------------------------------------------------------------


def test_config_error_close_still_closes_and_writes(tmp_path, monkeypatch):
    items = [{"type": "tcp", "name": "坏项", "port": 6379}]
    monitor, state, store, _plugin = _make_monitor(
        tmp_path, items, day_patch=monkeypatch
    )
    key = monitor._item_key(monitor.items[0])
    monitor.items[0]["config_error"] = "缺少 host"
    state.data.setdefault("monitors", {})["items"] = {
        key: {"alerting": True, "alerted": True, "since": time.time() - 30}
    }
    asyncio.run(monitor.check_all())
    st = state.data["monitors"]["items"][key]
    assert st.get("alerting") is False
    assert "recovery" in _event_statuses(store)


# ---------------------------------------------------------------------------
# F8: the outage starts at the FIRST failed probe.
# ---------------------------------------------------------------------------


def test_since_starts_at_first_failure(tmp_path, monkeypatch):
    now_box = [1000.0]
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"type": "tcp", "name": "redis", "host": "127.0.0.1", "port": 6399}],
        config={"monitors": {"fail_threshold": 3}},
    )
    monkeypatch.setattr(
        monitor_module, "time", SimpleNamespace(time=lambda: now_box[0])
    )
    mode = {"ok": False}

    async def fake_probe(item):
        if mode["ok"]:
            return True, "TCP ok", "127.0.0.1:6399"
        return False, "ConnectionRefusedError: x", "127.0.0.1:6399"

    monitor._probe = fake_probe
    key = monitor._item_key(monitor.items[0])

    async def run():
        await monitor._check_item(monitor.items[0])
        now_box[0] = 1060.0
        await monitor._check_item(monitor.items[0])
        now_box[0] = 1120.0
        await monitor._check_item(monitor.items[0])

    asyncio.run(run())
    st = state.data["monitors"]["items"][key]
    assert st.get("since") == 1000.0
    assert st.get("alerting") is True

    mode["ok"] = True
    now_box[0] = 1180.0

    async def recover():
        await monitor._check_item(monitor.items[0])

    asyncio.run(recover())
    texts = [text for _t, text, _s in monitor.plugin.sent if "服务恢复" in text]
    assert texts and "中断时长: 00:03:00" in texts[-1]


def test_config_error_clears_stale_since(tmp_path, monkeypatch):
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path, [{"type": "tcp", "name": "x", "port": 1}], day_patch=monkeypatch
    )
    key = monitor._item_key(monitor.items[0])
    state.data.setdefault("monitors", {})["items"] = {
        key: {"fails": 1, "since": 12345.0}
    }

    async def fake_probe(item):
        return False, "配置错误: 缺少 host", "x"

    monitor._probe = fake_probe

    async def run():
        await monitor._check_item(monitor.items[0])

    asyncio.run(run())
    st = state.data["monitors"]["items"][key]
    assert st.get("fails") == 0
    assert not st.get("since")


# ---------------------------------------------------------------------------
# F9: a GBK page without a charset header can still match the keyword.
# ---------------------------------------------------------------------------


class _FakeResponse:
    charset_encoding = None
    status_code = 200

    def __init__(self, body: bytes) -> None:
        self.headers: dict = {}
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _FakeStream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aenter__(self):
        return _FakeResponse(self._body)

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.is_closed = False

    def stream(self, *a, **k):
        return _FakeStream(self._body)

    async def aclose(self):
        return None


def test_gbk_keyword_without_charset(tmp_path):
    monitor, _state, _store, _plugin = _make_monitor(
        tmp_path,
        [
            {
                "type": "http",
                "name": "GBK 站",
                "url": "http://example/gbk",
                "keyword": "运行正常",
            }
        ],
    )
    gbk_body = "<html><meta charset=gbk>服务运行正常</html>".encode("gb18030")
    monitor._http_clients[(True, True)] = _FakeClient(gbk_body)

    async def run():
        return await monitor._probe(monitor.items[0])

    ok, detail, _target = asyncio.run(run())
    assert ok, detail


# ---------------------------------------------------------------------------
# F10 / D-P3-7: redactor boundaries.
# ---------------------------------------------------------------------------


def test_mac_address_survives_masking():
    redactor = Redactor({"hostname"})
    text = "网卡 eth0 的 MAC 是 00:1a:2b:3c:4d:5e，DHCP 正常"
    assert redactor.mask_address_tokens(text) == text
    assert redactor.mask_addresses(text) == text


def test_compact_ipv6_still_masked():
    redactor = Redactor({"hostname"})
    out = redactor.mask_address_tokens("上游 2001:db8::1 拒绝连接")
    assert "2001:db8::1" not in out
    assert "已隐藏" in out


def test_hex_host_port_still_masked():
    """A pure-hex host:port ("db:5432") is NOT an IPv6 candidate."""

    redactor = Redactor({"hostname"})
    assert "db:5432" not in redactor.mask_address_tokens("无法连接 db:5432")
    assert "db:5432" not in redactor.mask_name("db:5432")
    # MAC (>=3 colon-separated groups) stays exempt.
    text = "网卡 MAC 是 00:1a:2b:3c:4d:5e"
    assert redactor.mask_address_tokens(text) == text


def test_cjk_adjacent_hostname_fully_masked():
    redactor = Redactor({"hostname"})
    out = redactor.mask_address_tokens("主机nas.local.example.com超时")
    assert "nas" not in out and ".com" not in out


def test_mask_audit_strips_query_token():
    class _Issue:
        def __init__(self, message):
            self._message = message

        def as_dict(self):
            return {"message": self._message}

    plugin = SimpleNamespace(
        config={"display": {"page_redact": ["hostname"], "redact": ["hostname"]}}
    )
    api = PageApi.__new__(PageApi)
    api.plugin = plugin
    api._redactor_cache = None
    out = api._mask_audit(
        [_Issue("规则里有无法识别的类型：（已隐藏地址）/hook?token=TOKEN999")]
    )
    assert "TOKEN999" not in out[0]["message"]


# ---------------------------------------------------------------------------
# D-P3-2/3/4/5: dashboard robustness.
# ---------------------------------------------------------------------------


class _PagePlugin:
    def __init__(self) -> None:
        self.report_store = ReportStore(
            Path(tempfile.mkdtemp(prefix="sc_page_")) / "report.db"
        )
        self.config = {
            "display": {"timezone": "Asia/Shanghai"},
            "daily_report": {"history_days": 7},
        }
        self.state = MemoryState()
        self.monitor = None

    def audit_issues(self):
        return []


def test_probe_raise_degrades_instead_of_500(tmp_path, monkeypatch):
    api = PageApi(_PagePlugin())

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(api.plugin.report_store, "probe", boom)
    for coro in (api.history(), api.events(), api.reports(), api.export()):
        payload = asyncio.run(coro)
        assert payload is not None


def test_overview_dirty_process_row_no_500(tmp_path, monkeypatch):
    """Dirty pid/cpu values must not raise out of the overview helper."""

    def dirty(top_n):
        return {
            "top": [
                {"name": "p", "pid": "abc", "cpu_raw": "x", "mem_mb": 2.0},
                {"name": "q", "pid": 5, "cpu_raw": float("inf"), "mem_mb": None},
            ]
        }

    monkeypatch.setattr(page_api_module, "process_info", dirty)
    api = PageApi(_PagePlugin())
    rows = asyncio.run(api._latest_processes(8))
    assert rows[0]["cpu"] == 0.0
    assert rows[0]["mem_mb"] == 2.0
    assert rows[1]["cpu"] == 0.0
    assert rows[1]["mem_mb"] == 0.0


def test_verify_send_raise_no_cooldown(tmp_path, monkeypatch):
    class _RaisingPlugin(_PagePlugin):
        async def send_text(self, targets, text, source="unknown"):
            raise RuntimeError("channel down")

    api = PageApi(_RaisingPlugin())
    api.plugin.config = {
        "display": {},
        "alerts": {
            "targets": ["aiocqhttp:GroupMessage:1"],
            "platform_id": "aiocqhttp",
        },
    }

    async def body():
        return {"kind": "targets"}

    monkeypatch.setattr(
        page_api_module, "_request", SimpleNamespace(query={}, json=body)
    )

    async def run():
        return await api.verify()

    payload = asyncio.run(run())
    assert payload is not None and payload.get("ok") is False
    assert api._verify_last.get("targets") is None


def test_recovery_task_exception_consumed(tmp_path, monkeypatch):
    api = PageApi(_PagePlugin())

    def boom():
        raise RuntimeError("disk full")

    monkeypatch.setattr(api.plugin.report_store, "probe", lambda: False)
    monkeypatch.setattr(api.plugin.report_store, "ensure_ready", boom)

    async def run():
        gate = await api._db_unavailable_payload()
        assert gate and gate.get("db_unavailable")
        await asyncio.sleep(0.05)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# D-P3-8: audit accepts tuple kinds.
# ---------------------------------------------------------------------------


def test_audit_kinds_tuple_not_invalid():
    config = {
        "notify": {
            "rules": [
                {
                    "enabled": True,
                    "target": "aiocqhttp:GroupMessage:1",
                    "kinds": ("service",),
                }
            ]
        },
        "alerts": {"enabled": True},
        "monitors": {"enabled": False},
    }
    issues = run_audit(config)
    assert "notify_rule_invalid" not in [issue.code for issue in issues]


# ---------------------------------------------------------------------------
# C-P2-1: a rule that recovered yesterday and broke again afterwards must
# still show up as unresolved.
# ---------------------------------------------------------------------------


def test_unresolved_survives_yesterday_flap(tmp_path):
    store = ReportStore(tmp_path / "report.db")
    # CI runs in UTC while the business uses the configured timezone: build
    # the timestamps FROM the config timezone so the events land at the same
    # spot of the report window everywhere (the v2.20.21 lesson).
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    win_start = int(dt.datetime(2026, 11, 3, 0, 0, 0, tzinfo=tz).timestamp())
    flap_alert = win_start - 15 * 3600  # 11-02 09:00 in Asia/Shanghai
    flap_rec = flap_alert + 60
    relapse = win_start - 3 * 3600  # 11-02 21:00 in Asia/Shanghai
    store.add_event(flap_alert, "2026-11-02", "service", "Web", "抖动", "alert", "t")
    store.resolve_event(
        flap_rec, "Web", flap_rec, kind="service", target="t", close_all=True
    )
    store.add_event(
        flap_rec, "2026-11-02", "service", "Web", "持续 00:01:00", "recovery", "t"
    )
    store.add_event(relapse, "2026-11-02", "service", "Web", "复发未恢复", "alert", "t")
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day=dt.date(2026, 11, 3), span_days=1)
    anom = view.get("anomalies") or {}
    assert anom.get("unresolved", 0) >= 1
    items = anom.get("items") or []
    assert any(
        item.get("status_key") == "unresolved" or item.get("status") == "未恢复"
        for item in items
    )


# ---------------------------------------------------------------------------
# C-P3-1: the process section guards inf / absurd values.
# ---------------------------------------------------------------------------


def test_process_section_guards_inf(tmp_path):
    store = ReportStore(tmp_path / "report.db")
    ts = int(dt.datetime(2026, 11, 3, 12, 0, 0).timestamp())
    store.add_process_samples(
        ts,
        "cpu",
        [
            {"rank": 1, "name": "evil", "cpu": float("inf"), "mem_mb": 5.0},
            {"rank": 2, "name": "big", "cpu": 1e300, "mem_mb": 5.0},
            {"rank": 3, "name": "normal", "cpu": 3.0, "mem_mb": 5.0},
        ],
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(
        day=dt.date(2026, 11, 3),
        span_days=1,
        sections=[
            "resource",
            "anomaly",
            "qq",
            "token",
            "provider",
            "website",
            "process",
        ],
    )
    proc = view.get("process") or {}
    texts = [item.get("cpu_text", "") for item in (proc.get("cpu") or [])]
    assert texts
    assert all("inf" not in t.lower() and "e+" not in t.lower() for t in texts)


# ---------------------------------------------------------------------------
# C-P3-2: merging two partial snapshots recomputes "complete".
# ---------------------------------------------------------------------------


def test_monitor_fold_clears_recovery_offline_ladder(tmp_path, monkeypatch):
    """Folding a queued recovery into a new alert must drop its ladder."""

    now_box = [2000.0]
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"type": "tcp", "name": "redis", "host": "127.0.0.1", "port": 6399}],
        config={"monitors": {"fail_threshold": 1}},
    )
    import astrbot_plugin_statuscard.monitor as mon_mod

    # ``monkeypatch`` (not a bare MonkeyPatch) so the module-level ``time``
    # is restored: leaking a frozen clock into later tests made every
    # subsequent event write carry ts=2000 and its recovery unable to match
    # (2026-11-09 audit).
    monkeypatch.setattr(mon_mod, "time", SimpleNamespace(time=lambda: now_box[0]))
    mode = {"ok": False}

    async def fake_probe(item):
        if mode["ok"]:
            return True, "TCP ok", "127.0.0.1:6399"
        return False, "ConnectionRefusedError: x", "127.0.0.1:6399"

    monitor._probe = fake_probe
    key = monitor._item_key(monitor.items[0])
    items = state.data.setdefault("monitors", {}).setdefault("items", {})
    st = items[key] = {
        # A recovery notice from the PREVIOUS episode is queued and already
        # deep in the offline ladder.
        "pending_recovery": "✅ 服务恢复 · redis",
        "recovery_retry_count": 1,
        "recovery_next_attempt": now_box[0] + 3600.0,
        "recovery_offline_count": 4,
    }

    async def run():
        await monitor._check_item(monitor.items[0])

    asyncio.run(run())
    assert st.get("alerting") is True
    assert "pending_recovery" not in st
    assert "recovery_offline_count" not in st


def test_provider_fold_clears_recovery_offline_ladder(tmp_path, monkeypatch):
    """Same for the LLM watch: folding drops the offline ladder."""

    engine, state, _store, _plugin = _make_engine(
        tmp_path,
        {"alerts": {"provider_watch": {"enabled": True, "threshold": 1}}},
    )
    monkeypatch.setattr(
        alerts_module,
        "provider_recent_streak",
        lambda *a, **k: ((5, "failed", True, None)),
    )
    watch = state.data.setdefault("alerts", {})["provider_watch"] = {
        "pending_recovery": "✅ 已恢复 · LLM 服务",
        "recovery_retry_count": 1,
        "recovery_next_attempt": time.time() + 3600.0,
        "recovery_offline_count": 3,
    }
    engine.plugin.results = [_ok()]

    async def run():
        engine._provider_last_check = 0.0
        await engine._check_provider_quality()

    asyncio.run(run())
    assert watch.get("alerting") is True
    assert "pending_recovery" not in watch
    assert "recovery_offline_count" not in watch


def test_snapshot_merge_recomputes_complete(tmp_path):
    store = ReportStore(tmp_path / "report.db")
    store.record_snapshot("2026-11-03", 10, 5, 100, None, complete=False)
    store.record_snapshot("2026-11-03", None, None, None, 5000, complete=False)
    row = store.get_snapshot("2026-11-03")
    assert row is not None
    assert row.get("complete") in (1, True)
    assert row.get("recv_total") == 5000


# ---------------------------------------------------------------------------
# F11: the text report keeps the full monitor name.
# ---------------------------------------------------------------------------


def test_text_report_keeps_full_name(tmp_path):
    store = ReportStore(tmp_path / "report.db")
    ts = int(dt.datetime(2026, 11, 3, 8, 52, 0).timestamp())
    store.add_event(
        ts, "2026-11-03", "service", "New API 端口 3000", "HTTP 502", "alert", "t"
    )
    store.resolve_event(
        ts + 60,
        "New API 端口 3000",
        ts + 60,
        kind="service",
        target="t",
        close_all=True,
    )
    store.add_event(
        ts + 60,
        "2026-11-03",
        "service",
        "New API 端口 3000",
        "持续 00:01:00",
        "recovery",
        "t",
    )
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    view = builder.build(day=dt.date(2026, 11, 3), span_days=1)
    text = build_report_text(view)
    assert "New API 端口 3000" in text
    assert "端口 3 " not in text
