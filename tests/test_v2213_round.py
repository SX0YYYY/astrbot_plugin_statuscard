"""Regression tests for the v2.21.3 second full-process audit batch.

The batch fixed 8 P2 + 31 P3 findings (five independent audit passes plus a
reverse audit) and two long-standing known-limitation P3s (the offline alert
latch was only written to disk after the send, and a removed duplicate monitor
item left its keyed rows open until retention). Every test here drives the
production classes; source-level assertions are used only where a full
behavioural harness would need the AstrBot request context.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import collectors as collectors_module
from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.monitor import PROBE_ERROR_PREFIX, ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.provider_quality import fetch_recent, recent_streak
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import mask_chat_ids

CFG = {"display": {"timezone": "Asia/Shanghai"}}


class _Plugin:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or CFG
        self.state = None
        self.context = None


def _engine(tmp_path: Path, store: ReportStore | None = None) -> AlertEngine:
    return AlertEngine(
        _Plugin(),
        StateStore(tmp_path / "state.json"),
        {"alerts": {"enabled": True}, "display": {"timezone": "Asia/Shanghai"}},
        report_store=store,
    )


# ---------------------------------------------------------------------------
# A-P2-2: the offline detector's early returns must keep last_seen fresh
# ---------------------------------------------------------------------------


def test_offline_early_return_touches_last_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A day of "cannot tell" must not look like a day of not sampling.

    ``_mark_stale_rules`` reads ``last_seen`` to tell the two apart; the
    three early-return paths used to skip the write, so a restart during a
    long unknown-status window marked a still-alerting episode stale and
    swallowed its recovery notice.
    """

    engine = _engine(tmp_path)
    # Seed a latched offline episode so the stale mark would matter.
    engine.state.set(
        "alerts",
        {
            "offline": {
                "alerting": True,
                "since": time.time() - 3600,
                "last_seen": time.time() - 7200,
            }
        },
    )
    monkeypatch.setattr(alerts_module, "find_adapter", lambda *args, **kwargs: object())
    monkeypatch.setattr(alerts_module, "adapter_platform_name", lambda *a, **k: "qq")
    monkeypatch.setattr(alerts_module, "is_official_platform", lambda *a, **k: True)
    monkeypatch.setattr(alerts_module, "adapter_running", lambda *a, **k: None)

    asyncio.run(engine._check_offline())

    offline = engine.state.get("alerts", {}).get("offline", {})
    assert offline.get("last_seen"), offline
    assert time.time() - offline["last_seen"] < 5, offline


# ---------------------------------------------------------------------------
# A-P3-4: a silenced rule must tell the user it is now off
# ---------------------------------------------------------------------------


def test_threshold_zero_sends_close_notice(tmp_path: Path) -> None:
    """A red episode that silently disappears deserves a close message."""

    store = ReportStore(tmp_path / "report.db")
    engine = _engine(tmp_path, store)
    engine.state.set(
        "alerts",
        {
            "rules": {
                "cpu": {
                    "alerting": True,
                    "alerted": True,
                    "alert_since": time.time() - 120,
                }
            }
        },
    )
    calls: list[str] = []

    async def fake_send(text, *args, **kwargs):
        calls.append(text)
        from astrbot_plugin_statuscard.delivery import DeliveryResult

        return DeliveryResult(ok=True)

    engine._send = fake_send

    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 10.0, 0.0, "%", "10.0%"))

    rule = engine.state.get("alerts")["rules"]["cpu"]
    assert rule["alerting"] is False
    assert any("告警已关闭" in text for text in calls), calls
    assert any("规则已停用" in text for text in calls), calls


