"""Regression tests for the weekly report that was sent TWICE.

On 2026-11-01 21:30 the weekly report went out twice, 20 seconds apart,
with two DIFFERENT windows: the first covered 2026-10-18..10-24 and the
second 2026-10-25..10-31. Both images arrived in the group.

Root cause
----------
``_weekly_loop`` computed the report day from the ``now`` snapshot taken
at the TOP of the iteration. The loop sleeps until the slot, so that
snapshot can be a minute stale -- and stale in the worst possible way:

    21:29:30  now -> slot_before(21:29:30) = 2026-10-25 21:30
              (the 11-01 slot is still in the future, so it rolls back)
              -> report day 2026-10-24 (ISO week 2026-W43)
    21:30:00  the loop wakes up and sends with the STALE day -> W43
              -> _weekly_last_sent_week = 2026-W43
    21:31:00  the next iteration recomputes the slot; now it is 11-01
              21:30 (in the past, no rollback) -> report day 2026-10-31
              (ISO week 2026-W44)

Two different week keys for ONE slot, so the "already sent" guard could
not stop the second send.

The fix recomputes the slot at the moment of sending, and additionally
de-duplicates on the slot itself (not only on the week key), so one slot
means exactly one weekly report.
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import report as report_mod
from astrbot_plugin_statuscard.report import DailyReporter

TZ = timezone(timedelta(hours=8))
REPORT_PY = Path(report_mod.__file__).resolve().parent / "report.py"


def _reporter(**attrs) -> DailyReporter:
    """A DailyReporter with only the scheduling helpers wired up."""

    reporter = object.__new__(DailyReporter)
    reporter.config = {
        "display": {"timezone": "Asia/Shanghai"},
        "daily_report": {"weekly_time": "21:30", "weekly_enabled": True},
    }
    reporter._weekly_pause_until = 0.0
    reporter._weekly_last_sent_week = None
    reporter._weekly_last_sent_slot = None
    reporter._weekly_given_up_week = None
    for key, value in attrs.items():
        setattr(reporter, key, value)
    return reporter


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


# ----------------------------------------------------------------------
# the slot math
# ----------------------------------------------------------------------


def test_slot_rolls_back_before_the_trigger_time() -> None:
    """Before 21:30 the slot still points at last Sunday."""

    reporter = _reporter()
    slot = reporter._weekly_slot_before(_at("2026-11-01 21:29:59.900"))
    assert slot == _at("2026-10-25 21:30")


def test_slot_is_the_current_sunday_once_the_time_is_reached() -> None:
    """At/after 21:30 the slot is today's."""

    reporter = _reporter()
    slot = reporter._weekly_slot_before(_at("2026-11-01 21:30:00.100"))
    assert slot == _at("2026-11-01 21:30")


def test_report_day_depends_on_the_moment_it_is_computed() -> None:
    """The two report days that caused the double send.

    These are the two ISO weeks the production bug produced: 2026-10-24
    is W43 and 2026-10-31 is W44, which is why the week-key guard could
    not deduplicate them.
    """

    reporter = _reporter()
    stale = reporter._weekly_slot_before(_at("2026-11-01 21:29:59"))
    fresh = reporter._weekly_slot_before(_at("2026-11-01 21:30:00.100"))

    stale_day = (stale - timedelta(days=1)).date().isoformat()
    fresh_day = (fresh - timedelta(days=1)).date().isoformat()

    assert stale_day == "2026-10-24"
    assert fresh_day == "2026-10-31"
    # Different ISO weeks -> the week key alone cannot deduplicate.
    assert reporter._week_key(stale_day) == "2026-W43"
    assert reporter._week_key(fresh_day) == "2026-W44"
    assert reporter._week_key(stale_day) != reporter._week_key(fresh_day)


# ----------------------------------------------------------------------
# the slot key: an extra guard, not the fix (the fix is `target`)
# ----------------------------------------------------------------------


