"""Regression tests for issue #1: the status card was never sent.

Since v2.20.0 every command handler called ``event.stop_event()``. In
AstrBot that sets ``_force_stopped`` on the event, so ``is_stopped()``
returns True for the rest of the pipeline -- and the result decoration
stage checks that flag and RETURNS. The command's own results (the
placeholder text and the rendered image) were therefore never sent.

The user saw "状态卡正在生成" and then nothing, with the framework
logging "after_message_sent stopped event propagation".

``should_call_llm(True)`` is the framework's "do not call the default LLM"
flag. ``False`` is the default value and does not block anything: the
command sends the result before the LLM branch, and ``_has_send_oper`` is
what normally skips it. The explicit True is what keeps a failed send or a
result-clearing hook from handing the command text to the model (v2.20.22).

These tests read the REAL AstrBot source (when it is available in the
workspace) and otherwise fall back to the documented semantics, so they
cannot drift from the framework.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import main as main_mod

PLUGIN_ROOT = Path(main_mod.__file__).resolve().parent
MAIN_PY = PLUGIN_ROOT / "main.py"

# The workspace keeps the AstrBot sources for the compatibility checks.
WORKSPACE_ROOT = PLUGIN_ROOT.parent
ASTRBOT_ROOTS = [
    WORKSPACE_ROOT / "兼容性验证" / "astrbot-4.27.0",
    WORKSPACE_ROOT / "兼容性验证" / "astrbot-4.26.7",
]


def _framework_root() -> Path | None:
    for root in ASTRBOT_ROOTS:
        if (root / "astrbot" / "core" / "platform").is_dir():
            return root
    return None


# --------------------------------------------------------------------------
# 1. The source-level guard: no command handler may stop the event BEFORE
#    its first yield.
# --------------------------------------------------------------------------


def _stop_event_calls(node: ast.AsyncFunctionDef) -> list[ast.Call]:
    """Every ``event.stop_event()`` call inside a command handler."""

    out: list[ast.Call] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr != "stop_event":
            continue
        if isinstance(func.value, ast.Name) and func.value.id == "event":
            out.append(call)
    return out


def _first_yield_line(node: ast.AsyncFunctionDef) -> int | None:
    """The line of the handler's first yield, if any."""

    lines = [
        n.lineno for n in ast.walk(node) if isinstance(n, (ast.Yield, ast.YieldFrom))
    ]
    return min(lines) if lines else None


def test_no_command_handler_stops_the_event_before_its_first_yield() -> None:
    """No command handler may stop the event before producing a result.

    ``stop_event()`` makes the framework skip the result chain, so a result
    produced after it never leaves the plugin (issue #1).

    A call AFTER every yield is a different thing and is allowed: the
    results are already collected there, and stopping the event at that
    point is what keeps the reply out of the LLM branch (v2.5.1).
    """

    source = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("cmd_"):
            continue
        first_yield = _first_yield_line(node)
        for call in _stop_event_calls(node):
            if first_yield is not None and call.lineno > first_yield:
                continue  # after the results: allowed (the LLM guard)
            offenders.append(f"{node.name} (line {call.lineno})")
    assert not offenders, (
        "these handlers call event.stop_event() before their first yield, "
        "which stops the framework from sending their result: "
        f"{offenders}"
    )


def test_the_trailing_stop_event_llm_guard_is_kept() -> None:
    """The preview commands keep their trailing ``stop_event()``.

    v2.5.1 ("修复预览日报进入 LLM") added it deliberately. The explicit
    ``should_call_llm(True)`` is now the primary guard; this trailing call is
    kept as the older second line of defence. Dropping it would remove that
    defence, so the test keeps it locked in place.
    """

    source = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    guards: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name not in ("cmd_preview_report", "cmd_preview_weekly"):
            continue
        first_yield = _first_yield_line(node)
        calls = _stop_event_calls(node)
        guards[node.name] = bool(
            calls
            and first_yield is not None
            and all(call.lineno > first_yield for call in calls)
        )
    assert guards.get("cmd_preview_report") is True, guards
    assert guards.get("cmd_preview_weekly") is True, guards


def test_the_commands_still_block_the_llm() -> None:
    """``should_call_llm(True)`` must stay -- it is what keeps the LLM out.

    ``False`` is the framework default and is a no-op: ProcessStage enters
    the LLM branch when ``not event.call_llm``. The old code passed False, so
    the comment promised a guard that never existed (v2.20.22).
    """

    source = MAIN_PY.read_text(encoding="utf-8")
    # One per command handler (6 commands).
    assert source.count("event.should_call_llm(True)") >= 6
    assert "event.should_call_llm(False)" not in source


# --------------------------------------------------------------------------
# 2. The behavioural guard: the result must reach the framework.
# --------------------------------------------------------------------------

_EVENT_METHODS = {"stop_event", "is_stopped", "set_result", "get_result"}