def test_threshold_zero_stays_silent_when_never_delivered(tmp_path: Path) -> None:
    """No alert ever reached the user, so a close notice would confuse."""

    store = ReportStore(tmp_path / "report.db")
    engine = _engine(tmp_path, store)
    engine.state.set(
        "alerts",
        {"rules": {"cpu": {"alerting": True, "alerted": False}}},
    )
    calls: list[str] = []

    async def fake_send(text, *args, **kwargs):
        calls.append(text)
        from astrbot_plugin_statuscard.delivery import DeliveryResult

        return DeliveryResult(ok=True)

    engine._send = fake_send

    asyncio.run(engine._evaluate("cpu", "CPU 使用率", 10.0, 0.0, "%", "10.0%"))

    assert calls == [], calls


def test_removed_rule_sends_close_notice(tmp_path: Path) -> None:
    """_prune_rules mirrors the threshold=0 branch for removed rules."""

    store = ReportStore(tmp_path / "report.db")
    engine = _engine(tmp_path, store)
    engine.state.set(
        "alerts",
        {"rules": {"cpu": {"alerting": True, "alerted": True}}},
    )
    calls: list[str] = []

    async def fake_send(text, *args, **kwargs):
        calls.append(text)
        from astrbot_plugin_statuscard.delivery import DeliveryResult

        return DeliveryResult(ok=True)

    engine._send = fake_send

    asyncio.run(engine._prune_rules({"load"}))

    assert any("规则已移除" in text for text in calls), calls


# ---------------------------------------------------------------------------
# C-M1: partial member_count omission must read as unknown, not 0
# ---------------------------------------------------------------------------


