"""Regression tests for the v2.20.30 tech-debt batch (C group).

C5  verify with a lost body must be rejected, not silently run monitors
C6  a transient write lock (SQLITE_BUSY) is not a broken report database
C10 status() reports the configured target, with the state key beside it
B2  every audit code has a dashboard translation in both locales
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_statuscard import page_api as page_api_module
from astrbot_plugin_statuscard.monitor import ServiceMonitor
from astrbot_plugin_statuscard.page_api import PageApi
from astrbot_plugin_statuscard.report_store import ReportStore
from astrbot_plugin_statuscard.state import StateStore

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# Every stable code passed to AuditIssue in audit.py. Kept explicit on
# purpose: a new finding without a dashboard key would otherwise show Chinese
# on the English page, and a regex over the source can silently miss one.
AUDIT_CODES = [
    "alert_threshold_zero",
    "alerts_no_targets",
    "alerts_offline_no_targets",
    "alerts_resource_no_targets",
    "daily_report_no_targets",
    "daily_report_sections_empty",
    "daily_report_sections_unknown",
    "daily_summary_no_targets",
    "disk_path_missing",
    "display_timezone_invalid",
    "history_exceeds_retention",
    "invalid_schedule_time",
    "legacy_module_targets",
    "monitor_item_config",
    "monitor_item_skipped",
    "monitors_all_invalid",
    "monitors_no_items",
    "monitors_no_targets",
    "notify_center_active",
    "notify_rule_invalid",
    "notify_rule_unknown_kind",
    "offline_broadcast_requires_alerts",
    "provider_watch_no_targets",
    "silence_enabled_without_time",
    "silence_time_invalid",
    "target_format_suspect",
    "unknown_redact_rule",
    "webhook_no_token",
    "webhook_open_no_token",
    "weekly_report_no_targets",
]


def _strings(locale: str) -> dict:
    data = json.loads(
        (PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
            encoding="utf-8"
        )
    )
    return data["pages"]["status"]["strings"]


def _keys_for(code: str) -> list[str]:
    if code == "alert_threshold_zero":
        return [
            "audit_alert_threshold_zero_nan",
            "audit_alert_threshold_zero_zero",
        ]
    if code == "monitor_item_skipped":
        return [
            "audit_monitor_item_skipped_unsupported_type",
            "audit_monitor_item_skipped_duplicate",
        ]
    return ["audit_" + code]


def test_every_audit_code_has_both_locales() -> None:
    zh, en = _strings("zh-CN"), _strings("en-US")
    missing = []
    for code in AUDIT_CODES:
        for key in _keys_for(code):
            if key not in zh or key not in en:
                missing.append(key)
    # The two structured monitor reasons are rendered by the same nested
    # lookup the dashboard uses for config_error.
    for key in (
        "monitor_config_error_missing_required",
        "monitor_config_error_invalid_port",
    ):
        if key not in zh or key not in en:
            missing.append(key)
    assert missing == []


def test_audit_english_keys_have_no_chinese() -> None:
    en = _strings("en-US")
    for code in AUDIT_CODES:
        for key in _keys_for(code):
            assert not any("\u4e00" <= char <= "\u9fff" for char in en[key]), key
    for key in (
        "monitor_config_error_missing_required",
        "monitor_config_error_invalid_port",
    ):
        assert not any("\u4e00" <= char <= "\u9fff" for char in en[key]), key


def test_verify_rejects_a_missing_kind(monkeypatch) -> None:
    """A bridge that loses the JSON body used to run monitors silently."""

    async def request_value(name, default=None):
        return default

    monkeypatch.setattr(page_api_module, "_request_value", request_value)
    api = PageApi(SimpleNamespace(config={"display": {}}))
    payload = asyncio.run(api.verify())
    assert payload["ok"] is False
    assert payload["error_code"] == "verify_kind_missing"


def test_verify_rejects_an_unknown_kind(monkeypatch) -> None:
    async def request_value(name, default=None):
        return "everything"

    monkeypatch.setattr(page_api_module, "_request_value", request_value)
    api = PageApi(SimpleNamespace(config={"display": {}}))
    payload = asyncio.run(api.verify())
    assert payload["ok"] is False
    assert payload["error_code"] == "verify_kind_missing"


def test_probe_treats_a_write_lock_as_available(tmp_path, monkeypatch) -> None:
    """BUSY means another writer; the report path itself is read-only."""

    store = ReportStore(tmp_path / "report.db")
    assert store.available
    real_connect = store._connect

    class _BusyConnection:
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("database is locked")
            return SimpleNamespace(fetchone=lambda: (0,), fetchall=list)

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    calls = {"n": 0}

    def fake_connect():
        calls["n"] += 1
        if calls["n"] == 1:
            return _BusyConnection()
        return real_connect()

    monkeypatch.setattr(store, "_connect", fake_connect)
    assert store.probe() is True
    assert store.available is True


def test_probe_still_reports_a_read_only_database(tmp_path, monkeypatch) -> None:
    """A real read-only rejection must keep failing the probe."""

    store = ReportStore(tmp_path / "report.db")

    class _ReadOnlyConnection:
        def execute(self, sql, *args):
            if str(sql).strip().upper().startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return SimpleNamespace(fetchone=lambda: (0,), fetchall=list)

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(store, "_connect", lambda: _ReadOnlyConnection())
    assert store.probe() is False
    assert store.available is False


def test_status_reports_the_configured_target(tmp_path) -> None:
    """``target`` is what the operator configured; ``state_key`` is identity."""

    state = StateStore(tmp_path / "state.json")
    config = {
        "display": {"timezone": "Asia/Shanghai"},
        "monitors": {
            "enabled": True,
            "items": [
                {
                    "type": "http",
                    "name": "文档",
                    "url": "https://h/x?token=SECRET",
                    "keyword": "ok",
                }
            ],
        },
    }
    monitor = ServiceMonitor(None, state, config)
    item = monitor.status()["items"][0]
    # The display target must not leak the internal query hash / state suffix.
    assert "#q" not in item["target"]
    assert "#q" in item["state_key"]
    assert item["target"].startswith("https://h/")
    assert "SECRET" not in item["target"]


def test_diag_issue_text_translates_and_falls_back() -> None:
    """The diagnostics command uses the same codes as the dashboard."""

    from astrbot_plugin_statuscard.audit import AuditIssue
    from astrbot_plugin_statuscard.main import StatusCardPlugin
    from astrbot_plugin_statuscard.redactor import Redactor

    zh_plugin = SimpleNamespace(config={"display": {"language": "zh-CN"}})
    en_plugin = SimpleNamespace(config={"display": {"language": "en-US"}})
    redactor = Redactor([])
    issue = AuditIssue(
        "warning",
        "monitors_no_targets",
        "服务监测已启用，但没有可用的推送目标",
    )
    # The Chinese table carries the full sentence (the test message is its
    # prefix), so startswith is the right assertion.
    assert StatusCardPlugin._diag_issue_text(zh_plugin, issue, redactor).startswith(
        issue.message
    )
    english = StatusCardPlugin._diag_issue_text(en_plugin, issue, redactor)
    assert "Service monitoring" in english
    assert not any("\u4e00" <= char <= "\u9fff" for char in english)

    # An unknown code must still be actionable.
    unknown = AuditIssue("warning", "brand_new_code", "新的中文提示")
    assert (
        StatusCardPlugin._diag_issue_text(en_plugin, unknown, redactor)
        == "新的中文提示"
    )

    # The nested config-error reason is translated too.
    nested = AuditIssue(
        "warning",
        "monitor_item_config",
        "监测项「A」（HTTP）缺少 url 配置",
        {
            "name": "A",
            "kind": "HTTP",
            "error": "缺少 url 配置",
            "error_code": "missing_required",
            "error_params": {"required": "url"},
        },
    )
    text = StatusCardPlugin._diag_issue_text(en_plugin, nested, redactor)
    # The English template gained the {kind} placeholder with v2.21.3 (it
    # used to drop it while the Chinese one had it).
    assert text == 'Monitor item "A" (HTTP): missing the url setting'
