"""Regression tests for the v2.20.28 audit batch.

Every test drives the production path that changed:

* ``run_audit`` validates notify-rule kinds with the same strip().lower()
  normalisation the runtime resolver uses, so a hand-edited "Service" is
  not reported as an unknown type that "won't work";
* ``Redactor.mask_address_tokens`` hides a credential query that survives
  a URL ending in a closing bracket, without swallowing readable tails,
  ordinary text containing "token=", or IPv6-literal URLs.
"""

from __future__ import annotations

from astrbot_plugin_statuscard.audit import run_audit
from astrbot_plugin_statuscard.redactor import Redactor
from astrbot_plugin_statuscard.utils import redact_url


def _config(notify_rules):
    return {
        "display": {"timezone": "Asia/Shanghai"},
        "alerts": {
            "enabled": False,
            "targets": [],
            "offline_notify": False,
            "platform_id": "default",
        },
        "monitors": {"enabled": False, "targets": [], "items": []},
        "daily_report": {
            "enabled": False,
            "weekly_enabled": False,
            "targets": [],
            "time": "21:00",
        },
        "notify": {"rules": notify_rules},
    }


def _mask(text: str) -> str:
    return Redactor(["hostname"]).mask_address_tokens(redact_url(text))


def test_audit_accepts_mixed_case_notify_kinds() -> None:
    """A "Service" rule works at runtime; the audit must not call it unknown."""
    issues = run_audit(
        _config([{"enabled": True, "target": "123456", "kinds": ["Service"]}])
    )
    assert not any(issue.code == "notify_rule_unknown_kind" for issue in issues)


def test_audit_still_reports_a_real_unknown_kind() -> None:
    """Lower-casing must not swallow genuinely misspelled kinds."""
    issues = run_audit(
        _config([{"enabled": True, "target": "123456", "kinds": ["Servcie"]}])
    )
    messages = [
        issue.message for issue in issues if issue.code == "notify_rule_unknown_kind"
    ]
    assert messages and "Servcie" in messages[0]


def test_audit_accepts_padded_mixed_case_kinds() -> None:
    issues = run_audit(
        _config([{"enabled": True, "target": "123456", "kinds": ["  OFFLINE "]}])
    )
    assert not any(issue.code == "notify_rule_unknown_kind" for issue in issues)


def test_bracketed_url_credential_is_hidden() -> None:
    """A URL ending in ")" could leave ";token=..." after the placeholder."""
    for text in (
        "https://host/a);token=SECRET 超时",
        "https://host/a)password=SECRET 超时",
        "https://host/a)apikey=SECRET 超时",
        "https://host/a)secret=SECRET 超时",
    ):
        out = _mask(text)
        assert "SECRET" not in out, text
        assert "超时" in out, text


def test_bracketed_url_without_credential_keeps_readable_tail() -> None:
    out = _mask("https://host/a) 正常")
    assert "正常" in out
    assert "token=" not in out


def test_normal_url_redaction_is_unchanged() -> None:
    raw = Redactor(["hostname"]).mask_address_tokens
    assert "超时" in raw("https://host/api?token=abc 超时")
    assert "token=abc" not in raw("https://host/api?token=abc 超时")
    assert "如果超时请重试" in raw("https://host/health，如果超时请重试")
    assert "SECRET" not in raw("http://[fe80::1]/p?token=SECRET 超时")
    assert "SECRET" not in raw("https://host/a;jsessionid=abc?token=SECRET 超时")
    # The audit banner calls mask_address_tokens(redact_url(message)); that
    # chain must still hide the credentials even though redact_url strips the
    # query first.
    chained = _mask("https://host/api?token=abc 超时")
    assert "token=abc" not in chained
    assert "http" not in chained


def test_plain_text_token_is_not_over_masked() -> None:
    """The bounded sweep must stay anchored to a masked URL + closing bracket."""
    out = _mask("普通文本 token=SECRET 不是 URL")
    assert "token=SECRET" in out
    assert _mask("HTTP 500 (expected 200)") == "HTTP 500 (expected 200)"
    assert "无法连接" in _mask("10.0.0.8 无法连接")


def test_bracketed_url_non_credential_key_is_not_over_masked() -> None:
    assert "monkey=abc" in _mask("https://host/a)monkey=abc 超时")
    assert "tokenless=abc" in _mask("https://host/a)tokenless=abc 超时")
    assert "说明文字" in _mask("https://host/a)说明文字")


def test_underscore_prefixed_credentials_are_masked() -> None:
    """access_token / refresh_token / client_secret must be swept too.

    The lookbehind used to reject a "_" prefix, so only the bare names were
    masked (2026-11-09 reverse audit).
    """
    # The VALUE must not itself contain a credential word: "SECRET" would
    # match the built-in "secret" key and mask the row even with the buggy
    # lookbehind, making the test pass for the wrong reason.
    for key in ("access_token", "refresh_token", "client_secret"):
        out = _mask(f"https://host/a);{key}=VALUE123 超时")
        assert "VALUE123" not in out, (key, out)
        assert "超时" in out, (key, out)


def test_letter_prefixed_words_are_not_masked() -> None:
    """A letter prefix still means the word is not a credential key."""
    assert "monkey=abc" in _mask("https://host/a)monkey=abc 超时")
    assert "tokenless=abc" in _mask("https://host/a)tokenless=abc 超时")