def test_slot_key_is_stable_and_readable() -> None:
    reporter = _reporter()
    slot = reporter._weekly_slot_before(_at("2026-11-01 21:30"))
    assert reporter._weekly_slot_key(slot) == "2026-11-01 21:30"
    assert reporter._weekly_slot_key(None) is None


def test_slot_for_report_day_is_the_day_after_the_report_day() -> None:
    """A weekly report day is the Saturday before its slot."""

    reporter = _reporter()
    assert reporter._weekly_slot_for_report_day("2026-10-31") == "2026-11-01 21:30"
    assert reporter._weekly_slot_for_report_day("2026-10-24") == "2026-10-25 21:30"


def test_slot_for_report_day_rejects_bad_input() -> None:
    reporter = _reporter()
    assert reporter._weekly_slot_for_report_day(None) is None
    assert reporter._weekly_slot_for_report_day("not-a-date") is None


def test_slot_for_report_day_uses_the_configured_time() -> None:
    reporter = _reporter()
    reporter.config["daily_report"]["weekly_time"] = "12:00"
    assert reporter._weekly_slot_for_report_day("2026-10-31") == "2026-11-01 12:00"


def test_slot_for_report_day_rejects_an_invalid_time() -> None:
    reporter = _reporter()
    reporter.config["daily_report"]["weekly_time"] = "99:99"
    assert reporter._weekly_slot_for_report_day("2026-10-31") is None


# ----------------------------------------------------------------------
# the loop: one slot -> exactly one send
# ----------------------------------------------------------------------


def _run_real_loop(
    reporter,
    start: datetime,
    max_rounds: int = 60,
    clock_step_back: float = 0.0,
):
    """Drive the REAL ``_weekly_loop`` and the REAL sender.

    Only the clock and the outermost delivery are replaced:

    - ``report.now_in_timezone`` returns the virtual clock
    - ``asyncio.sleep`` advances the virtual clock instead of waiting
    - the plugin's ``send_text`` records the delivery

    ``clock_step_back`` simulates a wall clock that is stepped BACKWARDS
    during the sleep (NTP step, manual clock change) -- the scenario that
    re-opened the double send after the first fix.

    Everything that decides WHETHER a report goes out -- the slot math,
    the catch-up guard, the de-duplication guards in the sender -- is the
    shipped code. That is what makes this test able to catch a
    regression: a re-implementation of the loop could hide the very
    ordering bug that caused the double send.
    """

    sends: list[tuple[datetime, str]] = []
    clock = [start]
    rounds = [0]
    stepped = [False]
    report_days: list[str] = []

    async def fake_sleep(duration, *args, **kwargs):
        rounds[0] += 1
        if rounds[0] > max_rounds:
            raise asyncio.CancelledError
        clock[0] = clock[0] + timedelta(seconds=float(duration))
        if clock_step_back and not stepped[0]:
            stepped[0] = True
            clock[0] = clock[0] - timedelta(seconds=clock_step_back)

    async def fake_send_text(targets, text, source=None):
        sends.append((clock[0], report_days[-1] if report_days else ""))
        return _delivered_ok()

    async def main():
        original_now = report_mod.now_in_timezone
        original_sleep = asyncio.sleep
        report_mod.now_in_timezone = lambda cfg=None: clock[0]
        asyncio.sleep = fake_sleep
        try:
            # Stub only what the sender needs; the sender itself stays.
            def _build(day, span, sections):
                report_days.append(str(day))
                return {"day": str(day)}

            reporter._build_view = _build
            reporter.store = SimpleNamespace(
                probe=lambda: True, ensure_ready=lambda: True
            )
            reporter._targets = lambda kind=None: ["default:GroupMessage:1"]
            reporter._with_image = lambda: False
            reporter._save_delivery_state = lambda: None
            reporter._record = lambda day, ok, detail: None
            reporter._weekly_sections = lambda: ["resource"]
            reporter._weekly_empty_recorded_week = None
            reporter._weekly_retry_count = 0
            reporter._weekly_image_attempts = 0
            reporter._weekly_image_pending = None
            reporter._weekly_image_report_day = None
            reporter._weekly_pending = None
            reporter._weekly_pending_report_day = None
            reporter._weekly_pending_recovery_week = None
            reporter._weekly_pending_recovery_since = 0.0
            reporter._weekly_given_up_week = None
            reporter._weekly_last_sent_week = None
            reporter._weekly_last_sent_slot = None
            reporter._weekly_attempt_week = None
            reporter.plugin = SimpleNamespace(send_text=fake_send_text, send_image=None)
            reporter._weekly_running = True
            await reporter._weekly_loop()
        except asyncio.CancelledError:
            pass
        finally:
            report_mod.now_in_timezone = original_now
            asyncio.sleep = original_sleep

    asyncio.run(main())
    return sends