def _bot_groups(
    monkeypatch: pytest.MonkeyPatch,
    groups: list[dict],
    platform: str = "test-members-partial",
) -> dict:
    async def fake_call(client, action: str, timeout: float = 5.0):
        return {
            "get_login_info": {"user_id": 10001, "nickname": "n"},
            "get_version_info": {"app_name": "NapCat", "app_version": "4.0"},
            "get_friend_list": [],
            "get_group_list": groups,
            "get_status": {"online": True},
        }[action]

    monkeypatch.setattr(
        collectors_module, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(collectors_module, "_call_action", fake_call)
    return asyncio.run(
        collectors_module.collect_bot_info(
            context=None,
            event=None,
            config={"alerts": {"platform_id": platform}},
            recv_state={"available": False},
        )
    )


def test_group_members_partial_missing_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One group with the field must not zero-out the others."""

    bot = _bot_groups(
        monkeypatch,
        [
            {"group_id": 1, "member_count": 100},
            {"group_id": 2, "member_count": 50},
            {"group_id": 3},
            {"group_id": 4},
        ],
    )
    assert bot["groups"] == 4
    assert bot["members"] is None, bot


def test_group_members_bad_value_does_not_lose_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric count in one group must not erase the group count."""

    bot = _bot_groups(
        monkeypatch,
        [
            {"group_id": 1, "member_count": "abc"},
            {"group_id": 2, "member_count": 50},
        ],
        platform="test-members-badvalue",
    )
    assert bot["groups"] == 2
    assert bot["members"] is None, bot


# ---------------------------------------------------------------------------
# C-M2: the process probe needs a total bound like every other kind
# ---------------------------------------------------------------------------


def test_process_probe_total_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung scan returns a PROBE_ERROR instead of parking the round."""

    monitor = ServiceMonitor(
        _Plugin(),
        StateStore(tmp_path / "state.json"),
        {"monitors": {"enabled": True, "items": []}},
        report_store=None,
    )

    class _SlowIter:
        def __iter__(self):
            time.sleep(4)
            return iter(())

    monkeypatch.setattr(
        ServiceMonitor.__module__
        and __import__("astrbot_plugin_statuscard.monitor", fromlist=["psutil"]).psutil,
        "process_iter",
        lambda *a, **k: _SlowIter(),
    )

    started = time.perf_counter()
    ok, detail, _target = asyncio.run(
        monitor._probe(
            {
                "__template_key": "process",
                "name": "慢扫描",
                "pattern": "never",
                "timeout_seconds": 1,
            }
        )
    )
    elapsed = time.perf_counter() - started

    assert ok is False
    assert detail.startswith(PROBE_ERROR_PREFIX), detail
    assert elapsed < 10, elapsed


# ---------------------------------------------------------------------------
# E-P2-1 / E-P2-2: redaction completeness
# ---------------------------------------------------------------------------


def test_mask_chat_ids_handles_lowercase_hex() -> None:
    """A lowercase openid is the same identifier; mask it like the upper one."""

    text = "目标 Q官弗洛洛:GroupMessage:1b8c23d3e4f5678901234567890abcd 发送失败"
    masked = mask_chat_ids(text)
    assert "1b8c23d3e4f567890" not in masked, masked
    assert masked != text
    # The uppercase form still masks (no regression).
    upper = mask_chat_ids("ID 1B8C23D3E4F5678901234567890ABCD 失败")
    assert "1B8C23D3E4F567890" not in upper, upper


def test_hostname_suffix_gap_closed() -> None:
    """TLDs the old whitelist missed must be hidden like com/net are."""

    redactor = Redactor(["hostname"])
    for token in ("nas.edu", "server.vip", "app.asia", "www.gov.cn"):
        masked = redactor.mask_name(f"{token} 无法访问")
        assert token not in masked, (token, masked)
        assert "已隐藏" in masked or "已隐藏地址" in masked, (token, masked)
    # "host"/"name" were DROPPED again after the reverse audit: the field-name
    # shape (db.host, user.name) is far more common in diagnostic text than
    # the real .host/.name domains (reverse audit v2.21.3 #3).
    for token in ("my.host", "user.name"):
        assert token in redactor.mask_name(f"{token} 无法访问"), token


# ---------------------------------------------------------------------------
# E-P2-3: a dirty created_at row must not blind the provider streak
# ---------------------------------------------------------------------------


def test_provider_recent_drops_dirty_created_at(tmp_path: Path) -> None:
    """A non-date created_at sorts above every real timestamp in DESC order."""

    from astrbot_plugin_statuscard.provider_quality import error_streak

    db = tmp_path / "astrbot.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE provider_stats (created_at TEXT, status TEXT, start_time REAL)"
    )
    base = 1700000000
    for offset in range(4):
        connection.execute(
            "INSERT INTO provider_stats VALUES (?, ?, ?)",
            (
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(base + offset * 60)),
                "error",
                float(base + offset * 60),
            ),
        )
    # The REAL latest row succeeded; the dirty text row pretends an error and
    # would keep the streak alive forever.
    connection.execute(
        "INSERT INTO provider_stats VALUES (?, ?, ?)",
        (
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(base + 5 * 60)),
            "completed",
            0.0,
        ),
    )
    connection.execute("INSERT INTO provider_stats VALUES ('dirty-row', 'error', NULL)")
    connection.commit()

    recent = fetch_recent(connection, limit=20)
    assert all("dirty" not in created for created, _status in recent), recent
    assert error_streak(recent) == 0, recent

    streak, status, _count, _since = recent_streak(db, limit=20)
    assert status == "completed", (streak, status)
    assert streak == 0
    connection.close()


# ---------------------------------------------------------------------------
# E-P3-1 / E-P3-2: state.py backup freshness and strict JSON
# ---------------------------------------------------------------------------


def test_state_bak_holds_latest_write(tmp_path: Path) -> None:
    """After two saves, .bak must reflect the SECOND write, not the first."""

    store = StateStore(tmp_path / "state.json")
    store.set("round", 1)
    store.save()
    store.set("round", 2)
    store.save()

    bak = json.loads((tmp_path / "state.json.bak").read_text(encoding="utf-8"))
    assert bak.get("round") == 2, bak

    # An external corruption of state.json must restore the newest data.
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    restored = StateStore(tmp_path / "state.json")
    assert restored.get("round") == 2


def test_state_writes_strict_json(tmp_path: Path) -> None:
    """NaN/Infinity must not leak into state.json as invalid JSON."""

    store = StateStore(tmp_path / "state.json")
    store.set("bad", float("nan"))
    store.set("inf", float("inf"))
    store.save()

    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    # A strict parser (no NaN extension) must be able to read it.
    parsed = json.loads(raw, parse_constant=lambda c: pytest.fail(f"非严格 JSON: {c}"))
    assert parsed["bad"] is None
    assert parsed["inf"] is None


# ---------------------------------------------------------------------------
# L4: a removed duplicate closes its keyed rows without touching the survivor
# ---------------------------------------------------------------------------


def test_resolve_event_match_legacy_false_keeps_null_rows(tmp_path: Path) -> None:
    """match_legacy=False closes only keyed rows; NULL rows stay open."""

    store = ReportStore(tmp_path / "report.db")
    day = "2026-11-14"
    base = int(time.time()) - 600
    store.add_event(base, day, "service", "网关", "down", "alert", "t", item_key="k1")
    store.add_event(
        base + 10, day, "service", "网关", "legacy", "alert", "t", item_key=None
    )
    store.resolve_event(
        base + 100,
        "网关",
        base + 100,
        kind="service",
        target="t",
        close_all=True,
        item_key="k1",
        match_legacy=False,
    )
    rows = store.list_events_range(0, base + 200, limit=10)
    by_key = {(row.get("item_key")): row.get("resolved_ts") for row in rows}
    assert by_key["k1"] is not None, by_key
    assert by_key[None] is None, by_key


def test_prune_duplicate_closes_keyed_not_survivor(tmp_path: Path) -> None:
    """A removed item closes its own rows; the same-name survivor stays red.

    The duplicate shape the prune guards: two TCP items with the same
    name+probe target but different state keys (omitted host vs explicit
    127.0.0.1). The legacy recovery used to be skipped entirely; now the
    keyed rows of the removed item are closed precisely.
    """

    from astrbot_plugin_statuscard.state import StateStore as _State

    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        _Plugin(),
        _State(tmp_path / "state.json"),
        {"monitors": {"enabled": True, "items": []}},
        report_store=store,
    )
    survivor = {
        "__template_key": "tcp",
        "name": "网关",
        "host": "127.0.0.1",
        "port": 1234,
        "enabled": True,
    }
    removed = {
        "__template_key": "tcp",
        "name": "网关",
        "port": 1234,  # host omitted -> same probe target, different key
        "enabled": True,
    }
    monitor.items = [survivor]
    key_survivor = monitor._item_key(survivor)
    key_removed = monitor._item_key(removed)
    assert key_survivor != key_removed
    assert monitor._probe_target(survivor) == monitor._probe_target(removed)

    base = int(time.time()) - 900
    day = time.strftime("%Y-%m-%d", time.gmtime(base))
    store.add_event(
        base,
        day,
        "service",
        "网关",
        "down-a",
        "alert",
        "127.0.0.1:1234",
        item_key=key_survivor,
    )
    store.add_event(
        base + 5,
        day,
        "service",
        "网关",
        "down-b",
        "alert",
        "127.0.0.1:1234",
        item_key=key_removed,
    )
    monitor.state.set(
        "monitors",
        {
            "items": {
                key_survivor: {
                    "alerting": True,
                    "name": "网关",
                    "target": "127.0.0.1:1234",
                },
                key_removed: {
                    "alerting": True,
                    "name": "网关",
                    "target": "127.0.0.1:1234",
                },
            }
        },
    )

    monitor._prune_state()

    states = monitor.state.get("monitors", {}).get("items", {})
    assert key_removed not in states, states
    assert key_survivor in states, states
    rows = store.list_events_range(0, base + 1000, limit=10)
    survivor_row = next(r for r in rows if r.get("item_key") == key_survivor)
    removed_row = next(r for r in rows if r.get("item_key") == key_removed)
    assert survivor_row.get("resolved_ts") is None, survivor_row
    assert removed_row.get("resolved_ts") is not None, removed_row


# ---------------------------------------------------------------------------
# D-P2-1 / D-P2-2: dashboard API contract
# ---------------------------------------------------------------------------


def test_reports_day_error_carries_error_field() -> None:
    """The frontend reads ``error`` for a failed payload; keep both fields."""

    source = (Path(__file__).resolve().parents[1] / "page_api.py").read_text(
        encoding="utf-8"
    )
    for code in ("report_day_invalid", "report_day_out_of_range"):
        block_start = source.index(f'"{code}"')
        block = source[block_start : block_start + 260]
        assert '"error":' in block, code
        assert '"message":' in block, code


def test_webhook_verify_failure_no_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed probe must not start the 30s lockout, like the targets branch."""

    class _Alerts:
        _webhook_runner = object()  # "listening"

    class _VerifyPlugin:
        def __init__(self) -> None:
            self.config = {
                "alerts": {
                    "komari_webhook": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 1,  # unreachable -> immediate ConnectionError
                        "path": "/webhook",
                    }
                }
            }
            self.alerts = _Alerts()

    async def fake_request(name: str, default=None):
        return "webhook"

    monkeypatch.setattr(page_api_module, "_request_value", fake_request)

    api = PageApi(_VerifyPlugin())
    payload = asyncio.run(api.verify())
    data = (
        json.loads(payload.get_data(as_text=True))
        if hasattr(payload, "get_data")
        else payload
    )
    assert data.get("http_status") == 0, data
    # No cooldown was charged on the failure path.
    assert "webhook" not in api._verify_last, api._verify_last


