"""Contract tests for the provider-quality note (v2.20.30).

The same three sentences exist in two places: the report image builds them
from ``report_view.QUALITY_NOTE_*`` (always Chinese) and the dashboard
translates ``quality_note`` / ``quality_note_long`` / ``quality_note_truncated``
from ``.astrbot-plugin/i18n/zh-CN.json``. They are hand-maintained copies, so
this test fails the moment they drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_statuscard import report_view

ROOT = Path(__file__).resolve().parents[1]


def _strings(locale: str) -> dict:
    data = json.loads(
        (ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
            encoding="utf-8"
        )
    )
    return data["pages"]["status"]["strings"]


def test_quality_note_constants_match_the_dashboard_copy() -> None:
    zh = _strings("zh-CN")
    assert zh["quality_note"] == report_view.QUALITY_NOTE_BASE
    assert zh["quality_note_long"].format(n=3) == report_view.QUALITY_NOTE_LONG.format(
        n=3
    )
    assert zh["quality_note_truncated"] == report_view.QUALITY_NOTE_TRUNCATED


def test_english_quality_note_has_no_chinese() -> None:
    en = _strings("en-US")
    for key in ("quality_note", "quality_note_long", "quality_note_truncated"):
        assert key in en
        assert not any("\u4e00" <= char <= "\u9fff" for char in en[key]), key
