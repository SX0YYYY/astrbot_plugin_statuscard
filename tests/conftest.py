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
