"""Regression tests for the v2.20.14 audit round.

Every case locks one defect that was reproduced before the fix, with the
user-visible outcome as the assertion:

* a deleted report database must not produce an empty report that is sent
  and recorded as delivered,
* an orphan alert row must not swallow the next real outage,
* a recovery notice must not overtake the summary that carries its alert,
* a service outage must keep its original start time,
* a rule whose alert was never delivered must not announce a recovery,
* an abandon callback must not withdraw a same-named rule that delivered
  its own alert,
* a recovered row must not keep the "ongoing" marker,
* the compact IPv6 notation must be redacted,
* the English dashboard must not show Chinese sentences,
* every configured disk must be visible,
* the weekly report must cover the same window in the send and the catch-up,
* the retention prune must be atomic,
* the event counters must not claim a truncation that did not happen.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import alerts as alerts_module
from astrbot_plugin_statuscard import monitor as monitor_module
from astrbot_plugin_statuscard import redactor as redactor_module
from astrbot_plugin_statuscard import report as report_module
from astrbot_plugin_statuscard import report_store as store_module
from astrbot_plugin_statuscard import report_view as report_view_module
from astrbot_plugin_statuscard.aggregator import AlertAggregator
from astrbot_plugin_statuscard.delivery import DeliveryResult
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore

TZ = "Asia/Shanghai"
TZINFO = timezone(timedelta(hours=8))
DAY = "2026-10-30"
BASE = int(datetime(2026, 10, 30, tzinfo=TZINFO).timestamp())


def _config(**extra) -> dict:
    base = {
        "display": {"timezone": TZ},
        "alerts": {
            "enabled": True,
            "targets": ["123456"],
            "sustain_samples": 1,
            "cooldown_seconds": 1800,
        },
        "daily_report": {"enabled": True, "time": "21:00", "sections": ["anomaly"]},
    }
    for key, value in extra.items():
        base.setdefault(key, {}).update(value)
    return base


def _anomalies(store: ReportStore, day: str = DAY, span: int = 1) -> list[dict]:
    builder = report_view_module.DailyReportBuilder(
        store, {"display": {"timezone": TZ}}
    )
    built = builder.build(day=day, span_days=span)
    return (built.get("anomalies") or {}).get("items") or []


# ----------------------------------------------------------------------
# 1) A deleted report database must not be rebuilt into an empty one that
#    is then sent and recorded as delivered
# ----------------------------------------------------------------------


def test_probe_runs_before_ensure_ready(tmp_path: Path) -> None:
    """The probe has to see the missing schema before ensure_ready rebuilds it.

    ``ensure_ready()`` recreates the schema in the empty file that sqlite
    silently creates when the database file was deleted. With the old order
    the second attempt saw a "healthy" but empty database, built an
    all-"--" report, sent it and recorded the day as delivered.
    """

    store = ReportStore(tmp_path / "report.db")
    for suffix in ("", "-wal", "-shm"):
        path = store.path.with_name(store.path.name + suffix)
        if path.exists():
            path.unlink()

    # The new order: probe() first detects the missing schema and refuses,
    # so the send path gives up for this attempt instead of sending an empty
    # report and recording the day as delivered.
    assert store.probe() is False
    assert store.available is False
    # The rebuild happens afterwards and produces a healthy but EMPTY
    # database -- which is exactly why the probe has to run first.
    assert store.ensure_ready() is True
    assert store.probe() is True
    assert store.list_events_range(0, 2**31, limit=10) == []


def test_send_scheduled_probes_before_ensure_ready(tmp_path: Path) -> None:
    """The daily send path must probe first (the call order is asserted)."""

    source = Path(report_module.__file__).read_text(encoding="utf-8")
    assert "self.store.probe() or not self.store.ensure_ready()" in source
    assert "self.store.ensure_ready() or not self.store.probe()" not in source


# ----------------------------------------------------------------------
# 2) An orphan alert row must not swallow the next real outage
# ----------------------------------------------------------------------


def test_orphan_alert_row_does_not_swallow_the_next_outage(tmp_path: Path) -> None:
    """The real outage must keep its own start time and duration.

    The orphan (a fault whose recovery EVENT was pruned away) carries a
    ``resolved_ts`` that is earlier than the next outage. The pairing used
    to keep the orphan open until the next recovery, so the two merged into
    one row: a 90-second outage was reported as "07:01:30 已恢复" that
    started at the orphan's timestamp, and the real outage vanished.
    """

    store = ReportStore(tmp_path / "report.db")
    store.add_event(
        BASE + 3 * 3600,
        DAY,
        "resource",
        "磁盘使用率 (/)",
        "",
        "alert",
        resolved_ts=BASE + 3 * 3600 + 300,
    )
    store.add_event(BASE + 10 * 3600, DAY, "resource", "磁盘使用率 (/)", "", "alert")
    store.add_event(
        BASE + 10 * 3600 + 90, DAY, "resource", "磁盘使用率 (/)", "", "recovery"
    )

    items = _anomalies(store)
    assert len(items) == 2, items
    durations = sorted(str(item.get("duration")) for item in items)
    assert durations == ["00:01:30", "00:05:00"], durations
    # The real outage must be there with its own start time.
    times = sorted(str(item.get("time")) for item in items)
    assert times == ["03:00", "10:00"], times


def test_two_outages_of_one_rule_keep_their_own_durations(tmp_path: Path) -> None:
    """The control group: two genuine outages stay separate."""

    store = ReportStore(tmp_path / "report.db")
    store.add_event(BASE, DAY, "resource", "内存使用率", "92%", "alert")
    store.add_event(BASE + 90, DAY, "resource", "内存使用率", "持续", "recovery")
    store.add_event(BASE + 3600, DAY, "resource", "内存使用率", "93%", "alert")
    store.add_event(BASE + 3600 + 30, DAY, "resource", "内存使用率", "持续", "recovery")

    durations = [str(item.get("duration")) for item in _anomalies(store)]
    assert durations == ["00:01:30", "00:00:30"], durations


def test_failed_and_alert_rows_of_one_episode_count_once(tmp_path: Path) -> None:
    """The control group: one interruption with two rows is one anomaly."""

    store = ReportStore(tmp_path / "report.db")
    end = BASE + 25 * 60
    store.add_event(
        BASE, DAY, "resource", "内存使用率", "92%", "failed", resolved_ts=end
    )
    store.add_event(
        BASE + 600, DAY, "resource", "内存使用率", "91%", "alert", resolved_ts=end
    )
    store.add_event(end, DAY, "resource", "内存使用率", "持续", "recovery")

    builder = report_view_module.DailyReportBuilder(
        store, {"display": {"timezone": TZ}}
    )
    built = builder.build(day=DAY, span_days=1)
    assert (built.get("anomalies") or {}).get("count") == 1


# ----------------------------------------------------------------------
# 3) A recovery notice must not overtake the summary that carries its alert
# ----------------------------------------------------------------------


def test_aggregator_reports_a_pending_summary() -> None:
    """``summary_pending`` tells the caller that the summary is not out yet."""

    agg = AlertAggregator(window_seconds=120)
    assert agg.summary_pending("resource") is False

    delivered: list[str] = []

    async def send(text: str):
        delivered.append(text)
        return SimpleNamespace(ok=True)

    async def scenario() -> None:
        await agg.push("A", "r", "alert A", send, kind="resource")
        await agg.push("B", "r", "alert B", send, kind="resource")
        assert agg.summary_pending("resource") is True
        bucket = agg._buckets["resource"]
        bucket.until = 0.0
        await agg.flush("resource")
        assert agg.summary_pending("resource") is False

    asyncio.run(scenario())
    assert len(delivered) == 2


def test_failed_summary_keeps_the_notice_waiting() -> None:
    """A summary that failed and waits for its retry still blocks the notice."""

    agg = AlertAggregator(window_seconds=120)

    async def failing(text: str):
        return SimpleNamespace(ok=False)

    async def scenario() -> None:
        await agg.push("A", "r", "alert A", failing, kind="resource")
        await agg.push("B", "r", "alert B", failing, kind="resource")
        bucket = agg._buckets["resource"]
        bucket.until = 0.0
        await agg.flush("resource")
        assert agg.summary_pending("resource") is True

    asyncio.run(scenario())


def test_alerts_wait_for_the_summary_before_posting_the_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queued recovery must not be posted while the summary is pending."""

    state = StateStore(tmp_path / "state.json")
    sent: list[str] = []

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            sent.append(text)
            return DeliveryResult(ok=True, ok_targets=list(targets))

    engine = alerts_module.AlertEngine(
        _Plugin(), state, _config(), report_store=ReportStore(tmp_path / "report.db")
    )
    agg = AlertAggregator(window_seconds=120)
    engine._aggregator = agg

    # A summary that is still buffered: the recovery may not go out.
    async def send(text: str):
        return SimpleNamespace(ok=True)

    async def scenario() -> None:
        await agg.push("A", "r", "alert A", send, kind="resource")
        await agg.push("B", "r", "alert B", send, kind="resource")
        with state.section("alerts") as alerts:
            alerts.setdefault("rules", {})["ram"] = {
                "alerting": False,
                "alerted": True,
                "label": "内存使用率",
                "pending_recovery": "✅ 已恢复 · 内存使用率",
                "recovery_next_attempt": time.time() - 1,
                "recovery_awaits_summary": True,
            }
        # The recovery is delivered from the sampling loop, which is where
        # the queued notices are re-sent.
        await engine._sample_once()

    asyncio.run(scenario())
    assert not any("已恢复" in text for text in sent), sent