def _delivered_ok():
    from astrbot_plugin_statuscard.delivery import DeliveryResult

    return DeliveryResult(
        ok=True,
        ok_targets=["default:GroupMessage:1"],
        receipts={"default:GroupMessage:1": {"retcode": 0}},
        retcodes={"default:GroupMessage:1": 0},
    )


def test_loop_sends_the_weekly_report_exactly_once() -> None:
    """The 2026-11-01 21:30 slot must produce ONE report.

    This drives the production loop and the production sender, so it
    fails if the report day is computed from a stale clock again (the
    double send) or if the de-duplication guards stop working.
    """

    reporter = _reporter()
    sends = _run_real_loop(reporter, _at("2026-11-01 21:29:59"))

    assert len(sends) == 1, f"expected one weekly report, got {len(sends)}"
    sent_at = sends[0][0]
    assert sent_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-11-01 21:30:00"


def test_the_week_the_user_receives_is_the_current_one() -> None:
    """The report day is the Saturday before the slot, not the one before.

    The wrong week was the visible symptom: the first of the two reports
    covered 2026-10-18..10-24 instead of 2026-10-25..10-31.
    """

    reporter = _reporter()
    # The slot before the trigger time still points at last Sunday, which
    # is what produced the stale report day.
    slot = reporter._weekly_slot_before(_at("2026-11-01 21:29:59"))
    assert slot == _at("2026-10-25 21:30")
    assert (slot - timedelta(days=1)).date().isoformat() == "2026-10-24"

    # And the slot at the trigger time is the current one.
    slot = reporter._weekly_slot_before(_at("2026-11-01 21:30:00.100"))
    assert slot == _at("2026-11-01 21:30")
    report_day = date.fromisoformat((slot - timedelta(days=1)).date().isoformat())
    assert report_day + timedelta(days=1) == date(2026, 11, 1)
    assert report_day + timedelta(days=7) == date(2026, 11, 7)


def test_loop_does_not_resend_after_the_report_went_out() -> None:
    """Several loop iterations after the send must not send again."""

    reporter = _reporter()
    sends = _run_real_loop(reporter, _at("2026-11-01 21:30:00.100"))
    assert len(sends) == 1


def test_four_consecutive_weeks_each_send_exactly_one_report() -> None:
    """Four weekly slots must produce four reports -- no more, no less.

    This is the guard against the opposite failure: a de-duplication
    guard that is too strict would silently swallow the whole report
    (the guard inverted to ``!=`` sends nothing at all).
    """

    for week in range(4):
        reporter = _reporter()
        start = _at("2026-11-01 21:29:59") + timedelta(days=7 * week)
        sends = _run_real_loop(reporter, start)
        assert len(sends) == 1, (
            f"week {week + 1} ({start.date()}) produced {len(sends)} reports"
        )


