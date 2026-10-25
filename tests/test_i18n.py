from __future__ import annotations

from astrbot_plugin_statuscard.i18n import get_language, tr


def test_language_defaults_and_fallback() -> None:
    assert get_language({}) == "zh-CN"
    assert get_language({"display": {"language": "en-US"}}) == "en-US"
    assert get_language({"display": {"language": "bad"}}) == "zh-CN"
    assert get_language({"display": {"language": ""}}) == "zh-CN"


def test_language_normalizes_casing_and_separator() -> None:
    """Hand-typed values like " EN-us " or "zh_CN" must still resolve."""

    assert get_language({"display": {"language": " EN-US "}}) == "en-US"
    assert get_language({"display": {"language": "zh_CN"}}) == "zh-CN"
    assert get_language({"display": {"language": "fr"}}) == "zh-CN"


def test_translation_lookup_and_variables() -> None:
    assert tr({}, "platform_unsupported").startswith("当前平台")
    assert tr({"display": {"language": "en-US"}}, "platform_unsupported").startswith(
        "Unsupported"
    )
    assert (
        tr({"display": {"language": "en-US"}}, "cooldown", seconds=5)
        == "Status card cooldown, try again in 5 seconds."
    )
    assert tr({}, "missing_key") == "missing_key"
