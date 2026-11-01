from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

# Tests import the plugin as the package ``astrbot_plugin_statuscard`` so the
# relative imports inside the plugin modules resolve. The repository root is
# the plugin directory itself, so its parent has to be on sys.path.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))

# AstrBot is not installed in the test environment. The plugin follows the
# official rule of importing ``astrbot.api.logger``, so provide a minimal stub
# before any plugin module is imported.
try:
    from astrbot.api import logger  # noqa: F401
except ModuleNotFoundError:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logging.getLogger("astrbot")  # type: ignore[attr-defined]
    astrbot_module.api = api_module  # type: ignore[attr-defined]
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module

# page_api uses the official astrbot.api.web helpers; stub them when the
# astrbot package in this environment does not provide the submodule.
try:
    import astrbot.api.web  # noqa: F401
except Exception:
    web_module = types.ModuleType("astrbot.api.web")
    web_module.json_response = lambda payload: payload  # type: ignore[attr-defined]
    web_module.request = types.SimpleNamespace(query={})  # type: ignore[attr-defined]
    sys.modules["astrbot.api.web"] = web_module


# main.py (the command handlers) needs a few more AstrBot modules. Without
# them the module could not be imported at all, so no test could cover the
# command layer -- which is how issue #1 (the status card was never sent)
# slipped through: the handlers were never exercised.
def _install_command_layer_stubs() -> None:
    import types as _types

    astrbot = sys.modules["astrbot"]
    api = sys.modules["astrbot.api"]

    if not hasattr(api, "__path__"):
        api.__path__ = []  # type: ignore[attr-defined]

    # astrbot.api.message_components
    if "astrbot.api.message_components" not in sys.modules:
        comp = _types.ModuleType("astrbot.api.message_components")

        class Image:  # minimal stand-in
            def __init__(self, url: str = "", **kwargs: object) -> None:
                self.url = url

        class Plain:  # minimal stand-in
            def __init__(self, text: str = "", **kwargs: object) -> None:
                self.text = text

        comp.Image = Image  # type: ignore[attr-defined]
        comp.Plain = Plain  # type: ignore[attr-defined]
        api.message_components = comp  # type: ignore[attr-defined]
        sys.modules["astrbot.api.message_components"] = comp

    # astrbot.api.event
    if "astrbot.api.event" not in sys.modules:
        event_module = _types.ModuleType("astrbot.api.event")

        class AstrMessageEvent:  # minimal stand-in
            def __init__(self) -> None:
                self.call_llm = True

            def should_call_llm(self, call_llm: bool) -> None:
                self.call_llm = call_llm

            def stop_event(self) -> None:
                self.stopped = True

            def get_platform_name(self) -> str:
                return "aiocqhttp"

        class _PermissionType:
            ADMIN = "ADMIN"

        _perm_type = _PermissionType

        class _Filter:
            PermissionType = _perm_type

            @staticmethod
            def permission_type(_kind):
                return lambda fn: fn

            @staticmethod
            def command(_name):
                return lambda fn: fn

            @staticmethod
            def event_message_type(_kind):
                return lambda fn: fn

            @staticmethod
            def platform_logger_type(_kind):
                return lambda fn: fn

            @staticmethod
            def regex(_pattern):
                return lambda fn: fn

        event_module.AstrMessageEvent = AstrMessageEvent  # type: ignore[attr-defined]
        event_module.filter = _Filter()  # type: ignore[attr-defined]
        api.event = event_module  # type: ignore[attr-defined]
        sys.modules["astrbot.api.event"] = event_module

    # astrbot.api.star
    if "astrbot.api.star" not in sys.modules:
        star_module = _types.ModuleType("astrbot.api.star")

        class Context:  # minimal stand-in
            pass

        class Star:  # minimal stand-in
            pass

        class StarTools:  # minimal stand-in
            @staticmethod
            def get_data_dir(*_args, **_kwargs):
                return PLUGIN_ROOT

        def register(_name, *_args, **_kwargs):
            return lambda cls: cls

        star_module.Context = Context  # type: ignore[attr-defined]
        star_module.Star = Star  # type: ignore[attr-defined]
        star_module.StarTools = StarTools  # type: ignore[attr-defined]
        star_module.register = register  # type: ignore[attr-defined]
        api.star = star_module  # type: ignore[attr-defined]
        sys.modules["astrbot.api.star"] = star_module

    # astrbot.core.message.message_event_result
    if "astrbot.core.message.message_event_result" not in sys.modules:
        core = getattr(astrbot, "core", None)
        if core is None:
            core = _types.ModuleType("astrbot.core")
            core.__path__ = []  # type: ignore[attr-defined]
            astrbot.core = core  # type: ignore[attr-defined]
            sys.modules["astrbot.core"] = core
        core_msg = _types.ModuleType("astrbot.core.message")
        core_msg.__path__ = []  # type: ignore[attr-defined]
        result_module = _types.ModuleType("astrbot.core.message.message_event_result")

        class MessageChain:  # minimal stand-in
            def __init__(self, chain=None):
                self.chain = list(chain or [])

        class MessageEventResult(MessageChain):  # minimal stand-in
            def __init__(self, chain=None):
                super().__init__(chain)
                self.result_type = "continue"

        result_module.MessageChain = MessageChain  # type: ignore[attr-defined]
        result_module.MessageEventResult = MessageEventResult  # type: ignore[attr-defined]
        core_msg.message_event_result = result_module  # type: ignore[attr-defined]
        core.message = core_msg  # type: ignore[attr-defined]
        sys.modules["astrbot.core.message"] = core_msg
        sys.modules["astrbot.core.message.message_event_result"] = result_module


_install_command_layer_stubs()
