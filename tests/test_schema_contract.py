"""Contract tests for the configuration schema, i18n dictionaries and docs.

These used to live only in the CI shell snippet (JSON parses + dictionary
symmetry). A wrong ``template_list`` shape renders the config page as a text
box and a missing page key silently falls back to Chinese in the English UI,
so both are checked here where every ``pytest`` run and CI see them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _schema() -> dict:
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8-sig"))


def _walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def test_template_lists_declare_templates() -> None:
    """Every template_list must carry ``templates.<key>.items``.

    AstrBot 4.29 renders a bare ``items`` mapping as a text box; the
    ``__template_key`` field is added by the UI, never declared in the schema.
    """

    for path, node in _walk(_schema()):
        if node.get("type") != "template_list":
            continue
        templates = node.get("templates")
        assert isinstance(templates, dict) and templates, f"{path}: no templates"
        for name, template in templates.items():
            assert isinstance(template, dict), f"{path}.{name}: not an object"
            assert isinstance(template.get("items"), dict), (
                f"{path}.{name}: missing items"
            )
            assert "__template_key" not in template["items"], (
                f"{path}.{name}: __template_key must not be declared"
            )


def test_changelog_top_version_matches() -> None:
    version = re.search(
        r'__version__\s*=\s*"([^"]+)"',
        (ROOT / "version.py").read_text(encoding="utf-8"),
    ).group(1)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## v([0-9][^\s]*)", changelog, re.MULTILINE)
    assert match, "CHANGELOG has no version heading"
    assert match.group(1) == version, (
        f"CHANGELOG top version {match.group(1)} != version.py {version}"
    )


def test_every_literal_page_key_exists_in_both_locales() -> None:
    """``t("key", ...)`` literals in app.js must exist in zh-CN and en-US."""

    app = (ROOT / "pages" / "status" / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'\bt\(\s*"([A-Za-z0-9_]+)"', app))
    assert used, "no literal t() keys found"
    for locale in ("zh-CN", "en-US"):
        data = json.loads(
            (ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
                encoding="utf-8"
            )
        )
        strings = data["pages"]["status"]["strings"]
        missing = sorted(used - set(strings))
        assert not missing, f"{locale} is missing page keys: {missing}"


def test_schema_disk_paths_is_a_list() -> None:
    display = _schema()["display"]["items"]
    assert display["disk_paths"]["type"] == "list"
    assert display["disk_paths"]["default"] == []
