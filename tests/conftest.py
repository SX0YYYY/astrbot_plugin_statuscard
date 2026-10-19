from __future__ import annotations

import sys
from pathlib import Path

# Tests import the plugin as the package ``astrbot_plugin_statuscard`` so the
# relative imports inside the plugin modules resolve. The repository root is
# the plugin directory itself, so its parent has to be on sys.path.
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT.parent))