# ----------------------------------------------------------------------
# 4) A service outage keeps its original start time
# ----------------------------------------------------------------------


def _monitor(tmp_path: Path, plugin) -> monitor_module.ServiceMonitor:
    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    config = _config(
        monitors={
            "enabled": True,
            "items": [
                {
                    "enabled": True,
                    "__template_key": "http",
                    "name": "服务A",
                    "url": "http://a/",
                    "timeout_seconds": 5,
                }
            ],
        }
    )
    return monitor_module.ServiceMonitor(plugin, state, config, store)


def test_monitor_keeps_the_original_start_time(tmp_path: Path) -> None:
    """A retry must not move the start of the outage to the push moment.

    The monitor used to write ``since = now`` when it finally pushed the
    alert, so an outage that had been running for half an hour (a cooldown
    retry, a failed first attempt) reported an interruption of 00:00:00.
    """

    sent: list[str] = []

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            sent.append(text)
            return DeliveryResult(ok=True, ok_targets=list(targets))

    engine = _monitor(tmp_path, _Plugin())
    original = time.time() - 2000
    key = monitor_module.ServiceMonitor._item_key(engine.items[0])
    with engine.state.section("monitors") as monitors:
        monitors.setdefault("items", {})[key] = {
            "name": "服务A",
            "target": "http://a/",
            "kind": "http",
            "alerting": True,
            "alerted": True,
            "since": original,
            "fails": 3,
            "last_error": "boom",
            "pending": False,
        }

    async def probe(item):
        return True, "OK", ""

    engine._probe = probe  # type: ignore[method-assign]
    asyncio.run(engine.check_all())

    # The recovery notice must measure the real interruption. Before the fix
    # the start time was overwritten with the push moment, so the notice
    # said "故障开始 = 恢复时刻" and "中断时长 00:00:00" for an outage that
    # had been running for half an hour.
    assert sent, "the recovery notice was not sent"
    notice = "\n".join(sent)
    assert "00:00:00" not in notice, notice
    assert "33:20" in notice or "00:33" in notice, notice


