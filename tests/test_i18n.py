from __future__ import annotations

from astrbot_plugin_statuscard.i18n import get_language, tr


def test_language_defaults_and_fallback() -> None:
    assert get_language({}) == "zh-CN"
    assert get_language({"display": {"language": "en-US"}}) == "en-US"
    assert get_language({"display": {"language": "bad"}}) == "zh-CN"
    assert get_language({"display": {"language": ""}}) == "zh-CN"


def test_translation_lookup_and_variables() -> None:
    assert tr({}, "admin_only").startswith("查看状态")
    assert tr({"display": {"language": "en-US"}}, "admin_only").startswith(
        "Only"
    )
    assert (
        tr({"display": {"language": "en-US"}}, "todo_list_title", count=3)
        == "📝 To-dos (3 open)"
    )
    assert tr({}, "missing_key") == "missing_key"