# ---------------------------------------------------------------------------
# D-P3-1 / D-P3-4: bilingual wording contracts
# ---------------------------------------------------------------------------


def test_i18n_bilingual_placeholders_align() -> None:
    """Every zh-CN placeholder must survive into en-US (and vice versa)."""

    from astrbot_plugin_statuscard import i18n as i18n_module

    zh = i18n_module.MESSAGES["zh-CN"]
    en = i18n_module.MESSAGES["en-US"]
    assert set(zh) == set(en)
    for key, value in zh.items():
        zh_vars = set(re.findall(r"\{(\w+)\}", str(value)))
        en_vars = set(re.findall(r"\{(\w+)\}", str(en[key])))
        assert zh_vars <= en_vars, (key, sorted(zh_vars - en_vars))


def test_monitor_test_busy_bilingual() -> None:
    """The busy answer must not reuse the started-testing wording."""

    from astrbot_plugin_statuscard import i18n as i18n_module

    for lang in ("zh-CN", "en-US"):
        assert "monitor_test_busy" in i18n_module.MESSAGES[lang]
    main_src = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    assert main_src.count('tr(self.config, "monitor_testing")') == 1, (
        "busy 分支应改用 monitor_test_busy"
    )


# ---------------------------------------------------------------------------
# L1: the offline alert latches BEFORE the send await
# ---------------------------------------------------------------------------


