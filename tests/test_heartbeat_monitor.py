"""Regression tests for v2.23.0: the heartbeat (Dead Man's Switch) probe.

The heartbeat kind reuses the service-monitor state machine end to end; the
tests below drive the production classes (normalizer, probe, check_item,
prune, audit, and the aiohttp listener) instead of reimplementing the
semantics. The state-machine pairing itself is untouched by design -- see
the AGENTS red lines.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiohttp
import pytest
from astrbot_plugin_statuscard import audit as audit_module
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.heartbeat import HeartbeatServer
from astrbot_plugin_statuscard.monitor import (
    KIND_LABELS,
    PROBE_ERROR_PREFIX,
    REQUIRED_FIELDS,
    ServiceMonitor,
)
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore


def _hb_item(
    name: str = "备份脚本",
    token: str = "tok-1",
    period: int = 60,
    grace: int = 60,
    **extra: object,
) -> dict:
    return {
        "__template_key": "heartbeat",
        "name": name,
        "token": token,
        "period_seconds": period,
        "grace_seconds": grace,
        "enabled": True,
        **extra,
    }


def _monitor(tmp_path: Path, items: list, **cfg: object) -> ServiceMonitor:
    monitors = {"enabled": True, "items": items, "targets": ["123456"]}
    monitors.update(cfg)
    return ServiceMonitor(
        None, StateStore(tmp_path / "state.json"), {"monitors": monitors}
    )


def _set_heartbeat(state: StateStore, name: str, **entry: object) -> None:
    with state.section("monitors") as monitors:
        heartbeat = monitors.setdefault("heartbeat", {})
        heartbeat[name] = dict(entry)


# ---------------------------------------------------------------------------
# normalization / signature / key
# ---------------------------------------------------------------------------


def test_heartbeat_kind_registered() -> None:
    assert KIND_LABELS["heartbeat"] == "心跳"
    assert REQUIRED_FIELDS["heartbeat"] == "token"


def test_heartbeat_missing_token_is_config_error(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item(token="")])
    assert len(monitor.items) == 1
    assert monitor.items[0]["config_error_code"] == "missing_required"


def test_heartbeat_invalid_period_and_grace_are_config_errors(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item(period=-5, grace=-1)])
    # The period check runs first; a single code is enough for the audit
    # contract, and the runtime probe clamps both into range afterwards.
    assert monitor.items[0]["config_error_code"] == "invalid_period"

    monitor = _monitor(tmp_path, [_hb_item(period=30, grace=-1)])
    assert monitor.items[0]["config_error_code"] == "invalid_grace"


def test_heartbeat_signature_excludes_token(tmp_path: Path) -> None:
    """Rotating a token must not move the item to a new state slot."""

    monitor = _monitor(tmp_path, [_hb_item(token="a"), _hb_item(token="b")])
    assert len(monitor.items) == 1, "same name+period+grace is one check"

    monitor = _monitor(tmp_path, [_hb_item(period=30), _hb_item(period=90)])
    assert len(monitor.items) == 2, "different period is a different check"


def test_heartbeat_item_key_has_no_token(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item(token="s3cret")])
    key = monitor._item_key(monitor.items[0])
    assert key == "heartbeat:备份脚本:"
    assert "s3cret" not in key


def test_heartbeat_display_target_is_name(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    assert monitor._display_target(monitor.items[0]) == "备份脚本"
    assert monitor._probe_target(monitor.items[0]) == "备份脚本"


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def _probe(monitor: ServiceMonitor, item: dict) -> tuple[bool, str]:
    ok, detail, target = asyncio.run(monitor._probe(item))
    assert target == item["name"]
    return ok, detail


def test_probe_without_any_state_is_probe_error(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    ok, detail = _probe(monitor, monitor.items[0])
    assert ok is False
    assert detail.startswith(PROBE_ERROR_PREFIX), detail


def test_probe_within_first_horizon_is_ok(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    _set_heartbeat(monitor.state, "备份脚本", heartbeat_first_seen=time.time())
    ok, detail = _probe(monitor, monitor.items[0])
    assert ok is True
    assert "等待首次心跳" in detail


def test_probe_never_reported_after_horizon(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    _set_heartbeat(monitor.state, "备份脚本", heartbeat_first_seen=time.time() - 300)
    ok, detail = _probe(monitor, monitor.items[0])
    assert ok is False
    assert "从未收到心跳" in detail


def test_probe_fresh_heartbeat_is_ok(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=time.time())
    ok, detail = _probe(monitor, monitor.items[0])
    assert ok is True
    assert "心跳正常" in detail


def test_probe_stale_heartbeat_is_fault(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=time.time() - 300)
    ok, detail = _probe(monitor, monitor.items[0])
    assert ok is False
    assert "心跳超时" in detail


def test_heartbeat_deadline_matches_probe(tmp_path: Path) -> None:
    """The deadline helper and the probe must agree on the same numbers."""

    monitor = _monitor(tmp_path, [_hb_item(period=30, grace=45)])
    last = time.time() - 100
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=last)
    deadline = monitor._heartbeat_deadline(monitor.items[0])
    assert deadline == pytest.approx(last + 75, abs=1e-6)


# ---------------------------------------------------------------------------
# state machine: first-failure start time, alert, recovery
# ---------------------------------------------------------------------------


class _RecordingPlugin:
    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.config = {"display": {"timezone": "Asia/Shanghai"}}
        self.context = None

    async def send_text(self, targets, text, source="unknown", kind="service"):
        self.pushed.append(str(text))
        return DeliveryResult(ok=True)


def _live_monitor(tmp_path: Path, store: ReportStore, **cfg: object):
    plugin = _RecordingPlugin()
    monitors = {
        "enabled": True,
        "fail_threshold": 1,
        "items": [_hb_item()],
        "targets": ["123456"],
    }
    monitors.update(cfg)
    monitor = ServiceMonitor(
        plugin,
        StateStore(tmp_path / "state.json"),
        {"monitors": monitors, "display": {"timezone": "Asia/Shanghai"}},
        report_store=store,
    )
    return monitor, plugin


def test_first_failure_starts_at_deadline(tmp_path: Path) -> None:
    """A heartbeat outage began when the deadline elapsed, not when the
    probe loop noticed it (the probe can be a full interval late)."""

    store = ReportStore(tmp_path / "report.db")
    monitor, _plugin = _live_monitor(tmp_path, store)
    last = time.time() - 300
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=last)
    asyncio.run(monitor._check_item(monitor.items[0]))
    st = monitor.state.get("monitors")["items"]["heartbeat:备份脚本:"]
    assert st["since"] == pytest.approx(last + 120, abs=2), st["since"]
    assert st["since"] < time.time() - 100


def test_alert_then_recovery_round_trip(tmp_path: Path) -> None:
    """One dead reporter must produce exactly one alert and one recovery,
    through the SAME state machine as every other monitor kind."""

    store = ReportStore(tmp_path / "report.db")
    monitor, plugin = _live_monitor(tmp_path, store)

    def _rows(status: str) -> list[dict]:
        return [
            r
            for r in store.list_events(time.strftime("%Y-%m-%d", time.localtime()))
            if r["status"] == status and r["kind"] == "service"
        ]

    # Dead: deadline passed long ago.
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=time.time() - 3600)
    asyncio.run(monitor._check_item(monitor.items[0]))
    key = "heartbeat:备份脚本:"
    st = monitor.state.get("monitors")["items"][key]
    assert st["alerting"] is True
    assert len(plugin.pushed) == 1, plugin.pushed
    assert "心跳超时" in plugin.pushed[0]
    alerts = _rows("alert")
    assert len(alerts) == 1, _rows("alert") + _rows("recovery")

    # Alive again.
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=time.time())
    asyncio.run(monitor._check_item(monitor.items[0]))
    st = monitor.state.get("monitors")["items"][key]
    assert st["alerting"] is False
    assert len(plugin.pushed) == 2, plugin.pushed
    assert "服务恢复" in plugin.pushed[1], plugin.pushed[1]
    recoveries = _rows("recovery")
    assert len(recoveries) == 1, recoveries


def test_first_failure_without_beats_starts_at_baseline_deadline(
    tmp_path: Path,
) -> None:
    """A reporter that NEVER checked in: the episode starts at
    first_seen + period + grace, not at the probe moment (P3-1)."""

    store = ReportStore(tmp_path / "report.db")
    monitor, _plugin = _live_monitor(tmp_path, store)
    first = time.time() - 300
    _set_heartbeat(monitor.state, "备份脚本", heartbeat_first_seen=first)
    asyncio.run(monitor._check_item(monitor.items[0]))
    st = monitor.state.get("monitors")["items"]["heartbeat:备份脚本:"]
    assert st["since"] == pytest.approx(first + 120, abs=2), st["since"]
    assert st["since"] < time.time() - 100


def test_seed_writes_first_seen_baseline(tmp_path: Path) -> None:
    """A fresh item must not alert immediately after the plugin starts."""

    store = ReportStore(tmp_path / "report.db")
    monitor, _plugin = _live_monitor(tmp_path, store)
    monitor._seed_heartbeat_baselines()
    entry = monitor.state.get("monitors")["heartbeat"]["备份脚本"]
    assert entry["heartbeat_first_seen"] == pytest.approx(time.time(), abs=5)
    ok, detail, _target = asyncio.run(monitor._probe(monitor.items[0]))
    assert ok is True
    assert "等待首次心跳" in detail


def test_seed_keeps_existing_record(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    monitor, _plugin = _live_monitor(tmp_path, store)
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=12345.0)
    monitor._seed_heartbeat_baselines()
    entry = monitor.state.get("monitors")["heartbeat"]["备份脚本"]
    assert entry["last_heartbeat"] == 12345.0
    assert "heartbeat_first_seen" not in entry


def test_prune_drops_gone_reporters(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    _set_heartbeat(monitor.state, "旧脚本", last_heartbeat=1.0)
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=2.0)
    monitor._prune_state()
    heartbeat = monitor.state.get("monitors")["heartbeat"]
    assert set(heartbeat) == {"备份脚本"}


def test_prune_handles_dirty_heartbeat_section(tmp_path: Path) -> None:
    """A hand-edited scalar/list under monitors.heartbeat must not crash."""

    monitor = _monitor(tmp_path, [_hb_item()])
    monitor.state.set("monitors", {"heartbeat": [1, 2, 3]})
    monitor._prune_state()  # must not raise
    monitor.state.set("monitors", {"heartbeat": 5})
    monitor._prune_state()  # must not raise


def test_probe_handles_dirty_heartbeat_section(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item()])
    monitor.state.set("monitors", {"heartbeat": "oops"})
    ok, detail = _probe(monitor, monitor.items[0])
    assert ok is False
    assert detail.startswith(PROBE_ERROR_PREFIX), detail


# ---------------------------------------------------------------------------
# listener
# ---------------------------------------------------------------------------


def _server(
    tmp_path: Path, monitor: ServiceMonitor, port: int, **srv: object
) -> HeartbeatServer:
    conf = {
        "enabled": True,
        "host": "127.0.0.1",
        "port": port,
        "path": "/heartbeat",
        "require_token": True,
    }
    conf.update(srv)
    return HeartbeatServer(
        StateStore(tmp_path / "state.json") if monitor is None else monitor.state,
        {"monitors": {"heartbeat_server": conf}},
        monitor,
    )


async def _post(session: aiohttp.ClientSession, url: str, **kw) -> tuple[int, dict]:
    async with session.post(url, **kw) as resp:
        try:
            body = await resp.json()
        except Exception:
            body = {}
        return resp.status, body


def test_listener_roundtrip(tmp_path: Path) -> None:
    """A good report updates the state; bad ones are rejected without
    creating any state key."""

    monitor = _monitor(tmp_path, [_hb_item(token="tok-1")])
    server = _server(tmp_path, monitor, 19969)
    url = "http://127.0.0.1:19969/heartbeat"
    headers = {"Authorization": "Bearer tok-1"}

    async def scenario():
        await server._start()
        assert server._site is not None, "listener must be up"
        try:
            out = {}
            async with aiohttp.ClientSession() as session:
                out["ok"] = await _post(
                    session, url, json={"name": "备份脚本"}, headers=headers
                )
                out["bad_token"] = await _post(
                    session,
                    url,
                    json={"name": "备份脚本"},
                    headers={"Authorization": "Bearer wrong-token"},
                )
                out["no_auth"] = await _post(session, url, json={"name": "备份脚本"})
                out["unknown"] = await _post(
                    session, url, json={"name": "不存在的项"}, headers=headers
                )
                out["no_name"] = await _post(session, url, json={}, headers=headers)
                out["bad_json"] = await _post(
                    session,
                    url,
                    data=b"not json",
                    headers={**headers, "Content-Type": "application/json"},
                )
                out["too_big"] = await _post(
                    session,
                    url,
                    data=b'{"name":"bak","pad":"' + b"x" * (65 * 1024) + b'"}',
                    headers=headers,
                )
            await asyncio.sleep(0.05)
            return out
        finally:
            await server.stop()

    out = asyncio.run(scenario())
    assert out["ok"][0] == 200, out["ok"]
    assert out["bad_token"][0] == 403, out["bad_token"]
    assert out["no_auth"][0] == 403, out["no_auth"]
    assert out["unknown"][0] == 404, out["unknown"]
    assert out["no_name"][0] == 400, out["no_name"]
    assert out["bad_json"][0] == 400, out["bad_json"]
    assert out["too_big"][0] == 413, out["too_big"]

    heartbeat = monitor.state.get("monitors")["heartbeat"]
    assert set(heartbeat) == {"备份脚本"}, heartbeat
    entry = heartbeat["备份脚本"]
    assert entry["last_heartbeat"] == pytest.approx(time.time(), abs=10)
    # Only fields with a consumer are persisted (reverse audit P3-3: a raw
    # int() counter turned dirty values into a 500 on every report).
    assert set(entry) == {"last_heartbeat"}, entry


def test_listener_no_auth_mode(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item(token="")])
    assert monitor.items[0].get("config_error"), "empty token is a config error"
    # Use a valid token but require_token=False so the switch is honoured.
    monitor = _monitor(tmp_path, [_hb_item(token="tok-2")])
    server = _server(tmp_path, monitor, 19970, require_token=False)
    url = "http://127.0.0.1:19970/heartbeat"

    async def scenario():
        await server._start()
        try:
            async with aiohttp.ClientSession() as session:
                status, _body = await _post(session, url, json={"name": "备份脚本"})
            return status
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == 200


def test_listener_refuses_wildcard_without_token(tmp_path: Path, caplog) -> None:
    """Wildcard bind + no enforced token must refuse to start (G3)."""

    monitor = _monitor(tmp_path, [_hb_item()])
    server = _server(tmp_path, monitor, 19971, host="0.0.0.0", require_token=False)

    async def scenario():
        await server._start()
        running = server._site is not None
        await server.stop()
        return running

    assert asyncio.run(scenario()) is False


def test_listener_refuses_when_no_item_has_token(tmp_path: Path) -> None:
    monitor = _monitor(tmp_path, [_hb_item(token="")])
    server = _server(tmp_path, monitor, 19972, require_token=True)

    async def scenario():
        await server._start()
        running = server._site is not None
        await server.stop()
        return running

    assert asyncio.run(scenario()) is False


def test_listener_bind_matches_alerts_semantics(tmp_path: Path) -> None:
    """Symmetry contract with alerts._webhook_bind_allowed."""

    from astrbot_plugin_statuscard.alerts import _webhook_bind_allowed

    for host in ("127.0.0.1", "::1", "10.0.0.5"):
        assert HeartbeatServer._bind_allowed(host, False) is True
    for host in ("0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0", ""):
        assert HeartbeatServer._bind_allowed(host, False) is False
        assert HeartbeatServer._bind_allowed(host, True) is True
        # alerts requires a token STRING as well (server-level token);
        # heartbeat enforces per-item tokens in the handler instead.
        assert _webhook_bind_allowed(host, True, "tok") is True


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _audit(config: dict) -> list[str]:
    return [issue.code for issue in audit_module.run_audit(config)]


def test_audit_warns_items_without_server(tmp_path: Path) -> None:
    codes = _audit({"monitors": {"enabled": True, "items": [_hb_item()]}})
    assert "heartbeat_items_no_server" in codes


def test_audit_warns_server_without_items() -> None:
    codes = _audit(
        {
            "monitors": {
                "heartbeat_server": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 19980,
                }
            }
        }
    )
    assert "heartbeat_server_no_items" in codes


def test_audit_errors_on_all_items_without_token() -> None:
    codes = _audit(
        {
            "monitors": {
                "enabled": True,
                "items": [_hb_item(token="")],
                "heartbeat_server": {"enabled": True},
            }
        }
    )
    assert "heartbeat_server_no_token" in codes
    assert "heartbeat_items_no_server" not in codes


def test_audit_errors_on_wildcard_without_token() -> None:
    codes = _audit(
        {
            "monitors": {
                "enabled": True,
                "items": [_hb_item()],
                "heartbeat_server": {
                    "enabled": True,
                    "host": "0.0.0.0",
                    "require_token": False,
                },
            }
        }
    )
    assert "heartbeat_server_open_no_token" in codes


def test_audit_no_token_when_only_broken_items_hold_tokens() -> None:
    """A token on a broken item does not count: the runtime refuses to start
    when no VALID item has a token, and the audit must say so (P3-2)."""

    codes = _audit(
        {
            "monitors": {
                "enabled": True,
                "items": [
                    _hb_item(name="无Token", token=""),
                    _hb_item(name="坏周期", token="tok", period=0),
                ],
                "heartbeat_server": {"enabled": True},
            }
        }
    )
    assert "heartbeat_server_no_token" in codes, codes


def test_seed_log_counts_only_written_items(tmp_path: Path, caplog) -> None:
    """The baseline log reports how many entries were actually seeded."""

    import logging as _logging

    monitor, _plugin = _live_monitor(tmp_path, ReportStore(tmp_path / "r.db"))
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=12345.0)
    item2 = _hb_item(name="第二个")
    monitor.items = [*monitor.items, item2]
    with caplog.at_level(_logging.INFO, logger="astrbot"):
        monitor._seed_heartbeat_baselines()
    seeded_logs = [
        r.getMessage() for r in caplog.records if "基线已写入" in r.getMessage()
    ]
    assert seeded_logs and "1 个" in seeded_logs[-1], seeded_logs


def test_listener_accepts_scalar_entry(tmp_path: Path) -> None:
    """A hand-edited scalar under monitors.heartbeat must not 500 (P3-3)."""

    monitor = _monitor(tmp_path, [_hb_item(token="tok-9")])
    monitor.state.set("monitors", {"heartbeat": {"备份脚本": "oops"}})
    server = _server(tmp_path, monitor, 19973)

    async def scenario():
        await server._start()
        try:
            async with aiohttp.ClientSession() as session:
                status, body = await _post(
                    session,
                    "http://127.0.0.1:19973/heartbeat",
                    json={"name": "备份脚本"},
                    headers={"Authorization": "Bearer tok-9"},
                )
            await asyncio.sleep(0.05)
            heartbeat = monitor.state.get("monitors")["heartbeat"]
            return status, body, heartbeat
        finally:
            await server.stop()

    status, _body, heartbeat = asyncio.run(scenario())
    assert status == 200, status
    assert isinstance(heartbeat["备份脚本"], dict), heartbeat


def test_audit_warns_on_port_conflict() -> None:
    codes = _audit(
        {
            "alerts": {
                "komari_webhook": {"enabled": True, "host": "127.0.0.1", "port": 19981}
            },
            "monitors": {
                "enabled": True,
                "items": [_hb_item()],
                "heartbeat_server": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 19981,
                },
            },
        }
    )
    assert "heartbeat_server_conflict" in codes


def test_audit_clean_heartbeat_setup() -> None:
    codes = _audit(
        {
            "monitors": {
                "enabled": True,
                "items": [_hb_item()],
                "heartbeat_server": {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 19982,
                },
            }
        }
    )
    assert not [c for c in codes if c.startswith("heartbeat")], codes


def test_audit_period_grace_are_item_problems(tmp_path: Path) -> None:
    issues = audit_module.run_audit(
        {"monitors": {"enabled": True, "items": [_hb_item(period=0)]}}
    )
    item_issues = [i for i in issues if i.code == "monitor_item_config"]
    assert any(
        "invalid_period" in str(i.params) or "period" in i.message for i in item_issues
    ), [(i.code, i.message) for i in issues]


def test_monitor_items_drive_audit_and_runtime_identically(tmp_path: Path) -> None:
    """Symmetry: what the audit calls broken, the runtime also skips."""

    items = [
        _hb_item(),
        _hb_item(name="坏周期", period=0),
        _hb_item(name="坏宽限", grace=-2),
    ]
    monitor = _monitor(tmp_path, items)
    config = {"monitors": {"enabled": True, "items": items}}
    runtime_broken = {
        item["name"] for item in monitor.items if item.get("config_error")
    }
    audited = {
        problem["name"] for problem in audit_module._monitor_config_errors(config)
    }
    assert runtime_broken == audited
    assert runtime_broken == {"坏周期", "坏宽限"}


# ---------------------------------------------------------------------------
# notification path
# ---------------------------------------------------------------------------


def test_alert_push_goes_through_the_shared_send_path(tmp_path: Path) -> None:
    """The heartbeat alert must leave through the same _send/_redactor path
    as every other monitor kind (the push is masked there; the event log
    keeps the raw name for pairing)."""

    store = ReportStore(tmp_path / "report.db")
    monitor, plugin = _live_monitor(tmp_path, store)
    _set_heartbeat(monitor.state, "备份脚本", last_heartbeat=time.time() - 3600)
    asyncio.run(monitor._check_item(monitor.items[0]))
    assert plugin.pushed, "the alert must be pushed"
    assert "心跳" in plugin.pushed[0], plugin.pushed[0]
    rows = [
        r
        for r in store.list_events(time.strftime("%Y-%m-%d", time.localtime()))
        if r["kind"] == "service"
    ]
    assert rows and rows[-1]["name"] == "备份脚本", rows