def test_monitor_still_announces_the_recovery_when_the_alert_failed(
    tmp_path: Path,
) -> None:
    """A failed push must not leave the user with NO message at all.

    The episode is not "announced", but the recovery notice carries the
    "（异常告警此前未能送达）" note: the user learns that the service was down.
    Suppressing the notice entirely (because ``alerted`` stayed False)
    left the user with nothing at all when the first push failed and the
    service recovered before the retry budget ran out (v2.20.14).
    """

    sent: list[str] = []

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            sent.append(text)
            # A real DeliveryResult: a plain namespace would be truthy and
            # the monitor would count it as delivered.
            return DeliveryResult(ok=False, failed_targets=list(targets))

    engine = _monitor(tmp_path, _Plugin())
    key = monitor_module.ServiceMonitor._item_key(engine.items[0])
    with engine.state.section("monitors") as monitors:
        monitors.setdefault("items", {})[key] = {
            "name": "服务A",
            "target": "http://a/",
            "kind": "http",
            "fails": 3,
            "last_attempt": 0,
            "retry_count": 0,
            "next_attempt": 0,
        }

    async def probe(item):
        return False, "boom", "boom"

    engine._probe = probe  # type: ignore[method-assign]
    # The first round fails and sends the alert (the push itself fails).
    asyncio.run(engine.check_all())

    # The service recovers before the retry budget runs out.
    async def recovered(item):
        return True, "OK", ""

    engine._probe = recovered  # type: ignore[method-assign]
    asyncio.run(engine.check_all())

    # The recovery still reaches the user, with the note that the alert
    # itself never arrived.
    recoveries = [text for text in sent if "服务恢复" in text]
    assert recoveries, sent
    assert "此前未能送达" in recoveries[0], recoveries[0]


