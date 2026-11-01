"""Regression tests for issue #1: the status card was never sent.

Since v2.20.0 every command handler called ``event.stop_event()``. In
AstrBot that sets ``_force_stopped`` on the event, so ``is_stopped()``
returns True for the rest of the pipeline -- and the result decoration
stage checks that flag and RETURNS. The command's own results (the
placeholder text and the rendered image) were therefore never sent.

The user saw "状态卡正在生成" and then nothing, with the framework
logging "after_message_sent stopped event propagation".

``should_call_llm(False)`` is enough on its own: the framework only
calls the LLM when ``event.call_llm`` is still True.

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
# 1. The source-level guard: no command handler may stop the event.
# --------------------------------------------------------------------------


def test_no_command_handler_stops_the_event() -> None:
    """No command handler may call ``event.stop_event()``.

    ``stop_event()`` makes the framework skip the result chain, so the
    command's reply never leaves the plugin.
    """

    source = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "stop_event":
                continue
            # ``event.stop_event()`` -- the receiver must be named "event".
            if isinstance(func.value, ast.Name) and func.value.id == "event":
                offenders.append(f"{node.name} (line {call.lineno})")
    assert not offenders, (
        "these handlers call event.stop_event(), which stops the framework "
        f"from sending their result: {offenders}"
    )


def test_the_commands_still_block_the_llm() -> None:
    """``should_call_llm(False)`` must stay -- it is what keeps the LLM out."""

    source = MAIN_PY.read_text(encoding="utf-8")
    # One per command handler (6 commands).
    assert source.count("event.should_call_llm(False)") >= 6


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
        self.call_llm = True
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
def test_command_handlers_do_not_stop_the_event(command: str) -> None:
    """The handler must not stop the event, and must keep the LLM out."""

    handler = getattr(main_mod.StatusCardPlugin, command)
    source = Path(inspect.getsourcefile(handler) or "").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == command:
            target = node
            break
    assert target is not None, f"{command} not found"

    stopped = False
    llm_blocked = False
    for call in ast.walk(target):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if (
            call.func.attr == "stop_event"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "event"
        ):
            stopped = True
        if (
            call.func.attr == "should_call_llm"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "event"
        ):
            llm_blocked = True
    assert not stopped, f"{command} stops the event -> its reply is never sent"
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
            self.call_llm = True

        def should_call_llm(self, call_llm):
            self.call_llm = call_llm

    event = RealEvent()
    event.should_call_llm(False)
    assert event.call_llm is False, "the LLM must stay out"
    assert _framework_sends(event) is True, "the result must be sent"

    # And the forbidden call would break it.
    event2 = RealEvent()
    event2.should_call_llm(False)
    event2.stop_event()
    assert _framework_sends(event2) is False, (
        "stop_event() must prevent the send -- that is the bug"
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
    plugin._privacy_notified = set()
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
    assert event.call_llm is False, "cmd_status must keep the LLM out"
    assert results, "cmd_status produced no result at all"
    assert any("status.jpg" in chain[0] for chain in results if chain), results
    assert _framework_sends(event) is True, "the framework would skip the result chain"
