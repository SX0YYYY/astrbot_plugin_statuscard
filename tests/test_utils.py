from __future__ import annotations

from astrbot_plugin_statuscard.utils import (
    clean_report_sections,
    redact_url,
    truncate_text,
)


def test_redact_url_strips_query_and_fragment() -> None:
    assert (
        redact_url("http://example.com/health?token=secret#frag")
        == "http://example.com/health"
    )
    assert (
        redact_url("https://example.com:8443/a/b?key=1")
        == "https://example.com:8443/a/b"
    )


def test_redact_url_keeps_non_url_text() -> None:
    assert redact_url("ConnectError: refused") == "ConnectError: refused"
    assert redact_url("") == ""
    assert redact_url(None) == ""


def test_truncate_text_marks_overflow() -> None:
    short = "a" * 10
    assert truncate_text(short, 20) == short
    long = truncate_text("b" * 50, 20)
    assert len(long) <= 20
    assert long.endswith("（已截断）")


def test_clean_report_sections_still_removes_todo() -> None:
    assert clean_report_sections(["resource", "todo"]) == ["resource"]
    assert clean_report_sections(["resource"]) is None