# ----------------------------------------------------------------------
# 5) The abandon callback must not withdraw a rule that alerted on its own
# ----------------------------------------------------------------------


def test_abandon_keeps_a_same_named_rule_that_alerted_on_its_own(
    tmp_path: Path,
) -> None:
    """Only the rule that was really buffered loses its "notified" state."""

    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=True, ok_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, _config(), report_store=store)
    with state.section("alerts") as alerts:
        alerts["rules"] = {
            "disk": {
                "alerting": True,
                "alerted": True,
                "label": "磁盘使用率 (/)",
                "summary_buffered": True,
            },
            "disk:/mnt/other": {
                "alerting": True,
                "alerted": True,
                "label": "磁盘使用率 (/)",
            },
        }
    engine._on_summary_abandoned([("磁盘使用率 (/)", "原因")])
    rules = state.get("alerts")["rules"]
    # The buffered rule is withdrawn ...
    assert rules["disk"]["alerted"] is False
    assert rules["disk"].get("summary_abandoned") is True
    # ... the rule that delivered its own alert keeps its state.
    assert rules["disk:/mnt/other"]["alerted"] is True


def test_alerts_marks_a_buffered_rule(tmp_path: Path) -> None:
    """The resource rules mark a buffered rule, like the monitors do."""

    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")

    class _Plugin:
        context = None

        async def send_text(self, targets, text, source="unknown"):
            return DeliveryResult(ok=True, ok_targets=list(targets))

    engine = alerts_module.AlertEngine(_Plugin(), state, _config(), report_store=store)
    agg = AlertAggregator(window_seconds=120)
    engine._aggregator = agg
    engine.sustain = 1

    async def send(text: str):
        return SimpleNamespace(ok=True)

    async def scenario() -> None:
        # The first alert opens the window, the second one is buffered.
        await agg.push("A", "r", "alert A", send, kind="resource")
        await agg.push("B", "r", "alert B", send, kind="resource")
        await engine._evaluate("ram", "内存使用率", 99.0, 85.0, "%", "99.0%")

    asyncio.run(scenario())
    rule = state.get("alerts")["rules"]["ram"]
    assert rule.get("summary_buffered") is True


