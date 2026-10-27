"""Regression tests for the v2.11.0 reliability fixes.

Each test pins down a defect that was reproduced against the live plugin:

* P0-1 a lost OneBot receipt must not trigger a second delivery
* P0-2 the fallback send path must not repeat a message that may have
  already been delivered
* P0-3 the state store must survive a concurrent writer
* P0-4 the daily report retry counter must not carry over to the next day
* P1-4 ``redact_url`` must strip inline credentials
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from astrbot_plugin_statuscard.onebot_receipt import (
    is_definitely_not_sent,
    send_direct,
    should_retry_send,
)
from astrbot_plugin_statuscard.report import DailyReporter
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore
from astrbot_plugin_statuscard.utils import redact_url

# ----------------------------------------------------------------------
# P0-1: a lost receipt must not cause a duplicate delivery
# ----------------------------------------------------------------------


class _TimeoutClient:
    """A client that accepts the request but never answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_action(self, action, **params):
        self.calls += 1
        await asyncio.sleep(5)
        return {"message_id": 1}


class _UnavailableClient:
    """A client whose OneBot endpoint cannot be reached at all."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_action(self, action, **params):
        self.calls += 1
        raise RuntimeError("API root url invalid")


class _NetworkErrorClient:
    """A client whose request frame was written but the receipt was lost."""

    def __init__(self) -> None:
        self.calls = 0

    async def call_action(self, action, **params):
        self.calls += 1
        raise RuntimeError("WebSocket API call timeout")


def test_lost_receipt_marks_send_as_attempted() -> None:
    """A timed-out receipt must not be treated as "never sent"."""

    client = _TimeoutClient()
    receipt = asyncio.run(
        send_direct(
            client,
            "default:GroupMessage:123456789",
            [{"type": "text", "data": {"text": "hi"}}],
            timeout=0.05,
        )
    )
    assert receipt.ok is False
    # ``attempted`` tells the caller that the message may already be in the
    # chat, so it must not re-send it.
    assert receipt.attempted is True


def test_unreachable_endpoint_is_not_attempted() -> None:
    """A request that provably never left may still be retried."""

    client = _UnavailableClient()
    receipt = asyncio.run(
        send_direct(
            client,
            "default:GroupMessage:123456789",
            [{"type": "text", "data": {"text": "hi"}}],
            timeout=0.05,
        )
    )
    assert receipt.ok is False
    assert receipt.attempted is False


def test_network_error_is_not_definitely_not_sent() -> None:
    """A lost receipt is not proof that the message was never delivered."""

    receipt = asyncio.run(
        send_direct(
            _NetworkErrorClient(),
            "default:GroupMessage:123456789",
            [{"type": "text", "data": {"text": "hi"}}],
            timeout=0.05,
        )
    )
    assert receipt.ok is False
    assert receipt.attempted is True


def test_is_definitely_not_sent_only_for_unreachable() -> None:
    assert is_definitely_not_sent(RuntimeError("API root url invalid")) is True

    class ApiNotAvailable(Exception):
        pass

    assert is_definitely_not_sent(ApiNotAvailable()) is True
    assert is_definitely_not_sent(RuntimeError("WebSocket API call timeout")) is False
    assert is_definitely_not_sent(ConnectionResetError("reset by peer")) is False


# ----------------------------------------------------------------------
# P0-2: the fallback path must not repeat a delivered message
# ----------------------------------------------------------------------


def test_send_chain_does_not_retry_after_handover() -> None:
    """A transport error after the message was handed over is not retried."""

    exc = ConnectionResetError("websocket closed")
    assert should_retry_send(exc) is False


def test_send_chain_retries_when_never_sent() -> None:
    """An unreachable endpoint is still worth retrying."""

    assert should_retry_send(RuntimeError("API root url invalid")) is True


# ----------------------------------------------------------------------
# P0-3: the state store must survive a concurrent writer
# ----------------------------------------------------------------------


def test_state_store_survives_concurrent_writer(tmp_path: Path) -> None:
    """A worker thread may mutate the state while the loop persists it."""

    store = StateStore(tmp_path / "state.json")
    store.data["seed"] = 1

    stop = threading.Event()

    def worker() -> None:
        index = 0
        while not stop.is_set():
            with store.section("sysinfo_cache") as cache:
                cache[f"k{index % 64}"] = index
            index += 1

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.02)
    for _ in range(60):
        store.save()
    stop.set()
    thread.join(timeout=2)

    # The file must always be valid JSON with the seed intact.
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["seed"] == 1
    assert isinstance(payload.get("sysinfo_cache"), dict)


def test_state_store_save_never_raises_on_concurrent_mutation(
    tmp_path: Path,
) -> None:
    """``save`` must not lose the whole update because of a racing writer."""

    import astrbot_plugin_statuscard.state as state_module

    store = StateStore(tmp_path / "state.json")
    store.data["seed"] = 1

    failures: list[str] = []
    original = state_module.logger.warning

    def spy(message, *args, **kwargs):
        text = str(message)
        if "写入状态文件失败" in text:
            failures.append(text % args if args else text)
        return original(message, *args, **kwargs)

    stop = threading.Event()

    def worker() -> None:
        index = 0
        while not stop.is_set():
            # A writer that bypasses the lock, like a plain dict assignment.
            # The key count stays bounded so the snapshot copy stays cheap.
            store.data[f"k{index % 64}"] = index
            index += 1

    state_module.logger.warning = spy
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    time.sleep(0.02)
    for _ in range(60):
        store.save()
    stop.set()
    thread.join(timeout=2)
    state_module.logger.warning = original

    assert not failures, f"状态保存失败: {failures[:3]}"


def test_state_store_section_is_exclusive(tmp_path: Path) -> None:
    """Mutations through ``section`` are serialised against each other."""

    store = StateStore(tmp_path / "state.json")
    seen: list[int] = []

    def writer(name: str) -> None:
        for _ in range(50):
            with store.section("counters") as counters:
                current = int(counters.get(name) or 0)
                counters[name] = current + 1
                seen.append(current)

    threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    with store.section("counters") as counters:
        assert counters["t0"] == 50
        assert counters["t3"] == 50


# ----------------------------------------------------------------------
# P0-4: the report retry counter is scoped to a single day
# ----------------------------------------------------------------------


class _FakeConfig(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _make_reporter() -> DailyReporter:
    reporter = DailyReporter.__new__(DailyReporter)
    reporter.config = _FakeConfig({"display": {"timezone": "Asia/Shanghai"}})
    reporter._pending_retry = None
    reporter._pending_retry_count = 0
    reporter._pending_retry_day = None
    return reporter


def test_report_retry_counter_resets_each_day(monkeypatch) -> None:
    """A single failure on a new day must not be treated as the third one."""

    reporter = _make_reporter()
    days = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
    current = {"day": "2026-10-19"}
    monkeypatch.setattr(reporter, "_today", lambda: current["day"], raising=False)

    for index, day in enumerate(days):
        current["day"] = day
        reporter._schedule_retry(day)
        if index < len(days) - 1:
            # Each evening starts from scratch.
            assert reporter._pending_retry_count == 1

    # The last evening failed for the first time, so a retry is still queued.
    assert reporter._pending_retry is not None
    assert reporter._pending_retry_count == 1


def test_report_retry_stops_after_three_failures_in_one_day(monkeypatch) -> None:
    """Three failures on the same day still stop the retries."""

    reporter = _make_reporter()
    monkeypatch.setattr(reporter, "_today", lambda: "2026-10-21", raising=False)

    reporter._schedule_retry("2026-10-21")
    reporter._schedule_retry("2026-10-21")
    reporter._schedule_retry("2026-10-21")
    assert reporter._pending_retry is not None
    assert reporter._pending_retry_count == 3

    reporter._schedule_retry("2026-10-21")
    assert reporter._pending_retry is None
    assert reporter._pending_retry_count == 0


def test_report_retry_state_survives_reload(tmp_path: Path) -> None:
    """Retry plans live in state.json so a plugin reload cannot drop them.

    Saving any config triggers a plugin reload; before this was persisted a
    queued retry disappeared on reload and a failed weekly report silently
    vanished for the whole week.
    """

    from types import SimpleNamespace

    store = ReportStore(tmp_path / "report.db")
    state = StateStore(tmp_path / "state.json")
    config = _FakeConfig({"display": {"timezone": "Asia/Shanghai"}})
    plugin = SimpleNamespace(state=state)
    reporter = DailyReporter(plugin, store, config)
    reporter._pending_retry_day = "2026-10-21"
    reporter._schedule_retry("2026-10-20")

    reloaded = DailyReporter(plugin, store, config)
    assert reloaded._pending_retry is not None
    assert reloaded._pending_retry[0] == "2026-10-20"
    assert reloaded._pending_retry_count == 1


# ----------------------------------------------------------------------
# P1-4: credentials must not survive in a persisted target
# ----------------------------------------------------------------------


def test_redact_url_strips_inline_credentials() -> None:
    assert "secretpass" not in redact_url("https://user:secretpass@host/path?token=ABC")
    assert "ABC" not in redact_url("https://user:secretpass@host/path?token=ABC")
    assert redact_url("https://user:secretpass@host/path?token=ABC") == (
        "https://host/path"
    )


def test_redact_url_keeps_port_and_path() -> None:
    assert redact_url("http://127.0.0.1:3002/status?x=1") == (
        "http://127.0.0.1:3002/status"
    )


def test_redact_url_leaves_plain_text_alone() -> None:
    assert redact_url("Komari 端口 25774") == "Komari 端口 25774"
    assert redact_url("") == ""
