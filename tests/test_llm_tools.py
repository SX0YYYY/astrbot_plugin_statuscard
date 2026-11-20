"""Tests for v2.24.0: the three read-only LLM tools.

Layer 1 guards the plugin-side contract with AST (async, no yield, typed
Args docstrings) -- a docstring missing one parameter type fails the plugin
at LOAD time in real AstrBot, so the docstring_parser check below parses the
REAL library (skipped on CI, which does not install AstrBot dependencies).
Layer 2 drives the production service and the real handler bodies with a
fake self, no AstrBot runtime required.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_statuscard import main as main_module
from astrbot_plugin_statuscard.llm_tools import (
    MAX_OUTPUT_CHARS,
    StatusCardToolService,
)
from astrbot_plugin_statuscard.report_store import ReportStore

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
MAIN_TREE = ast.parse(MAIN_SRC)

TOOL_NAMES = (
    "statuscard_get_server_status",
    "statuscard_query_events",
    "statuscard_get_uptime_summary",
)

# AstrBot source trees the local machine keeps for compatibility checks; CI
# has none of them, so those guards skip instead of failing.
_ASTRBOT_SRC_DIRS = [
    PLUGIN_ROOT.parent / "兼容性验证" / "astrbot-4.26.7" / "astrbot",
    PLUGIN_ROOT.parent / "兼容性验证" / "astrbot-4.27.0" / "astrbot",
]
_ASTRBOT_SRC = next((p for p in _ASTRBOT_SRC_DIRS if p.is_dir()), None)


# ---------------------------------------------------------------------------
# layer 1: the plugin-side tool contract (AST)
# ---------------------------------------------------------------------------


def _tool_functions() -> dict[str, ast.AsyncFunctionDef]:
    found: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(MAIN_TREE):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            call = deco if isinstance(deco, ast.Call) else None
            if call is None:
                continue
            func = call.func
            name_attr = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name_attr != "llm_tool":
                continue
            names = [
                kw.value.value
                for kw in call.keywords
                if kw.arg == "name" and isinstance(kw.value, ast.Constant)
            ]
            if names:
                found[str(names[0])] = node
    return found


def test_three_tools_registered_with_namespaced_names() -> None:
    functions = _tool_functions()
    assert set(functions) == set(TOOL_NAMES), sorted(functions)


def test_tool_handlers_are_async_and_yield_free() -> None:
    """A generator handler breaks on the guarded-tool path (see research)."""

    for name, node in _tool_functions().items():
        assert not any(
            isinstance(child, (ast.Yield, ast.YieldFrom)) for child in ast.walk(node)
        ), f"{name} must not be an async generator"
        returns = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Return) and child.value is not None
        ]
        assert returns, f"{name} must return a string"


def test_tool_docstrings_document_every_parameter() -> None:
    """A missing ``param(type):`` line raises at plugin load in AstrBot."""

    for name, node in _tool_functions().items():
        doc = ast.get_docstring(node) or ""
        assert "Args:" in doc, f"{name} docstring must carry an Args: section"
        args_section = doc.split("Args:", 1)[1]
        params = [a.arg for a in node.args.args if a.arg not in ("self", "event")]
        assert params, f"{name} should declare tool parameters"
        for param in params:
            assert f"{param}(" in args_section, (
                f"{name}: parameter {param} needs a 'param(type): description' line"
            )


def test_tool_names_use_the_statuscard_prefix() -> None:
    for name in _tool_functions():
        assert len(name) <= 64
        assert all(ch.isalnum() or ch in "_-" for ch in name)
        assert name.startswith("statuscard_")


def test_real_docstring_parser_accepts_all_tool_docstrings() -> None:
    """The REAL library AstrBot uses must parse every tool docstring with a
    supported type per parameter (import-time failure otherwise)."""

    docstring_parser = pytest.importorskip("docstring_parser")

    supported = {"string", "number", "boolean", "object", "array"}
    for name, node in _tool_functions().items():
        doc = ast.get_docstring(node) or ""
        parsed = docstring_parser.parse(doc)
        documented = {p.arg_name: p.type_name for p in parsed.params}
        params = [a.arg for a in node.args.args if a.arg not in ("self", "event")]
        for param in params:
            assert param in documented, (name, param)
            assert documented[param] in supported, (
                f"{name}.{param} type {documented[param]!r} is not a supported type"
            )


def test_astrbot_source_still_rejects_untyped_docstrings() -> None:
    """Framework guard: the load-time ValueError must still exist upstream,
    otherwise the docstring contract above guards nothing (skip on CI)."""

    if _ASTRBOT_SRC is None:
        pytest.skip("AstrBot compatibility sources are not present on this runner")
    source = (
        _ASTRBOT_SRC / "core" / "star" / "register" / "star_handler.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    bodies = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "register_llm_tool"
    ]
    assert bodies, "register_llm_tool decorator not found in AstrBot source"
    text = ast.dump(bodies[0])
    assert "docstring_parser" in text
    assert "add_func" in text
    assert text.count("Raise") >= 2, "the two load-time ValueError raises vanished"


def test_astrbot_source_calls_handler_with_event_first() -> None:
    """Guard: the framework passes event as the FIRST handler argument."""

    if _ASTRBOT_SRC is None:
        pytest.skip("AstrBot compatibility sources are not present on this runner")
    func_file = _ASTRBOT_SRC / "core" / "provider" / "func_tool_manager.py"
    if not func_file.is_file():
        pytest.skip("func_tool_manager.py layout changed")
    source = func_file.read_text(encoding="utf-8")
    # 4.26.7/4.27.0: the guarded-tool call passes the event as the FIRST
    # argument (keyword-spread for the rest). 4.29.4's call_local_llm_tool
    # uses handler(event, *args, **kwargs); either shape proves the contract.
    assert (
        "handler(event, **kwargs)" in source
        or "handler(event, *args, **kwargs)" in source
    )


# ---------------------------------------------------------------------------
# layer 2: the service
# ---------------------------------------------------------------------------


def _service(tmp_path: Path, store: ReportStore | None = None, monitor=None):
    return StatusCardToolService(
        {"display": {"timezone": "Asia/Shanghai"}}, None, store, monitor
    )


def test_get_server_status_reports_core_lines(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    text = asyncio.run(svc.get_server_status(include_processes=True, top_n=3))
    assert "CPU:" in text and "内存:" in text
    assert "进程 Top" in text


def test_get_server_status_without_processes(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    text = asyncio.run(svc.get_server_status(include_processes=False))
    assert "进程 Top" not in text


def test_get_server_status_clamps_top_n(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    text = asyncio.run(svc.get_server_status(top_n=0))
    assert "进程 Top 1" in text  # clamped to the low bound


def test_query_events_filters_by_status_and_kind(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = int(time.time())
    day = time.strftime("%Y-%m-%d")
    # The window is [now - days, now]: timestamps in the (near) future would
    # be correctly excluded, so write them in the past.
    store.add_event(
        now - 30, day, "service", "备份脚本", "心跳超时", "alert", "备份脚本"
    )
    store.add_event(
        now - 20, day, "service", "备份脚本", "已恢复", "recovery", "备份脚本"
    )
    store.add_event(now - 10, day, "resource", "CPU", "超过 85%", "alert", "")
    svc = _service(tmp_path, store)

    text = asyncio.run(svc.query_events(days=1, limit=10))
    assert "备份脚本" in text and "CPU" in text

    only_recovery = asyncio.run(svc.query_events(days=1, status="recovery"))
    assert "已恢复" in only_recovery and "心跳超时" not in only_recovery

    only_service = asyncio.run(svc.query_events(days=1, kind="service"))
    assert "备份脚本" in only_service and "超过 85%" not in only_service

    empty = asyncio.run(svc.query_events(days=1, status="recovery", kind="resource"))
    assert "没有符合条件的事件" in empty


def test_query_events_marks_unresolved(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = int(time.time())
    store.add_event(
        now,
        time.strftime("%Y-%m-%d"),
        "service",
        "孤行服务",
        "仍然故障",
        "alert",
        "",
    )
    svc = _service(tmp_path, store)
    text = asyncio.run(svc.query_events(days=1))
    assert "（未恢复）" in text


def test_query_events_requires_store(tmp_path: Path) -> None:
    svc = _service(tmp_path, None)
    text = asyncio.run(svc.query_events())
    assert "日报数据库不可用" in text


def test_query_events_clamps_days_and_limit(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    svc = _service(tmp_path, store)
    text = asyncio.run(svc.query_events(days=9999, limit=-5))
    assert "最近 90 天" in text


def test_uptime_summary_counts_and_ranks(tmp_path: Path) -> None:
    store = ReportStore(tmp_path / "report.db")
    now = int(time.time())
    day = time.strftime("%Y-%m-%d")
    # Two episodes for 抖动服务, one resolved; one still open for 死服务.
    for i in range(2):
        start = now - 3600 * (i + 1)
        store.add_event(
            start, day, "service", "抖动服务", "超时", "alert", "", item_key=f"j{i}"
        )
        store.resolve_event(start + 600, "抖动服务", start + 600, kind="service")
    store.add_event(
        now - 60, day, "service", "死服务", "心跳超时", "alert", "", item_key="dead"
    )
    svc = _service(tmp_path, store)
    text = asyncio.run(svc.get_uptime_summary(days=7))
    assert "故障 3 次" in text
    assert "仍未恢复 1 项" in text
    assert "抖动服务" in text and "死服务" in text
    assert text.index("抖动服务") < text.index("死服务"), (
        "most-failing item ranks first"
    )


def test_uptime_summary_empty_store(tmp_path: Path) -> None:
    svc = _service(tmp_path, ReportStore(tmp_path / "report.db"))
    text = asyncio.run(svc.get_uptime_summary(days=7))
    assert "没有" in text


def test_finish_truncates_long_output(tmp_path: Path) -> None:
    svc = _service(tmp_path)
    long_text = "x" * (MAX_OUTPUT_CHARS + 500)
    out = svc._finish(long_text)
    assert out.endswith("已截断）")
    assert len(out) <= MAX_OUTPUT_CHARS + 40


def test_cache_returns_the_cached_value(tmp_path: Path) -> None:
    svc = _service(tmp_path, ReportStore(tmp_path / "report.db"))
    key = "events:7:50::"
    svc._cache[key] = (time.monotonic(), "CACHED-VALUE")
    assert asyncio.run(svc.query_events()) == "CACHED-VALUE"


# ---------------------------------------------------------------------------
# layer 2b: the real handler bodies (permission branch + delegation)
# ---------------------------------------------------------------------------


class _FakeEvent:
    def is_admin(self) -> bool:
        return False

    def get_platform_name(self) -> str:
        return "aiocqhttp"


def _fake_self(service: StatusCardToolService, operator: bool):
    return SimpleNamespace(_is_operator=lambda _event: operator, _tool_service=service)


def test_handlers_refuse_non_operators(tmp_path: Path) -> None:
    svc = _service(tmp_path, ReportStore(tmp_path / "report.db"))
    fake = _fake_self(svc, operator=False)
    event = _FakeEvent()
    for method, kwargs in (
        (main_module.StatusCardPlugin.llm_tool_get_server_status, {}),
        (main_module.StatusCardPlugin.llm_tool_query_events, {}),
        (main_module.StatusCardPlugin.llm_tool_get_uptime_summary, {}),
    ):
        answer = asyncio.run(method(fake, event, **kwargs))
        assert answer == "权限不足：该工具仅限管理员使用。", answer


def test_handlers_delegate_for_operators(tmp_path: Path) -> None:
    svc = _service(tmp_path, ReportStore(tmp_path / "report.db"))
    fake = _fake_self(svc, operator=True)
    event = _FakeEvent()
    status = asyncio.run(
        main_module.StatusCardPlugin.llm_tool_get_server_status(fake, event)
    )
    assert "CPU:" in status
    events = asyncio.run(
        main_module.StatusCardPlugin.llm_tool_query_events(
            fake, event, days=1, limit=10
        )
    )
    assert "没有符合条件的事件" in events or "事件" in events
    uptime = asyncio.run(
        main_module.StatusCardPlugin.llm_tool_get_uptime_summary(fake, event)
    )
    # An empty store answers with the "nothing recorded" line instead of a
    # summary header.
    assert "可用性汇总" in uptime or "没有" in uptime


def test_handlers_turn_exceptions_into_messages(tmp_path: Path) -> None:
    class _Boom:
        async def get_server_status(self, *a, **k):
            raise RuntimeError("boom")

    fake = _fake_self(_Boom(), operator=True)
    answer = asyncio.run(
        main_module.StatusCardPlugin.llm_tool_get_server_status(fake, _FakeEvent())
    )
    assert answer == "读取服务器状态失败，请稍后再试。", answer


def test_conftest_stubs_keep_main_importable() -> None:
    """0.5.8: the module must stay importable with only the test stubs."""

    assert hasattr(main_module, "StatusCardPlugin")
    assert hasattr(main_module, "StatusCardToolService")