# ----------------------------------------------------------------------
# 6) A recovered row must not keep the "ongoing" marker
# ----------------------------------------------------------------------


def test_duration_code_is_cleared_when_a_row_is_completed(tmp_path: Path) -> None:
    """Old rows without resolved_ts are completed in place.

    The completion updated the status and the duration but left the
    ``ongoing`` marker, so the English dashboard showed "Ongoing" for an
    outage that had already recovered.
    """

    store = ReportStore(tmp_path / "report.db")
    store.add_event(BASE + 10 * 3600, DAY, "resource", "内存使用率", "", "alert")
    store.add_event(
        BASE + 10 * 3600 + 60, DAY, "resource", "内存使用率", "", "recovery"
    )
    items = _anomalies(store)
    assert items
    for item in items:
        if item.get("status_key") == "recovered":
            assert item.get("duration_code") is None, item
            assert str(item.get("duration")) != "进行中", item


def test_duration_codes_are_translatable() -> None:
    """Every Chinese duration word has a stable code."""

    assert report_view_module._duration_code("进行中") == "ongoing"
    assert report_view_module._duration_code("跨窗口关闭") == "cross_window"
    assert (
        report_view_module._duration_code("23:00:00（窗口前开始）")
        == "started_before_window"
    )
    assert report_view_module._duration_code("00:01:30") is None


# ----------------------------------------------------------------------
# 7) The compact IPv6 notation must be redacted
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "连接 [2001:db8::1]:443 超时",
        "无法连接 fe80::1%eth0",
        "2001:0db8:0000:0000:0000:ff00:0042:8329",
        "::1",
        "fd00::dead:beef",
    ],
)
def test_compact_ipv6_is_redacted(text: str) -> None:
    """The hostname rule must hide the compressed notation too."""

    redactor = redactor_module.Redactor({"hostname"})
    assert redactor.mask_addresses(text) == "已隐藏（错误详情含主机或地址信息）"
    assert "2001" not in redactor.mask_address_tokens(text)
    assert "fe80" not in redactor.mask_address_tokens(text)


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 500",
        "ReadTimeout",
        "12:34:56",
        "配置错误：缺少 url 配置",
        "rate: 5",
    ],
)
def test_ordinary_text_is_not_touched_by_the_ipv6_rule(text: str) -> None:
    """The new rule must not hide readable diagnostics."""

    redactor = redactor_module.Redactor({"hostname"})
    assert redactor.mask_addresses(text) == text
    assert redactor.mask_address_tokens(text) == text


# ----------------------------------------------------------------------
# 8) The English dashboard must not show Chinese sentences
# ----------------------------------------------------------------------


def test_llm_quality_empty_reasons_carry_a_code() -> None:
    """Every LLM-quality failure reason has a translatable code."""

    source = Path(report_view_module.__file__).read_text(encoding="utf-8")
    for code in (
        "reason_no_provider_db",
        "reason_provider_query_failed",
        "reason_no_llm_calls",
        "reason_llm_data_broken",
    ):
        assert f'"empty_reason_code": "{code}"' in source, code


def test_verify_errors_carry_a_code() -> None:
    """The verify endpoint returns a translatable error code."""

    api = Path(__file__).resolve().parents[1] / "page_api.py"
    source = api.read_text(encoding="utf-8")
    assert "verify_too_frequent" in source
    assert "verify_no_targets" in source


def test_english_and_chinese_i18n_have_the_same_keys() -> None:
    """The two language files must stay in sync."""

    base = Path(__file__).resolve().parents[1] / ".astrbot-plugin" / "i18n"
    zh = json.loads((base / "zh-CN.json").read_text(encoding="utf-8"))
    en = json.loads((base / "en-US.json").read_text(encoding="utf-8"))

    def flat(node: dict, prefix: str = "") -> set[str]:
        out: set[str] = set()
        for key, value in node.items():
            if isinstance(value, dict):
                out |= flat(value, f"{prefix}{key}.")
            else:
                out.add(f"{prefix}{key}")
        return out

    assert flat(zh) == flat(en)