def test_offline_alert_latches_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside the delivery must not replay the offline alert."""

    import astrbot_plugin_statuscard.alerts as am

    store = ReportStore(tmp_path / "report.db")
    engine = _engine(tmp_path, store)
    latch_on_disk: dict[str, object] = {}

    async def fake_send(text, *args, **kwargs):
        # Called DURING the delivery: the state file on disk must already
        # carry the latch, so a crash here cannot replay the alert.
        disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        latch_on_disk["alerting"] = bool(
            disk.get("alerts", {}).get("offline", {}).get("alerting")
        )
        from astrbot_plugin_statuscard.delivery import DeliveryResult

        return DeliveryResult(ok=True)

    engine._send = fake_send

    async def fake_call(client, action: str, timeout: float = 5.0):
        return {"online": False}

    monkeypatch.setattr(am, "_get_aiocqhttp_client", lambda *a, **k: object())
    monkeypatch.setattr(am, "_call_action", fake_call)

    samples = am.OFFLINE_ALERT_SAMPLES
    engine.state.set(
        "alerts",
        {"offline": {"count": samples - 1, "last_seen": time.time()}},
    )
    asyncio.run(engine._check_offline())

    assert latch_on_disk.get("alerting") is True, latch_on_disk
    offline = engine.state.get("alerts", {}).get("offline", {})
    assert offline.get("alerting") is True, offline


# ---------------------------------------------------------------------------
# Reverse-audit follow-ups for the v2.21.3 batch itself
# ---------------------------------------------------------------------------


def test_prune_duplicate_legacy_only_writes_nothing(tmp_path: Path) -> None:
    """A removed item whose episode predates item_key writes NO recovery.

    The keyed recovery would find nothing to close and surface in the daily
    report as an orphan "recovered" line next to the still-open legacy alert
    (reverse audit v2.21.3 #1).
    """

    from astrbot_plugin_statuscard.state import StateStore as _State

    store = ReportStore(tmp_path / "report.db")
    monitor = ServiceMonitor(
        _Plugin(),
        _State(tmp_path / "state.json"),
        {"monitors": {"enabled": True, "items": []}},
        report_store=store,
    )
    survivor = {
        "__template_key": "tcp",
        "name": "网关",
        "host": "127.0.0.1",
        "port": 1234,
        "enabled": True,
    }
    removed = {"__template_key": "tcp", "name": "网关", "port": 1234, "enabled": True}
    monitor.items = [survivor]
    key_removed = monitor._item_key(removed)

    base = int(time.time()) - 900
    day = time.strftime("%Y-%m-%d", time.gmtime(base))
    # Only a LEGACY row (item_key NULL) exists for the removed episode.
    store.add_event(
        base,
        day,
        "service",
        "网关",
        "down",
        "alert",
        "127.0.0.1:1234",
        item_key=None,
    )
    monitor.state.set(
        "monitors",
        {
            "items": {
                key_removed: {
                    "alerting": True,
                    "name": "网关",
                    "target": "127.0.0.1:1234",
                },
            }
        },
    )

    monitor._prune_state()

    rows = store.list_events_range(0, base + 1000, limit=10)
    recoveries = [r for r in rows if r.get("status") == "recovery"]
    assert recoveries == [], recoveries


def test_render_loop_skips_ts_zero_like_pairing_loop(tmp_path: Path) -> None:
    """A ts=0 row must not render as a phantom line nor inflate the count."""

    store = ReportStore(tmp_path / "report.db")
    day = "2026-11-14"
    base = int(time.time()) - 300
    # A dirty ts=0 row plus a real 5-minute outage that recovered.
    store.add_event(0, day, "service", "svc", "dirty", "alert", "t")
    store.add_event(base, day, "service", "svc", "down", "alert", "t")
    store.add_event(base + 300, day, "service", "svc", "ok", "recovery", "t")
    store.resolve_event(
        base + 300, "svc", base + 300, kind="service", target="t", close_all=True
    )

    view = DailyReportBuilder(store, CFG).build(day=day, span_days=1)
    anomalies = view.get("anomalies") or {}
    assert anomalies.get("count") == 1, anomalies
    for row in anomalies.get("items", []):
        assert row.get("start_time") != "01-01 08:00", row


def test_hostname_field_names_are_not_masked() -> None:
    """``name`` was dropped from the suffix set: user.name/db.host stay."""

    redactor = Redactor(["hostname"])
    for token in ("user.name", "db.host"):
        masked = redactor.mask_name(f"{token} 登录失败")
        assert token in masked, (token, masked)
    # Real domains with the newly added suffixes still get masked.
    for token in ("nas.edu", "server.vip", "app.asia"):
        masked = redactor.mask_name(f"{token} 无法访问")
        assert token not in masked, (token, masked)