def _extract_framework_methods() -> str | None:
    """Read the real ``stop_event`` / ``is_stopped`` from the AstrBot source."""

    root = _framework_root()
    if root is None:
        return None
    path = root / "astrbot" / "core" / "platform" / "astr_message_event.py"
    if not path.is_file():
        return None
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    chunks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AstrMessageEvent":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in _EVENT_METHODS:
                    segment = ast.get_source_segment(source, item)
                    lines = segment.splitlines()
                    head = lines[0].strip()
                    body = textwrap.indent(
                        textwrap.dedent("\n".join(lines[1:])).strip("\n"), "    "
                    )
                    chunks.append(head + "\n" + body)
    if not chunks:
        return None
    return "\n\n".join(chunks)


class _Result:
    """The framework's result object (real source when available)."""

    def __init__(self, chain=None) -> None:
        self.chain = list(chain or [])
        self.result_type = "continue"

    def stop_event(self):
        self.result_type = "stop"
        return self

    def continue_event(self):
        self.result_type = "continue"
        return self

    def is_stopped(self):
        return self.result_type == "stop"


class _Event:
    """The framework's event semantics (the real source when available)."""

    def __init__(self) -> None:
        self._result = None
        self._force_stopped = False
        # The real AstrMessageEvent initialises call_llm to False; True is
        # the "forbid the default LLM" flag passed by the commands.
        self.call_llm = False
        self.stopped_calls = 0

    def set_result(self, result):
        self._result = result

    def get_result(self):
        return self._result

    def clear_result(self):
        self._result = None

    def stop_event(self):
        self.stopped_calls += 1
        self._force_stopped = True

    def is_stopped(self):
        if self._force_stopped:
            return True
        if self._result is None:
            return False
        return False

    def should_call_llm(self, call_llm):
        self.call_llm = call_llm

    # --- the command handlers use these ------------------------------
    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return SimpleNamespace(chain=[text])

    def image_result(self, url):
        return SimpleNamespace(chain=[url])

    @property
    def unified_msg_origin(self):
        return "default:GroupMessage:1"

    message_str = "查看状态"


def _framework_sends(event) -> bool:
    """The framework's result decoration stage.

    astrbot/core/pipeline/result_decorate/stage.py:

        if event.is_stopped():
            logger.info("... stopped event propagation.")
            return
    """

    return not event.is_stopped()


def _llm_branch_entered(event) -> bool:
    """The framework's LLM condition.

    astrbot/core/pipeline/process_stage/stage.py:

        if (
            not event._has_send_oper
            and event.is_at_or_wake_command
            and not event.call_llm
        ):
            if (
                event.get_result() and not event.is_stopped()
            ) or not event.get_result():
                async for _ in self.agent_sub_stage.process(event):

    The branch is entered only when ``event.call_llm`` is False (the
    framework default). ``should_call_llm(True)`` therefore keeps the reply
    out before the nested result check runs; a trailing ``stop_event()`` is
    the older second line of defence.
    """

    # The nested form mirrors the framework's own code; merging it into one
    # condition would hide which check does the work.
    if (  # noqa: SIM102
        not getattr(event, "_has_send_oper", False)
        and getattr(event, "is_at_or_wake_command", True)
        and not event.call_llm
    ):
        if (event.get_result() and not event.is_stopped()) or not event.get_result():
            return True
    return False


def test_the_framework_source_stops_sending_when_the_event_is_stopped() -> None:
    """Guard the assumption this test rests on.

    If the AstrBot source is available, assert that the result decoration
    stage really does return early on a stopped event -- otherwise the
    behavioural test below would be testing a stale assumption.
    """

    root = _framework_root()
    if root is None:
        pytest.skip("the AstrBot sources are not in the workspace")
    stage = root / "astrbot" / "core" / "pipeline" / "result_decorate" / "stage.py"
    if not stage.is_file():
        pytest.skip("the result decoration stage is not in the workspace")
    text = stage.read_text(encoding="utf-8")
    assert re.search(r"if event\.is_stopped\(\):", text), (
        "the framework no longer skips the result chain on a stopped event"
    )
    assert "stopped event propagation" in text