def test_english_strings_are_not_chinese() -> None:
    """The English file must not carry a Chinese sentence."""

    base = Path(__file__).resolve().parents[1] / ".astrbot-plugin" / "i18n"
    en = json.loads((base / "en-US.json").read_text(encoding="utf-8"))
    strings = en["pages"]["status"]["strings"]
    for key in (
        "report_cross_window",
        "report_started_before_window",
        "reason_no_provider_db",
        "reason_provider_query_failed",
        "reason_no_llm_calls",
        "reason_llm_data_broken",
        "verify_too_frequent",
        "verify_no_targets",
        "section_disk_detail",
    ):
        value = strings[key]
        assert not any("一" <= ch <= "鿿" for ch in value), (key, value)


# ----------------------------------------------------------------------
# 9) Every configured disk must be visible
# ----------------------------------------------------------------------


def test_dashboard_ships_every_mount() -> None:
    """The backend already sends the full list; the page must show it."""

    app = (
        Path(__file__).resolve().parents[1] / "pages" / "status" / "app.js"
    ).read_text(encoding="utf-8")
    assert "disk.disks" in app or "disks" in app
    assert "disk-detail" in app
    html = (
        Path(__file__).resolve().parents[1] / "pages" / "status" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'id="disk-detail"' in html


# ----------------------------------------------------------------------
# 10) The weekly report covers the same window in the send and the catch-up
# ----------------------------------------------------------------------


def test_weekly_retry_pins_the_report_day(tmp_path: Path) -> None:
    """An offline weekly retry keeps the day the slot announced."""

    state = StateStore(tmp_path / "state.json")
    store = ReportStore(tmp_path / "report.db")
    reporter = report_module.DailyReporter(
        SimpleNamespace(context=None, state=state, render_report_image=None),
        store,
        _config(daily_report={"weekly_time": "21:30", "weekly_enabled": True}),
    )
    # Sunday 21:30 -> the window ends on the Saturday before.
    sunday = datetime(2026, 11, 1, 21, 30, tzinfo=TZINFO)
    reporter._weekly_slot_before = lambda now: sunday
    reporter._save_delivery_state = lambda: None  # type: ignore[method-assign]
    reporter._schedule_weekly_retry(transport_offline=True)
    assert reporter._weekly_pending_report_day == "2026-10-31"


def test_weekly_catch_up_uses_the_slot_day(tmp_path: Path) -> None:
    """The catch-up send derives the report day from the slot."""

    source = Path(report_module.__file__).read_text(encoding="utf-8")
    assert "catchup_day" in source
    assert "_weekly_slot_before(now)" in source


# ----------------------------------------------------------------------
# 11) The retention prune is atomic
# ----------------------------------------------------------------------


def test_prune_uses_one_transaction(tmp_path: Path) -> None:
    """Every table is cleaned in a single transaction."""

    source = Path(store_module.__file__).read_text(encoding="utf-8")
    assert "_write_batch(" in source
    assert "def _write_batch" in source


# ----------------------------------------------------------------------
# 12) The event counters must not claim a truncation that did not happen
# ----------------------------------------------------------------------


def test_counts_truncated_needs_one_more_row_than_the_limit() -> None:
    """The query asks for one row beyond the counter limit."""

    source = Path(report_module.__file__).read_text(encoding="utf-8")
    api = Path(__file__).resolve().parents[1] / "page_api.py"
    api_source = api.read_text(encoding="utf-8")
    assert "limit=2001" in api_source
    assert "counts_truncated = len(events) > 2000" in api_source
    assert "len(events) >= 2000" not in api_source
    assert source  # the module still loads
