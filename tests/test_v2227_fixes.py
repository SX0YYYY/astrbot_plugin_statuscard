"""Regression tests for the v2.20.27 audit batch.

Every test drives the production path that changed:

* the truncated-target state migration matches the legacy (un-rstripped)
  target, so upgrading from <=2.20.22 carries "python #1" state over;
* the silent-window recovery and the config-error close both drop the
  offline backoff counters (the normal recovery path already did);
* an alert suppressed by a down dependency still records the episode start;
* probe signatures strip keyword/host like the probe does, so a pasted
  trailing space cannot create a second alerting slot;
* disabling (not deleting) an alerting item writes "已停用", not "已移除";
* orphaned state.json.<pid>.<tid>.tmp files from a crashed process are
  cleaned at load();
* Windows mount errors ("[WinError 2/3]") map to 挂载点不存在;
* the audit fallback rejects a boolean TCP port like the runtime does.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from contextlib import contextmanager
from pathlib import Path

import astrbot_plugin_statuscard.collectors as collectors_module
import astrbot_plugin_statuscard.monitor as monitor_module
from astrbot_plugin_statuscard.alerts import AlertEngine
from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.report_view import DailyReportBuilder
from astrbot_plugin_statuscard.state import StateStore

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
        self.context = None

    def _pop(self):
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return DeliveryResultLike()

    async def send_text(self, targets, text, source="unknown"):
        self.sent.append((list(targets), text, source))
        return self._pop()


class DeliveryResultLike:
    ok = True


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


# ---------------------------------------------------------------------------
# Truncated-target migration matches the un-rstripped legacy slot.
# ---------------------------------------------------------------------------


def test_migration_carries_trailing_space_slot(tmp_path, monkeypatch) -> None:
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"name": "worker", "type": "process", "pattern": "python #1"}],
        day_patch=monkeypatch,
    )
    state.data["monitors"] = {
        "items": {
            # Legacy key/target built by the v2.20.22 redact_url: NO rstrip.
            "process:worker:python ": {
                "name": "worker",
                "target": "python ",
                "alerting": True,
                "fails": 2,
            }
        }
    }
    monitor._migrate_truncated_target_state(
        "process:worker:python #1", monitor.items[0]
    )
    items = state.data["monitors"]["items"]
    assert "process:worker:python #1" in items, "legacy slot must be carried over"
    assert "process:worker:python " not in items
    assert items["process:worker:python #1"]["alerting"] is True


def test_migration_still_skips_when_key_exists(tmp_path, monkeypatch) -> None:
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"name": "worker", "type": "process", "pattern": "python #1"}],
        day_patch=monkeypatch,
    )
    state.data["monitors"] = {
        "items": {
            "process:worker:python ": {"name": "worker", "target": "python "},
            "process:worker:python #1": {"name": "worker", "target": "python #1"},
        }
    }
    monitor._migrate_truncated_target_state(
        "process:worker:python #1", monitor.items[0]
    )
    # The target key already exists: nothing moves, nothing is lost.
    assert "process:worker:python " in state.data["monitors"]["items"]


# ---------------------------------------------------------------------------
# The silent-window recovery drops the offline backoff counters.
# ---------------------------------------------------------------------------


def test_silent_recovery_clears_offline_backoff(tmp_path, monkeypatch) -> None:
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"name": "api", "type": "http", "url": "http://127.0.0.1:1/health"}],
        config={
            "monitors": {
                "silence_enabled": True,
                # A window that covers the fixed "now" (11-05 12:00).
                "silence_until": "2026-11-05 13:00:00",
            }
        },
        day_patch=monkeypatch,
    )
    key = "http:api:http://127.0.0.1:1/health"
    state.data["monitors"] = {
        "items": {
            key: {
                "name": "api",
                "target": "http://127.0.0.1:1/health",
                "alerting": True,
                "fails": 2,
                "since": 1000.0,
                "offline_retry_count": 3,
                "recovery_offline_count": 2,
                "next_attempt": 0,
            }
        }
    }

    async def fake_probe(item):
        return True, "HTTP 200（5ms）", item.get("url")

    monitor._probe = fake_probe

    async def run():
        await monitor._check_item(monitor.items[0])

    asyncio.run(run())
    st = state.data["monitors"]["items"][key]
    assert "offline_retry_count" not in st, "silent-window recovery must clear it"
    assert "recovery_offline_count" not in st


# ---------------------------------------------------------------------------
# The config-error close drops offline_retry_count too.
# ---------------------------------------------------------------------------


def test_config_error_close_clears_offline_backoff(tmp_path, monkeypatch) -> None:
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [{"name": "t", "type": "tcp", "host": "127.0.0.1", "port": "oops"}],
        day_patch=monkeypatch,
    )
    key = "tcp:t:127.0.0.1:0"
    st = {
        "name": "t",
        "target": "127.0.0.1:oops",
        "alerting": True,
        "offline_retry_count": 3,
    }
    state.data["monitors"] = {"items": {key: st}}

    # This is the check_all entry point for items the normalizer flagged
    # with config_error (a bad TCP port never reaches _probe).
    async def run():
        monitor._close_episode_for_config_error(
            st,
            "t",
            "127.0.0.1:0",
            "tcp",
            "配置不完整（端口 'oops' 不是有效的 1–65535 端口）",
        )

    asyncio.run(run())
    assert "offline_retry_count" not in st, "config-error close must clear it"


# ---------------------------------------------------------------------------
# A suppressed probe still records the episode start.
# ---------------------------------------------------------------------------


def test_suppressed_failure_records_since(tmp_path, monkeypatch) -> None:
    monitor, state, _store, _plugin = _make_monitor(
        tmp_path,
        [
            {"name": "dep", "type": "http", "url": "http://dep.example/"},
            {
                "name": "app",
                "type": "http",
                "url": "http://app.example/",
                "suppress_when_down": "dep",
            },
        ],
        config={"monitors": {"suppress_duplicates": True}},
        day_patch=monkeypatch,
    )

    async def fake_probe(item):
        return False, "ConnectError: down", item.get("url")

    monitor._probe = fake_probe

    async def run():
        await monitor._check_item(monitor.items[0])  # dep goes alerting
        await monitor._check_item(monitor.items[1])  # app is suppressed

    asyncio.run(run())
    states = state.data["monitors"]["items"]
    app_key = next(k for k in states if ":app:" in k)
    assert states[app_key].get("since"), "suppressed episode must have a start"
    assert not states[app_key].get("alerting")


# ---------------------------------------------------------------------------
# Probe signatures strip keyword/host like the probe does.
# ---------------------------------------------------------------------------


def test_signature_strips_keyword_and_host() -> None:
    # __template_key is what kind resolution reads: without it both sides
    # collapse to ("", name) and the assertion passes even on the unfixed
    # baseline (2026-11-08 reverse audit - keep this key here).
    base = {
        "name": "n",
        "type": "http",
        "__template_key": "http",
        "url": "http://x/",
        "keyword": "ok ",
    }
    twin = {**base, "keyword": "ok"}
    assert ServiceMonitor._probe_signature(base) == ServiceMonitor._probe_signature(
        twin
    )
    # And the pair is distinguishable before stripping: different keyword
    # must differ (guards against a signature that ignores the field).
    other = {**base, "keyword": "other"}
    assert ServiceMonitor._probe_signature(base) != ServiceMonitor._probe_signature(
        other
    )

    tcp_a = {
        "name": "n",
        "type": "tcp",
        "__template_key": "tcp",
        "host": " 127.0.0.1",
        "port": 3000,
    }
    tcp_b = {**tcp_a, "host": "127.0.0.1"}
    assert ServiceMonitor._probe_signature(tcp_a) == ServiceMonitor._probe_signature(
        tcp_b
    )
    tcp_c = {**tcp_a, "host": "192.0.2.9"}
    assert ServiceMonitor._probe_signature(tcp_a) != ServiceMonitor._probe_signature(
        tcp_c
    )


# ---------------------------------------------------------------------------
# Disabling an alerting item writes 已停用, not 已移除.
# ---------------------------------------------------------------------------


def test_prune_says_disabled_for_disabled_entry(tmp_path, monkeypatch) -> None:
    monitor, state, store, _plugin = _make_monitor(
        tmp_path,
        # Same key as the live state slot, but disabled in the config.
        [
            {
                "name": "api",
                "type": "http",
                "url": "http://127.0.0.1:2/",
                "enabled": False,
            }
        ],
        day_patch=monkeypatch,
    )
    key = "http:api:http://127.0.0.1:2/"
    state.data["monitors"] = {
        "items": {
            key: {
                "name": "api",
                "target": "http://127.0.0.1:2/",
                "alerting": True,
            }
        }
    }
    monitor._prune_state()
    rows = store.list_events(FIXED_DAY)
    details = [str(row.get("detail") or "") for row in rows]
    assert any("已停用" in d for d in details), details
    assert not any("已移除" in d for d in details), details


def test_prune_still_says_removed_for_gone_entry(tmp_path, monkeypatch) -> None:
    monitor, state, store, _plugin = _make_monitor(
        tmp_path,
        [{"name": "other", "type": "http", "url": "http://other.example/"}],
        day_patch=monkeypatch,
    )
    key = "http:api:http://gone.example/"
    state.data["monitors"] = {
        "items": {
            key: {"name": "api", "target": "http://gone.example/", "alerting": True}
        }
    }
    monitor._prune_state()
    rows = store.list_events(FIXED_DAY)
    details = [str(row.get("detail") or "") for row in rows]
    assert any("已移除" in d for d in details), details


# ---------------------------------------------------------------------------
# Orphaned state temp files from a crashed process are cleaned at load().
# ---------------------------------------------------------------------------


def test_load_cleans_orphan_tmps(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    orphan = tmp_path / "state.json.999999.8888.tmp"
    orphan.write_text("{}", encoding="utf-8")
    mine = tmp_path / f"state.json.{os.getpid()}.777.tmp"
    mine.write_text("{}", encoding="utf-8")
    store = StateStore(path)
    store.load()
    assert not orphan.exists(), "crashed-process temp must be removed"
    assert mine.exists(), "this process's temp must be left alone"


# ---------------------------------------------------------------------------
# Windows mount errors map to 挂载点不存在.
# ---------------------------------------------------------------------------


def test_short_disk_error_windows(monkeypatch) -> None:
    monkeypatch.setattr(collectors_module, "_IS_WINDOWS", True, raising=False)
    assert (
        collectors_module._short_disk_error("[WinError 3] 系统找不到指定的路径。")
        == "挂载点不存在"
    )
    assert (
        collectors_module._short_disk_error("[WinError 2] 系统找不到指定的文件。")
        == "挂载点不存在"
    )


# ---------------------------------------------------------------------------
# The audit fallback rejects a boolean port like the runtime does.
# ---------------------------------------------------------------------------


def test_audit_rejects_boolean_port() -> None:
    config = {
        "monitors": {
            "enabled": True,
            "items": [{"name": "t", "type": "tcp", "host": "127.0.0.1", "port": True}],
        }
    }
    issues = run_audit(config)
    texts = [str(getattr(issue, "message", issue)) for issue in issues]
    assert any("端口不是数字" in t for t in texts), texts


# ---------------------------------------------------------------------------
# The dashboard note reads the NUMERIC aborted_long from the report stats.
# ---------------------------------------------------------------------------

_CONFIG = {"display": {"timezone": "Asia/Shanghai"}}

_PROVIDER_COLUMNS = (
    "created_at, status, provider_model, conversation_id, "
    "start_time, end_time, time_to_first_token"
)


def _provider_row(status: str, latency: float) -> tuple:
    end = 1_800_000_000.0
    return ("2026-11-05 04:00:00.000", status, "m", "conv-1", end - latency, end, 0.1)


def _quality_stats(tmp_path: Path, rows: list[tuple]) -> dict:
    db_path = tmp_path / "data_v4.db"
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.execute(f"CREATE TABLE provider_stats ({_PROVIDER_COLUMNS})")
    connection.executemany(
        "INSERT INTO provider_stats VALUES (?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()
    builder = DailyReportBuilder(
        ReportStore(tmp_path / "report.db"), _CONFIG, db_path=db_path
    )
    view = builder._provider_quality_section(
        dt.date(2026, 11, 5), history_days=1, span=1
    )
    return view.get("stats") or {}


def test_today_overview_default_has_truncated(tmp_path: Path) -> None:
    import astrbot_plugin_statuscard.provider_quality as pq

    overview = pq.today_overview(_CONFIG, tmp_path / "missing.db")
    assert overview["available"] is False
    assert overview.get("truncated") is False


def test_report_stats_carry_numeric_aborted_long(tmp_path: Path) -> None:
    stats = _quality_stats(
        tmp_path,
        [_provider_row("aborted", 2160.0), _provider_row("aborted", 2.0)],
    )
    # The dashboard note appends the long-abort sentence from this key;
    # v2.20.26 only shipped the text variant, so the sentence never showed.
    assert stats.get("aborted_long") == 1
    assert stats.get("aborted_long_text") == "1"


def test_report_stats_aborted_long_defaults_to_zero(tmp_path: Path) -> None:
    stats = _quality_stats(tmp_path, [_provider_row("completed", 10.0)])
    assert stats.get("aborted_long") == 0


# ---------------------------------------------------------------------------
# A deferred offline alert folds the queued recovery of the PREVIOUS episode.
# ---------------------------------------------------------------------------


def _make_offline_engine(tmp_path, day_patch=None):
    state = MemoryState()
    plugin = ScriptedPlugin()
    plugin.state = state
    store = ReportStore(tmp_path / "report.db")
    cfg = {"alerts": {"targets": ["default:GroupMessage:1"]}}
    engine = AlertEngine(plugin, state, cfg, store)
    if day_patch is not None:
        day_patch.setattr(
            __import__("astrbot_plugin_statuscard.alerts", fromlist=["x"]),
            "now_in_timezone",
            _fixed_now,
        )
    return engine, state, store, plugin


def test_deferred_offline_alert_folds_queued_recovery(tmp_path, monkeypatch) -> None:
    import time as time_module

    import astrbot_plugin_statuscard.alerts as alerts_module

    engine, state, _store, _plugin = _make_offline_engine(tmp_path)
    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *args, **kwargs: object()
    )

    async def offline_call(*args, **kwargs):
        return {"online": False}

    monkeypatch.setattr(alerts_module, "_call_action", offline_call)
    state.data["alerts"] = {
        "offline": {
            "alerting": True,
            "alerted": False,
            "pending_alert": False,
            "since": time_module.time() - 7200,
            "last_sent": time_module.time() - engine.cooldown - 60,
            # The previous episode's recovery notice never made it out.
            "pending_recovery": True,
            "recovery_offline_count": 1,
        }
    }

    asyncio.run(engine._check_offline())

    body = "\n".join(text for _t, text, _s in _plugin.sent) if _plugin.sent else ""
    assert "已并入本条" in body, body
    st = state.data["alerts"]["offline"]
    assert "pending_recovery" not in st
    assert "recovery_offline_count" not in st
    assert st.get("alerted") is True
