"""Mechanised guardrails (G1-G5) from the 2026-11-14 architecture review.

These tests replace the "run another bug-hunt round" loop for the seven
recurring defect patterns (see 文档/架构回顾与重构评估_20261114.md §2): every
pattern that history showed recurring 15+ times gets a mechanised check that
fails the build instead of waiting for the next audit to find it.

- G1  symmetry contract: the same episode scenario drives BOTH the resource
      alert engine and the service monitor; their user-visible semantics must
      agree (explicitly-declared differences are listed in one place).
- G2  dirty-data contract: every documented dirt shape in state.json and
      report.db must not raise anywhere in the build/render/alert pipeline.
- G3  no-false-success contract: every path that decides NOT to deliver
      (silence, empty targets) must return a failure result, never ok.
- G4  enum/style contract: every status_key / monitor status the backends can
      emit has a badge/dot style in BOTH stylesheets, and every indirectly
      referenced i18n key (mapping tables) exists in both locales.
- G5  state-key lifecycle contract: the "close episode" cleanup lists must
      cover every transient marker each subsystem writes, and pop-lists must
      only pop keys that are actually written (dead-key guard).

Only G4 grew a product fix on first run (`.dot.config_error` had no style);
everything else locked existing behaviour.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# G4: enum <-> style <-> i18n contract
# ---------------------------------------------------------------------------


def _badge_classes(css: str) -> set[str]:
    return set(re.findall(r"\.badge\.([A-Za-z0-9_-]+)", css))


def _dot_classes(css: str) -> set[str]:
    return set(re.findall(r"\.dot\.([A-Za-z0-9_-]+)", css))


def test_g4_status_key_enum_has_badge_styles_everywhere() -> None:
    """Every status_key the report builder can emit needs a badge in BOTH css.

    The v2.20.11 incident: the builder switched `recovery` -> `recovered`
    while the report CSS only had `.badge.recovery`, so every pushed daily
    report rendered its "已恢复" rows as an unstyled grey pill.
    """

    view_src = _read("report_view.py")
    # Literal emissions plus the closure conversion (alert/failed + resolved_ts
    # -> "recovered") plus the unresolved branch.
    emitted = set(re.findall(r'"status_key":\s*"([a-z_]+)"', view_src))
    emitted |= set(re.findall(r'\.get\("status_key"\)\s*==\s*"([a-z_]+)"', view_src))
    emitted |= set(re.findall(r'\bstatus_key\]?\s*=\s*"([a-z_]+)"', view_src))
    known_transitions = {"recovered"}  # alert/failed + resolved_ts -> recovered
    unresolved = {"unresolved"}  # explicit branch, report_view.py L~1650
    # The raw `status` column values the pairing loop can pass through.
    raw = {"alert", "failed", "recovery"}
    enum = emitted | known_transitions | unresolved | raw
    # Drop values that are actually event-status comparisons, not emissions:
    # everything above IS the closed set the dashboard can render.
    assert enum == {
        "alert",
        "failed",
        "recovery",
        "recovered",
        "unresolved",
    }, sorted(enum)

    report_css = _read("templates", "report", "report.css")
    dash_css = _read("pages", "status", "style.css")
    for css_name, classes in (
        ("templates/report/report.css", _badge_classes(report_css)),
        ("pages/status/style.css", _badge_classes(dash_css)),
    ):
        missing = enum - classes
        assert not missing, f"{css_name} lacks .badge. styles for: {sorted(missing)}"


def test_g4_monitor_status_enum_has_dot_styles() -> None:
    """Every `_monitor_status` return value needs a .dot.<status> style.

    The overview cards build `class="dot " + item.status`; a value without a
    style silently degrades to the neutral grey -- which is exactly how
    `config_error` became indistinguishable from `unknown` (found by this
    guard on its first run, fixed in the same batch).
    """

    page_src = _read("page_api.py")
    body = page_src[page_src.index("def _monitor_status") :]
    body = body[: body.index("\nclass ")]
    returns = set(re.findall(r'return\s+"([a-z_]+)"', body))
    assert returns == {"ok", "degraded", "unknown", "fail", "config_error"}, sorted(
        returns
    )
    dash_css = _read("pages", "status", "style.css")
    dots = _dot_classes(dash_css)
    missing = returns - dots
    assert not missing, (
        f"pages/status/style.css lacks .dot. styles for: {sorted(missing)}"
    )


def _mapping_table_keys(app_src: str) -> set[str]:
    """All i18n keys reachable through mapping tables, not literal t() calls.

    The v2.20.12 incident (English UI showing "Alert" for 未恢复) was a
    mapping-table value missing from the locales; the literal-scan contract
    test cannot see these because the key is only computed at runtime.
    """

    keys: set[str] = set()
    for table in ("STATUS_I18N", "DURATION_I18N", "EMPTY_REASON_I18N"):
        block = app_src[app_src.index(f"const {table} =") :]
        block = block[: block.index("};")]
        keys |= set(re.findall(r':\s*"([A-Za-z0-9_]+)"', block))
    # EMPTY_REASON_CODE_KEYS maps codes to bare key names (no t() wrapper).
    block = app_src[app_src.index("const EMPTY_REASON_CODE_KEYS =") :]
    block = block[: block.index("};")]
    keys |= set(re.findall(r":\s*\"?([A-Za-z0-9_]+)\"?,", block))
    # Dynamic prefixes: "monitor_config_error_" + error_code.
    en = json.loads(
        (ROOT / ".astrbot-plugin" / "i18n" / "en-US.json").read_text(encoding="utf-8")
    )
    strings = en["pages"]["status"]["strings"]
    # t("monitor_config_error_" + code): the WHOLE concatenated key must
    # exist, so add every such locale key itself to the required set.
    keys |= {k for k in strings if k.startswith("monitor_config_error_")}
    return {k for k in keys if k}


def test_g4_mapping_table_i18n_keys_exist_in_both_locales() -> None:
    app_src = _read("pages", "status", "app.js")
    keys = _mapping_table_keys(app_src)
    assert keys, "no mapping-table keys found (extraction broke?)"
    for locale in ("zh-CN", "en-US"):
        data = json.loads(
            (ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        strings = data["pages"]["status"]["strings"]
        missing = sorted(keys - set(strings))
        assert not missing, f"{locale} is missing mapping keys: {missing}"


# ---------------------------------------------------------------------------
# G3: no false success on suppressed delivery
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_g3_monitor_send_silence_returns_failure(tmp_path) -> None:
    """Maintenance silence must REPORT failure, not pretend success.

    A swallowed summary that claims ok would mark its rules notified and
    their later recovery would claim an alert the user never saw (the exact
    v2.20.18 reverse-audit finding).
    """

    from astrbot_plugin_statuscard.monitor import ServiceMonitor
    from astrbot_plugin_statuscard.state import StateStore

    monitor = ServiceMonitor(
        _BarePlugin(),
        StateStore(tmp_path / "state.json"),
        {
            "monitors": {
                "enabled": True,
                "items": [],
                "silence_until": "2099-01-01 00:00",
            }
        },
        report_store=None,
    )
    result = _run(monitor._send("任何文本"))
    assert result.ok is False
    assert not result.uncertain_targets


def test_g3_empty_targets_returns_failure_both_engines(tmp_path) -> None:
    """No targets configured: a failure result, never a clean ok."""

    from astrbot_plugin_statuscard.alerts import AlertEngine
    from astrbot_plugin_statuscard.monitor import ServiceMonitor
    from astrbot_plugin_statuscard.state import StateStore

    engine = AlertEngine(
        _BarePlugin(),
        StateStore(tmp_path / "a.json"),
        {"alerts": {"enabled": True}, "display": {"timezone": "Asia/Shanghai"}},
        report_store=None,
    )
    result = _run(engine._send("文本", kind="resource"))
    assert result.ok is False

    monitor = ServiceMonitor(
        _BarePlugin(),
        StateStore(tmp_path / "m.json"),
        {"monitors": {"enabled": True, "items": []}},
        report_store=None,
    )
    result = _run(monitor._send("文本"))
    assert result.ok is False


def test_g3_official_scene_loss_is_offline_not_success(tmp_path) -> None:
    """A falsy framework return (lost official scene cache) is offline.

    platform_support.send_by_session returns None when the official adapter's
    in-memory scene cache is empty; treating that as "sent" would silently
    disable alerts (v2.21.0 fixed the prime, the contract locks the result).
    """

    from astrbot_plugin_statuscard.delivery import DeliveryService

    class _Ctx:
        async def send_message(self, target, chain):
            return None  # exactly what a lost scene cache looks like

    service = DeliveryService.__new__(DeliveryService)
    service.context = _Ctx()
    service._build_chain = lambda payload: [{"type": "tts", "data": {"text": ""}}]
    sent, uncertain, offline = _run(
        service._send_via_astrbot(
            "Q官弗洛洛:GroupMessage:1B8C23D3E4F5678901234567890ABCD",
            {"text": "文本"},
        )
    )
    assert sent is False
    assert offline is True
    assert uncertain is False


class _BarePlugin:
    """Minimal plugin stand-in: no state, default config, no context use."""

    def __init__(self) -> None:
        self.config = {"display": {"timezone": "Asia/Shanghai"}}
        self.context = None


# ---------------------------------------------------------------------------
# G5: state-key lifecycle contract
# ---------------------------------------------------------------------------

# Transient markers each subsystem writes per episode. Every "close episode"
# branch (threshold=0, mount gone, disabled, config error) must clear ALL of
# them -- a missed key survives the shutdown and resurfaces as a stale notice
# (the v2.20.0 awaiting_summary incident, the v2.21.3 recovery_awaits_summary
# noise, and the monitor-side twin of both).
_TRANSIENT_BY_SUBSYSTEM = {
    "alerts.rules": {
        "awaiting_summary",
        "summary_buffered",
        "summary_key",
        "summary_reason",
        "summary_abandoned",
        "recovery_awaits_summary",
        "pending_recovery",
        "recovery_offline_count",
        "recovery_next_attempt",
        "recovery_retry_count",
        "retry_count",
        "next_attempt",
        "offline_retry_count",
        "send_inflight",
        "alerting",
        "alerted",
        "stale",
    },
    "monitors.items": {
        "awaiting_summary",
        "summary_buffered",
        "summary_key",
        "summary_reason",
        "summary_abandoned",
        "recovery_awaits_summary",
        "pending_recovery",
        "recovery_offline_count",
        "recovery_next_attempt",
        "recovery_retry_count",
        "retry_count",
        "next_attempt",
        "offline_retry_count",
        "send_inflight",
        "send_exhausted",
        "pending",
        "alerting",
        "alerted",
        "stale",
    },
}

# Keys that are only ever popped, never written since the item_key rework:
# historical rows may still carry them, so the cleanup lists keep popping.
# + "disk": alerts.py's mount migration pops the OLD bare "disk" layout key
#   so the newer "disk:<path>" entries win (documented at that pop site).
_KNOWN_DEAD_KEYS = {"recovery_attempt", "disk"}


def _block(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _popped_keys(block: str) -> set[str]:
    # Literal pops plus every quoted snake_case key in the block: the close
    # branches pop most keys through a `for stale_key in (...)` tuple, so a
    # literal-only scan would miss the whole loop. Over-matching is harmless
    # here -- the check only asks whether the KNOWN transient set is covered.
    literals = set(re.findall(r'st\.pop\("([A-Za-z_]+)"', block))
    literals |= set(re.findall(r'"([a-z_]{5,})"', block))
    return literals


def _zeroed_keys(block: str) -> set[str]:
    # Explicit falsy resets inside the close branch (= 0 / = False / = None).
    return set(re.findall(r'st\["([A-Za-z_]+)"\]\s*=\s*(?:0|False|None)\b', block))


def test_g5_close_branches_clear_every_transient_marker() -> None:
    """alerts: the threshold=0 close branch must clear the whole marker set."""

    alerts_src = _read("alerts.py")
    block = _block(
        alerts_src,
        "was_alerting = alerting",
        "if value >= threshold:",
    )
    cleared = _popped_keys(block) | _zeroed_keys(block)
    missing = _TRANSIENT_BY_SUBSYSTEM["alerts.rules"] - cleared
    assert not missing, (
        "alerts 阈值 0 关闭分支漏清状态键（新标记要同步加入关闭清单）: "
        f"{sorted(missing)}"
    )


def test_g5_monitor_config_error_close_covers_transients() -> None:
    """monitor: the config-error closure must clear the whole marker set."""

    monitor_src = _read("monitor.py")
    block = _block(
        monitor_src,
        "def _close_episode_for_config_error",
        "async def _probe(",
    )
    cleared = _popped_keys(block) | _zeroed_keys(block)
    # The config-error closure deliberately keeps `ever_ok`/identity keys; it
    # must still clear every transient delivery marker.
    missing = _TRANSIENT_BY_SUBSYSTEM["monitors.items"] - cleared - {"stale"}
    assert not missing, f"monitor 配置错误关闭分支漏清状态键: {sorted(missing)}"


def test_g5_popped_keys_must_exist_as_writes_or_known_dead() -> None:
    """Popping a key that nothing writes is a dead cleanup (or a typo).

    `recovery_attempt` is the known case: legacy state may still carry it, so
    the pops stay, but any NEW key that only appears in pop lists is a bug in
    the making (the cleanup would silently do nothing).
    """

    findings: list[str] = []
    for filename in ("alerts.py", "monitor.py"):
        src = _read(filename)
        # Any left-hand variable (st / watch / state / items...): the
        # provider_watch and migration blocks use different dict variables.
        written = set(re.findall(r'[\w.]+\["([A-Za-z_]+)"\]\s*=\s', src))
        written |= set(re.findall(r'\.setdefault\("([A-Za-z_]+)"', src))
        popped = set(re.findall(r'\.pop\("([A-Za-z_]+)"', src))
        ghosts = popped - written - _KNOWN_DEAD_KEYS
        # Nested-dict pops like st.pop("key", None) inside section dicts of a
        # DIFFERENT subsystem are filtered by the write scan of the same file;
        # anything left is either a genuine typo or a foreign key that needs
        # an explicit entry in _KNOWN_DEAD_KEYS with a comment.
        if ghosts:
            findings.append(f"{filename}: {sorted(ghosts)}")
    assert not findings, f"pop 了从未写入的键（死清理）: {findings}"


def test_g5_recovery_attempt_stays_dead() -> None:
    """The dead key must stay dead: written nowhere, popped for legacy rows."""

    for filename in ("alerts.py", "monitor.py"):
        src = _read(filename)
        assert not re.search(r'\w+\["recovery_attempt"\]\s*=\s(?!None)', src), (
            f"{filename} 又开始写 recovery_attempt（历史死键）"
        )


# ---------------------------------------------------------------------------
# G2: dirty-data contract
# ---------------------------------------------------------------------------

_DIRTY = [
    None,
    "",
    -1,
    10**18,
    float("inf"),
    float("nan"),
    "abc",
    [],
    {"nested": {"deep": float("inf")}},
]


def _dirty_states(base: dict, keys: list[str]) -> list[dict]:
    """One variant per (key, dirt) over the given keys, layered on base."""

    variants = []
    for key in keys:
        for dirt in _DIRTY:
            variant = json.loads(json.dumps(base))  # deep copy minus inf/nan
            variant[key] = dirt
            variants.append(variant)
    return variants


def test_g2_dirty_rule_state_never_raises(tmp_path) -> None:
    """Every dirt shape in a resource rule survives a full sample round."""

    from astrbot_plugin_statuscard.alerts import AlertEngine
    from astrbot_plugin_statuscard.state import StateStore

    keys = [
        "alerting",
        "alerted",
        "alert_since",
        "count",
        "last_sent",
        "last_attempt",
        "last_error",
        "retry_count",
        "next_attempt",
        "offline_retry_count",
        "send_inflight",
        "awaiting_summary",
        "summary_buffered",
        "summary_key",
        "summary_reason",
        "summary_abandoned",
        "pending_recovery",
        "recovery_retry_count",
        "recovery_next_attempt",
        "recovery_awaits_summary",
        "stale",
        "label",
        "last_seen",
    ]
    failures: list[str] = []
    for index, variant in enumerate(_dirty_states({"label": "CPU 使用率"}, keys)):
        store = StateStore(tmp_path / f"state_{index}.json")
        store.set("alerts", {"rules": {"cpu": variant}})
        engine = AlertEngine(
            _BarePlugin(),
            store,
            {"alerts": {"enabled": True}, "display": {"timezone": "Asia/Shanghai"}},
            report_store=None,
        )
        try:
            engine._mark_stale_rules()
            _run(engine._evaluate("cpu", "CPU 使用率", 10.0, 85.0, "%", "10.0%"))
            _run(engine._evaluate("cpu", "CPU 使用率", 90.0, 85.0, "%", "90.0%"))
            _run(engine._evaluate("cpu", "CPU 使用率", 10.0, 85.0, "%", "10.0%"))
        except Exception as exc:
            failures.append(f"key={variant!r:.60} -> {exc.__class__.__name__}: {exc}")
    assert not failures, "\n".join(failures[:8])


def test_g2_dirty_offline_state_never_raises(tmp_path, monkeypatch) -> None:
    """Every dirt shape in alerts.offline survives a disconnect-detect round."""

    from astrbot_plugin_statuscard import alerts as alerts_module
    from astrbot_plugin_statuscard.alerts import AlertEngine
    from astrbot_plugin_statuscard.state import StateStore

    keys = [
        "alerting",
        "alerted",
        "since",
        "count",
        "unreachable",
        "unreachable_warned",
        "last_seen",
        "last_sent",
        "pending_alert",
        "pending_alert_text",
        "pending_recovery",
        "recovery_retry_count",
        "recovery_next_attempt",
        "stale",
    ]

    async def fake_call(client, action: str, timeout: float = 5.0):
        return {"online": True}

    monkeypatch.setattr(
        alerts_module, "_get_aiocqhttp_client", lambda *a, **k: object()
    )
    monkeypatch.setattr(alerts_module, "_call_action", fake_call)

    failures: list[str] = []
    for index, variant in enumerate(_dirty_states({}, keys)):
        store = StateStore(tmp_path / f"state_{index}.json")
        store.set("alerts", {"offline": variant})
        engine = AlertEngine(
            _BarePlugin(),
            store,
            {
                "alerts": {"enabled": True, "platform_id": "default"},
                "display": {"timezone": "Asia/Shanghai"},
            },
            report_store=None,
        )
        try:
            _run(engine._check_offline())
        except Exception as exc:
            failures.append(f"key={variant!r:.60} -> {exc.__class__.__name__}: {exc}")
    assert not failures, "\n".join(failures[:8])


def test_g2_dirty_monitor_item_never_raises(tmp_path, monkeypatch) -> None:
    """Every dirt shape in a monitor item survives probe + prune rounds."""

    from astrbot_plugin_statuscard.monitor import ServiceMonitor
    from astrbot_plugin_statuscard.state import StateStore

    keys = [
        "alerting",
        "alerted",
        "since",
        "fails",
        "last_checked",
        "last_ok",
        "ever_ok",
        "last_error",
        "retry_count",
        "next_attempt",
        "offline_retry_count",
        "send_inflight",
        "send_exhausted",
        "pending",
        "pending_recovery",
        "recovery_retry_count",
        "recovery_next_attempt",
        "recovery_awaits_summary",
        "summary_abandoned",
        "awaiting_summary",
        "stale",
        "silent_since",
        "config_error_warned",
        "name",
        "target",
    ]
    monitor = ServiceMonitor(
        _BarePlugin(),
        StateStore(tmp_path / "state.json"),
        {
            "monitors": {
                "enabled": True,
                "items": [
                    {
                        "__template_key": "http",
                        "name": "站点",
                        "url": "https://x/",
                        "enabled": True,
                    }
                ],
            }
        },
        report_store=None,
    )
    monitor.items[0]["timeout_seconds"] = 1

    async def fake_probe(item):
        return True, "ok", "https://x/"

    monkeypatch.setattr(monitor, "_probe", fake_probe)

    item_key = monitor._item_key(monitor.items[0])
    failures: list[str] = []
    for index, variant in enumerate(_dirty_states({"name": "站点"}, keys)):
        monitor.state.set("monitors", {"items": {item_key: variant}})
        try:
            monitor._prune_state()
            _run(monitor._check_item(monitor.items[0]))
        except Exception as exc:
            failures.append(f"key={variant!r:.60} -> {exc.__class__.__name__}: {exc}")
    assert not failures, "\n".join(failures[:8])


def test_g2_dirty_report_delivery_state_never_raises(tmp_path) -> None:
    """Every dirt shape in report_delivery survives the restore round."""

    from astrbot_plugin_statuscard.report import DailyReporter
    from astrbot_plugin_statuscard.state import StateStore

    keys = [
        "last_sent_day",
        "last_attempt_day",
        "pending_retry",
        "pending_retry_count",
        "pending_retry_day",
        "pending_image_retry",
        "pending_image_retry_report_day",
        "pending_image_retry_send_day",
        "image_retry_attempts",
        "offline_retry_count",
        "offline_retry_ts",
        "weekly_last_sent_week",
        "weekly_last_sent_slot",
        "weekly_pending",
        "weekly_pending_report_day",
        "weekly_retry_count",
        "weekly_image_attempts",
        "weekly_image_pending",
        "weekly_image_report_day",
        "weekly_given_up_week",
        "weekly_offline_retry_count",
        "weekly_offline_retry_ts",
        "daily_given_up_day",
    ]
    failures: list[str] = []
    for index, dirt_key in enumerate(keys):
        for dirt in _DIRTY:
            store = StateStore(tmp_path / f"state_{index}.json")
            store.set("report_delivery", {dirt_key: dirt})
            plugin = _BarePlugin()
            plugin.state = store
            reporter = DailyReporter(
                plugin, store, {"display": {"timezone": "Asia/Shanghai"}}
            )
            try:
                reporter._restore_delivery_state()
            except Exception as exc:
                failures.append(
                    f"{dirt_key}={dirt!r:.40} -> {exc.__class__.__name__}: {exc}"
                )
    assert not failures, "\n".join(failures[:8])


def _dirty_db(tmp_path) -> Path:
    """A report.db seeded with every documented dirt shape per table."""

    db = tmp_path / "dirty.db"
    store = __import__(
        "astrbot_plugin_statuscard.report_store", fromlist=["ReportStore"]
    ).ReportStore(db)
    store.close() if hasattr(store, "close") else None
    connection = sqlite3.connect(db)
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    rows = [
        # (ts, day, kind, name, detail, status, target, resolved_ts, started_ts, item_key)
        (0, day, "service", "svc", "ts0", "alert", "t", None, None, None),
        (-5, day, "service", "svc", "tsneg", "failed", "t", None, None, None),
        ("abc", day, "service", "svc", "tstext", "alert", "t", None, None, None),
        (now, day, "service", None, "nullname", "alert", None, None, None, None),
        (now, day, "service", "svc", "badstatus", "xyz", "t", None, None, None),
        (now, day, "service", "svc", "resolved_inf", "alert", "t", "inf", None, None),
        (
            now,
            day,
            "service",
            "svc",
            "resolved_text",
            "recovery",
            "t",
            "abc",
            None,
            None,
        ),
        (now, day, "service", "svc", "started_text", "alert", "t", None, "abc", "k1"),
        (10**18, day, "service", "svc", "ts_huge", "alert", "t", None, None, None),
    ]
    for row in rows:
        connection.execute(
            "INSERT INTO events (ts, day, kind, name, detail, status, target,"
            " resolved_ts, started_ts, item_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    # Dirty resource samples and snapshots.
    for offset, (cpu, ram) in enumerate(
        ((float("nan"), -1), ("abc", 10.0**300), (None, 10.0**300))
    ):
        connection.execute(
            "INSERT INTO resource_samples (ts, cpu, ram, swap, disk, load1,"
            " net_up, net_down, disk_read, disk_write) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now + offset, cpu, ram, None, None, None, None, None, None, None),
        )
    connection.execute(
        "INSERT INTO daily_snapshots (day, friends, groups, members,"
        " recv_total, complete, captured_at) VALUES (?,?,?,?,?,?,?)",
        (day, "abc", None, -1, "inf", 0, "not-a-time"),
    )
    connection.commit()
    connection.close()
    return db


def test_g2_dirty_report_db_never_breaks_the_build(tmp_path) -> None:
    """A fully dirty report.db must not abort the report or the dashboard."""

    from astrbot_plugin_statuscard.report_store import ReportStore
    from astrbot_plugin_statuscard.report_view import (
        DailyReportBuilder,
        build_report_text,
    )

    store = ReportStore(_dirty_db(tmp_path))
    builder = DailyReportBuilder(store, {"display": {"timezone": "Asia/Shanghai"}})
    failures: list[str] = []
    try:
        view = builder.build(span_days=1)
        text = build_report_text(view)
        assert isinstance(text, str) and text
        store.list_unresolved_alerts(int(time.time()) + 60, 0)
    except Exception as exc:
        failures.append(f"{exc.__class__.__name__}: {exc}")
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# G1: symmetry contract -- one scenario, two engines, same semantics
# ---------------------------------------------------------------------------

# Explicitly-declared differences between the two sides (do NOT turn these
# into failures; they are intentional, documented designs):
#   * alert retry budget: alerts gives up after 3 attempts, monitor after 10
#     ("an outage is still real" -- monitor.py MAX_ALERT_RETRIES comment).
#   * monitor carries `pending` / `send_exhausted`; alerts does not.
#   * monitor has suppress_when_down + maintenance silence; alerts does not.
#   * failed-event rows: alerts writes one on first send failure inside the
#     offline branch; monitor writes it once outside.
# Everything else that a user can SEE (was an alert pushed? was a recovery
# queued behind the summary? did the offline path preserve the retry budget?)
# must agree.


def _classify(texts: list[str]) -> list[str]:
    """Normalize pushed texts into semantic nodes.

    alerts pushes "⚠️ 系统告警 · X" / "✅ 已恢复 · X"; monitor pushes
    "🔴 服务异常 · X" / "✅ 服务恢复 · X". Match on the wording, not the
    emoji, so both sides normalize onto the same nodes.
    """

    out = []
    for text in texts:
        if "系统告警" in text or "服务异常" in text or "🔴" in text:
            out.append("ALERT")
        elif "已恢复" in text or "服务恢复" in text:
            out.append("RECOVERY")
        else:
            out.append("OTHER")
    return out


class _AlertHarness:
    """Drives the resource engine through abstract fault/recover steps."""

    def __init__(self, tmp_path, aggregator=None, store=None) -> None:
        from astrbot_plugin_statuscard.alerts import AlertEngine
        from astrbot_plugin_statuscard.state import StateStore

        self.state = store or StateStore(tmp_path / "alerts_state.json")
        config = {
            "alerts": {
                "enabled": True,
                "cpu_threshold": 85.0,
                "mem_threshold": 90.0,
                "sustain_samples": 2,
                "cooldown_seconds": 3600,
                "interval_seconds": 30,
            },
            "display": {"timezone": "Asia/Shanghai"},
        }
        self.engine = AlertEngine(
            _BarePlugin(), self.state, config, report_store=None, aggregator=aggregator
        )
        self.texts: list[str] = []

        async def fake_send(text, source="", kind=""):
            self.texts.append(text)
            from astrbot_plugin_statuscard.delivery import DeliveryResult

            return DeliveryResult(ok=True)

        self.engine._send = fake_send

    def fail(self, key: str = "cpu") -> None:
        value = 90.0 if key == "cpu" else 95.0
        threshold = 85.0 if key == "cpu" else 90.0
        _run(
            self.engine._evaluate(key, key.upper(), value, threshold, "%", f"{value}%")
        )

    def recover(self, key: str = "cpu") -> None:
        value = 10.0 if key == "cpu" else 15.0
        threshold = 85.0 if key == "cpu" else 90.0
        _run(
            self.engine._evaluate(key, key.upper(), value, threshold, "%", f"{value}%")
        )

    def semantics(self, key: str = "cpu") -> dict:
        rules = self.state.get("alerts", {}).get("rules", {})
        st = rules.get(key, {})
        return {
            "alerting": bool(st.get("alerting")),
            "notified": bool(st.get("alerted")),
            "queued_recovery": bool(st.get("pending_recovery")),
            "waits_summary": bool(st.get("recovery_awaits_summary")),
            "offline_backoff": int(st.get("offline_retry_count") or 0),
            "retries": int(st.get("retry_count") or 0),
        }

    def nodes(self) -> list[str]:
        return _classify(self.texts)


class _MonitorHarness:
    """Drives the service monitor through the SAME abstract steps."""

    def __init__(self, tmp_path, aggregator=None, store=None) -> None:
        from astrbot_plugin_statuscard.monitor import ServiceMonitor
        from astrbot_plugin_statuscard.state import StateStore

        self.state = store or StateStore(tmp_path / "monitor_state.json")
        config = {
            "monitors": {
                "enabled": True,
                "fail_threshold": 2,
                "cooldown_seconds": 3600,
                "interval": 30,
                "items": [
                    {
                        "__template_key": "http",
                        "name": "CPU",
                        "url": "https://cpu/",
                        "enabled": True,
                    },
                    {
                        "__template_key": "http",
                        "name": "MEM",
                        "url": "https://mem/",
                        "enabled": True,
                    },
                ],
            },
            "display": {"timezone": "Asia/Shanghai"},
        }
        self.monitor = ServiceMonitor(
            _BarePlugin(), self.state, config, report_store=None, aggregator=aggregator
        )
        self.texts: list[str] = []
        self.probe_ok = {"CPU": False, "MEM": False}

        async def fake_send(text):
            self.texts.append(text)
            from astrbot_plugin_statuscard.delivery import DeliveryResult

            return DeliveryResult(ok=True)

        self.monitor._send = fake_send

        async def fake_probe(item):
            ok = self.probe_ok.get(str(item.get("name")), False)
            return ok, "detail", str(item.get("url") or "")

        self.monitor._probe = fake_probe

    def fail(self, key: str = "CPU") -> None:
        self.probe_ok[key] = False
        item = next(i for i in self.monitor.items if i["name"] == key)
        _run(self.monitor._check_item(item))

    def recover(self, key: str = "CPU") -> None:
        self.probe_ok[key] = True
        item = next(i for i in self.monitor.items if i["name"] == key)
        _run(self.monitor._check_item(item))

    def semantics(self, name: str = "CPU") -> dict:
        items = self.state.get("monitors", {}).get("items", {})
        key = self.monitor._item_key(
            {"__template_key": "http", "name": name, "url": f"https://{name.lower()}/"}
        )
        st = items.get(key, {})
        return {
            "alerting": bool(st.get("alerting")),
            "notified": bool(st.get("alerted")),
            "queued_recovery": bool(st.get("pending_recovery")),
            "waits_summary": bool(st.get("recovery_awaits_summary")),
            "offline_backoff": int(st.get("offline_retry_count") or 0),
            "retries": int(st.get("retry_count") or 0),
        }

    def nodes(self) -> list[str]:
        return _classify(self.texts)


def test_g1_normal_episode_symmetric(tmp_path) -> None:
    """Two consecutive failures alert; the next pass recovers. Both sides."""

    alert = _AlertHarness(tmp_path)
    monitor = _MonitorHarness(tmp_path)

    # Run-up + latch.
    alert.fail()
    alert.fail()
    monitor.fail()
    monitor.fail()
    assert alert.nodes() == ["ALERT"], alert.nodes()
    assert monitor.nodes() == ["ALERT"], monitor.nodes()
    assert alert.semantics()["alerting"] is True
    assert monitor.semantics()["alerting"] is True
    assert alert.semantics()["notified"] is True
    assert monitor.semantics()["notified"] is True

    # Recovery.
    alert.recover()
    monitor.recover()
    assert alert.nodes() == ["ALERT", "RECOVERY"], alert.nodes()
    assert monitor.nodes() == ["ALERT", "RECOVERY"], monitor.nodes()
    assert alert.semantics()["alerting"] is False
    assert monitor.semantics()["alerting"] is False


def test_g1_cooldown_relatch_records_without_pushing(tmp_path) -> None:
    """A second episode inside the cooldown is recorded, not pushed."""

    alert = _AlertHarness(tmp_path)
    monitor = _MonitorHarness(tmp_path)

    for side in (alert, monitor):
        side.fail()
        side.fail()
        side.recover()

    # Both sides pushed exactly one alert + one recovery so far.
    assert alert.nodes() == ["ALERT", "RECOVERY"], alert.nodes()
    assert monitor.nodes() == ["ALERT", "RECOVERY"], monitor.nodes()

    # Immediately fail again, past the sustain threshold: latched, silent.
    for _ in range(2):
        alert.fail()
        monitor.fail()

    assert alert.semantics()["alerting"] is True, alert.semantics()
    assert monitor.semantics()["alerting"] is True, monitor.semantics()
    assert alert.nodes() == ["ALERT", "RECOVERY"], (
        f"alerts 冷却内不应再推送: {alert.nodes()}"
    )
    assert monitor.nodes() == ["ALERT", "RECOVERY"], (
        f"monitor 冷却内不应再推送: {monitor.nodes()}"
    )


def test_g1_offline_failure_preserves_retry_budget(tmp_path) -> None:
    """A send failure during a latched transport outage must not burn budget."""

    from astrbot_plugin_statuscard.delivery import DeliveryResult
    from astrbot_plugin_statuscard.state import StateStore

    # One shared StateStore, like the real deployment: monitor's
    # _transport_offline() reads the alerts.offline latch from the SAME file.
    shared = StateStore(tmp_path / "shared_state.json")
    alert = _AlertHarness(tmp_path, store=shared)
    monitor = _MonitorHarness(tmp_path, store=shared)

    # Latch the transport outage: BOTH sides read alerts.offline.alerting.
    shared.set("alerts", {"offline": {"alerting": True}})

    # Make the sends fail (plain failure, transport latched offline).
    async def fail_send(*args, **kwargs):
        return DeliveryResult(ok=False)

    alert.engine._send = fail_send
    monitor.monitor._send = fail_send

    alert.fail()
    alert.fail()
    monitor.fail()
    monitor.fail()

    a, m = alert.semantics(), monitor.semantics()
    assert a["offline_backoff"] > 0, a
    assert m["offline_backoff"] > 0, m
    # The regular retry budget stays untouched while the channel is down.
    assert a["retries"] == 0, a
    assert m["retries"] == 0, m


def test_g1_recovery_queues_behind_undelivered_summary(tmp_path) -> None:
    """A recovery with an undelivered summary queues on BOTH sides.

    Two rules fail at once: the first alert is pushed directly, the second is
    buffered for the summary. While that summary has not reached the chat,
    the recovery must queue (pending_recovery + recovery_awaits_summary) --
    the v2.20.14 incident had the recovery overtake the summary and announce
    a recovery for an outage the user never saw.
    """

    from astrbot_plugin_statuscard.aggregator import AlertAggregator

    agg = AlertAggregator(window_seconds=30)
    alert = _AlertHarness(tmp_path, aggregator=agg)
    monitor_agg = AlertAggregator(window_seconds=30)
    monitor = _MonitorHarness(tmp_path, aggregator=monitor_agg)

    # First alert of each window is pushed directly, second one is buffered.
    # Two rounds per rule: sustain/fail_threshold = 2.
    alert.fail("cpu")
    alert.fail("cpu")
    alert.fail("mem")
    alert.fail("mem")
    monitor.fail("CPU")
    monitor.fail("CPU")
    monitor.fail("MEM")
    monitor.fail("MEM")

    assert alert.nodes() == ["ALERT"], alert.nodes()  # cpu direct; mem buffered
    assert monitor.nodes() == ["ALERT"], monitor.nodes()

    # The DIRECTLY-pushed rule (cpu) recovers: its alert already reached the
    # chat, so its recovery goes out immediately -- and it must do so on both
    # sides symmetrically.
    alert.recover("cpu")
    monitor.recover("CPU")
    assert alert.nodes() == ["ALERT", "RECOVERY"], alert.nodes()
    assert monitor.nodes() == ["ALERT", "RECOVERY"], monitor.nodes()

    # The BUFFERED rule (mem) recovers while the summary is still pending:
    # its recovery must queue behind the summary on BOTH sides, because the
    # user has not seen its alert yet (v2.20.14 incident).
    alert.recover("mem")
    monitor.recover("MEM")

    a, m = alert.semantics("mem"), monitor.semantics("MEM")
    assert a["queued_recovery"] is True, a
    assert a["waits_summary"] is True, a
    assert m["queued_recovery"] is True, m
    assert m["waits_summary"] is True, m
    # No recovery text for the buffered rule may have gone out.
    assert alert.nodes() == ["ALERT", "RECOVERY"], alert.nodes()
    assert monitor.nodes() == ["ALERT", "RECOVERY"], monitor.nodes()