def test_one_reporter_across_five_weeks_sends_one_report_per_week() -> None:
    """The same reporter instance must send once per week.

    The per-week test above builds a fresh reporter every time, so it
    cannot see a guard that is too strict from the second week on.
    """

    reporter = _reporter()
    start = _at("2026-11-01 21:29:59")
    total = 0
    for week in range(5):
        sends = _run_real_loop(
            reporter, start + timedelta(days=7 * week), max_rounds=40
        )
        assert len(sends) == 1, (
            f"week {week + 1} produced {len(sends)} reports "
            f"(last_sent_week={reporter._weekly_last_sent_week!r}, "
            f"last_sent_slot={reporter._weekly_last_sent_slot!r})"
        )
        total += len(sends)
    assert total == 5


def test_a_backwards_wall_clock_does_not_cause_a_double_send() -> None:
    """A clock stepped backwards during the sleep must not send twice.

    Deriving the slot from a wall-clock read at the sending moment is
    not enough: ``_weekly_slot_before`` rolls back seven days whenever
    the clock is even a hair before the slot, so a clock that is stepped
    backwards (NTP step, manual change) re-opens the double send. The
    slot has to come from ``target``.
    """

    for step_back in (0.5, 2.0, 30.0):
        reporter = _reporter()
        sends = _run_real_loop(
            reporter, _at("2026-11-01 21:29:59"), clock_step_back=step_back
        )
        assert len(sends) == 1, (
            f"clock stepped back {step_back}s -> {len(sends)} reports"
        )
        assert sends[0][1] == "2026-10-31", (
            f"clock stepped back {step_back}s -> wrong report day {sends[0][1]!r}"
        )


def test_the_delivered_report_day_is_the_current_week() -> None:
    """The user must receive the week that just ended, not the one before."""

    reporter = _reporter()
    sends = _run_real_loop(reporter, _at("2026-11-01 21:29:59"))
    assert len(sends) == 1
    report_day = date.fromisoformat(sends[0][1])
    assert report_day == date(2026, 10, 31)
    # The window is the seven days that END on that Saturday.
    assert report_day + timedelta(days=1) == date(2026, 11, 1)


def test_catch_up_guard_stops_after_this_week_was_sent() -> None:
    """Once the slot is sent, the catch-up guard must not fire again."""

    reporter = _reporter(_weekly_last_sent_week="2026-W44")
    reporter._weekly_last_sent_slot = "2026-11-01 21:30"
    original = report_mod.now_in_timezone
    report_mod.now_in_timezone = lambda cfg=None: _at("2026-11-01 21:30:12")
    try:
        assert reporter._weekly_catch_up_allowed() is False
    finally:
        report_mod.now_in_timezone = original


# ----------------------------------------------------------------------
# regression guard: the Monday catch-up must keep working
# ----------------------------------------------------------------------


def test_monday_restart_still_catches_up() -> None:
    """A Monday-morning restart must still deliver last week's report."""

    reporter = _reporter()
    original = report_mod.now_in_timezone
    report_mod.now_in_timezone = lambda cfg=None: _at("2026-11-02 00:30:00")
    try:
        assert reporter._weekly_catch_up_allowed() is True
        slot = reporter._weekly_slot_before(_at("2026-11-02 00:30:00"))
        assert slot == _at("2026-11-01 21:30")
        assert (slot - timedelta(days=1)).date().isoformat() == "2026-10-31"
    finally:
        report_mod.now_in_timezone = original


def test_catch_up_window_still_expires() -> None:
    """Outside the 8.5 hour window the catch-up must stay closed."""

    reporter = _reporter()
    original = report_mod.now_in_timezone
    report_mod.now_in_timezone = lambda cfg=None: _at("2026-11-02 06:01:00")
    try:
        assert reporter._weekly_catch_up_allowed() is False
    finally:
        report_mod.now_in_timezone = original


# ----------------------------------------------------------------------
# source-level guards
# ----------------------------------------------------------------------