@pytest.mark.parametrize(
    "command",
    [
        "cmd_status",
        "cmd_alert_test",
        "cmd_monitor_test",
        "cmd_diag",
        "cmd_preview_report",
        "cmd_preview_weekly",
    ],
)
def test_command_handlers_do_not_stop_the_event_before_the_first_yield(
    command: str,
) -> None:
    """No handler may stop the event before it produced its result.

    A stop AFTER every yield is allowed (and required for the two preview
    commands): by then the results are collected, and it is what keeps the
    reply out of the LLM branch.
    """

    handler = getattr(main_mod.StatusCardPlugin, command)
    source = Path(inspect.getsourcefile(handler) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == command:
            target = node
            break
    assert target is not None, f"{command} not found"

    first_yield = _first_yield_line(target)
    early_stop = False
    llm_blocked = False
    for call in ast.walk(target):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if (  # noqa: SIM102
            call.func.attr == "stop_event"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "event"
        ):
            if first_yield is None or call.lineno <= first_yield:
                early_stop = True
        if (
            call.func.attr == "should_call_llm"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "event"
            # Only the literal True forbids the default LLM; False is the
            # framework default and a no-op (v2.20.22).
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value is True
        ):
            llm_blocked = True
    assert not early_stop, (
        f"{command} stops the event before its first yield -> its reply is never sent"
    )
    assert llm_blocked, f"{command} no longer blocks the default LLM"


def test_the_real_framework_semantics_send_the_result() -> None:
    """Drive the real framework methods when the sources are available."""

    extracted = _extract_framework_methods()
    if extracted is None:
        pytest.skip("the AstrBot sources are not in the workspace")

    namespace: dict[str, object] = {"MessageEventResult": _Result}
    exec(  # noqa: S102 - running the framework's own source, by design
        "class _Event:\n" + textwrap.indent(extracted, "    "),
        namespace,
    )
    real_event = namespace["_Event"]

    class RealEvent(real_event):  # type: ignore[misc,valid-type]
        def __init__(self):
            self._result = None
            self._force_stopped = False
            # Real framework default.
            self.call_llm = False

        def should_call_llm(self, call_llm):
            self.call_llm = call_llm

    event = RealEvent()
    event.should_call_llm(True)
    assert event.call_llm is True, "the LLM must stay out"
    assert _framework_sends(event) is True, "the result must be sent"

    # And the forbidden call would break it.
    event2 = RealEvent()
    event2.should_call_llm(True)
    event2.stop_event()
    assert _framework_sends(event2) is False, (
        "stop_event() must prevent the send -- that is the bug"
    )


def test_the_trailing_guard_keeps_the_reply_out_of_the_llm_branch() -> None:
    """Both the explicit flag and the trailing guard keep the LLM out.

    ``should_call_llm(True)`` blocks the branch before the nested result
    check; the trailing ``stop_event()`` is the v2.5.1 second line of
    defence. A bare default event (``call_llm=False``) does enter the branch
    when no reply was sent -- that is why neither guard may be removed.
    """

    extracted = _extract_framework_methods()
    if extracted is None:
        pytest.skip("the AstrBot sources are not in the workspace")

    namespace: dict[str, object] = {"MessageEventResult": _Result}
    exec(  # noqa: S102
        "class _Event:\n" + textwrap.indent(extracted, "    "),
        namespace,
    )
    real_event = namespace["_Event"]

    class RealEvent(real_event):  # type: ignore[misc,valid-type]
        def __init__(self):
            self._result = None
            self._force_stopped = False
            self.call_llm = False
            self._has_send_oper = False
            self.is_at_or_wake_command = True

        def should_call_llm(self, call_llm):
            self.call_llm = call_llm

    # Without either guard the reply would reach the LLM.
    without = RealEvent()
    without.set_result(_Result())
    assert _llm_branch_entered(without) is True, (
        "without a guard the reply reaches the LLM branch"
    )

    # The explicit flag alone already keeps it out.
    flagged = RealEvent()
    flagged.should_call_llm(True)
    flagged.set_result(_Result())
    assert _llm_branch_entered(flagged) is False, (
        "should_call_llm(True) must keep the reply out of the LLM branch"
    )

    # The trailing stop_event() remains the second line of defence.
    with_guard = RealEvent()
    with_guard.set_result(_Result())
    with_guard.stop_event()
    assert _llm_branch_entered(with_guard) is False, (
        "the trailing stop_event() must keep the reply out of the LLM branch"
    )


def test_status_card_result_reaches_the_framework() -> None:
    """The placeholder and the image must both be produced and sendable."""

    plugin = object.__new__(main_mod.StatusCardPlugin)
    plugin.config = {
        "display": {
            "cooldown_seconds": 0,
            "privacy_notice": False,
            "progress_notice": True,
        },
        "alerts": {"targets": ["1"]},
    }
    plugin._status_cooldown = {}
    plugin._status_inflight = set()
    plugin._privacy_notified = {}  # insertion-ordered dict since v2.20.32
    plugin._diag = {}
    plugin.state = None
    plugin.context = None

    async def fake_render(_event):
        return "https://example.invalid/status.jpg"

    plugin._render_status_image = fake_render

    event = _Event()
    results = []

    async def drive():
        agen = main_mod.StatusCardPlugin.cmd_status(plugin, event)
        async for ret in agen:
            if ret is not None:
                event.set_result(ret)
                results.append(list(ret.chain))

    asyncio.run(drive())

    assert event.stopped_calls == 0, "cmd_status stopped the event"
    assert event.call_llm is True, "cmd_status must keep the LLM out"
    assert results, "cmd_status produced no result at all"
    assert any("status.jpg" in chain[0] for chain in results if chain), results
    assert _framework_sends(event) is True, "the framework would skip the result chain"