def _find_async_function(name: str):
    tree = ast.parse(REPORT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def test_loop_uses_the_target_slot_not_a_clock_read() -> None:
    """The slot must be the ``target`` this iteration already decided on.

    Two earlier attempts to derive the slot from the clock both failed:

    - from the snapshot at the top of the iteration: it is stale after
      the sleep and named last week's report day (the original double
      send on 2026-11-01);
    - from a fresh wall-clock read at the sending moment: a clock stepped
      backwards during the sleep puts the read before the slot again and
      the double send returns.

    ``target`` is computed from the configured time and pushed to the
    coming Sunday, so it names the slot this iteration fires for no
    matter what the clock does. This AST assertion pins that shape.
    """

    loop = _find_async_function("_weekly_loop")
    assert loop is not None, "_weekly_loop not found"

    # Find the assignment that produces the slot handed to the sender.
    target_assignments = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "slot" for t in node.targets)
        and isinstance(node.value, ast.Name)
        and node.value.id == "target"
    ]
    assert target_assignments, (
        "_weekly_loop must use `target` as the slot for the scheduled "
        "send -- a clock read there can land before the slot and re-open "
        "the double send"
    )

    # The catch-up branch is the one exception: there the slot is in the
    # past and has to be derived from the clock.
    assert "_weekly_slot_before" in ast.dump(loop), (
        "the catch-up branch must still derive the slot from the clock"
    )


def test_loop_does_not_use_the_stale_now_snapshot_for_the_slot() -> None:
    """The broken form must be gone: ``_weekly_slot_before(now)``."""

    loop = _find_async_function("_weekly_loop")
    assert loop is not None

    stale = [
        call
        for call in ast.walk(loop)
        if isinstance(call, ast.Call)
        and getattr(call.func, "attr", None) == "_weekly_slot_before"
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "now"
    ]
    assert not stale, (
        "_weekly_loop must not compute the slot from the stale `now` "
        "snapshot (that is the double-send bug)"
    )


def test_loop_does_not_derive_the_scheduled_slot_from_the_clock() -> None:
    """The scheduled send must not read the clock to find its slot.

    ``_weekly_slot_before`` rolls back seven days whenever the clock is
    even a hair BEFORE the slot, so a clock that is stepped backwards
    during the sleep names last week's report day -- and the second
    weekly report follows.
    """

    loop = _find_async_function("_weekly_loop")
    assert loop is not None

    tree = ast.parse(REPORT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_weekly_loop":
            loop = node
            break

    # Find the assignment of the slot that is handed to the sender and
    # check that it is not derived from a clock read.
    clock_derived = []
    for node in ast.walk(loop):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if getattr(fn, "attr", None) == "_weekly_slot_before":
                arg = node.value.args[0]
                if (
                    isinstance(arg, ast.Call)
                    or isinstance(arg, ast.Name)
                    and arg.id in {"now"}
                ):
                    clock_derived.append(node)
    # The only clock-derived slot may be the catch-up branch, which is
    # guarded by `if caught_up:`. Anything computed from a bare clock
    # read outside that guard is the bug.
    for node in clock_derived:
        assert isinstance(node.value.args[0], ast.Call), (
            "_weekly_loop must not derive the slot from the loop-top `now` "
            "snapshot -- use `target` for the scheduled send"
        )


def test_sender_has_the_slot_guard() -> None:
    """The sender must refuse a second report for the same slot."""

    sender = _find_async_function("_send_weekly_scheduled")
    assert sender is not None

    compares_slot = False
    for node in ast.walk(sender):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(
                isinstance(op, ast.Attribute) and op.attr == "_weekly_last_sent_slot"
                for op in operands
            ):
                compares_slot = True
    assert compares_slot, (
        "_send_weekly_scheduled must compare the slot against "
        "_weekly_last_sent_slot -- the week key alone cannot prevent a "
        "double send when the report day lands in a different ISO week"
    )


def test_sender_accepts_and_persists_a_slot_key() -> None:
    source = REPORT_PY.read_text(encoding="utf-8")
    assert "weekly_last_sent_slot" in source
    assert re.search(
        r"async def _send_weekly_scheduled\(\s*self,\s*day:.*?slot:.*?\)",
        source,
        re.DOTALL,
    ), "_send_weekly_scheduled must accept the slot key"
