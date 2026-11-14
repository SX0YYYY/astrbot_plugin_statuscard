"""One startup pass that turns configuration mistakes into visible issues.

Most "I configured it but nothing happens" reports come from a handful of
quiet combinations: alerts enabled without a push target, monitor items
missing required fields, a weekly report with nowhere to go. The audit
collects them in one place so they can be logged once at start-up and
shown on the dashboard, instead of being discovered by silence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .collectors import (
    cfg_bool,
    cfg_float,
    cfg_get,
    notify_rules_active,
    resolve_targets,
)
from .monitor import KIND_LABELS, REQUIRED_FIELDS
from .platform_support import is_official_platform
from .redactor import RULES as REDACT_RULES
from .utils import mask_chat_ids, normalize_targets, redact_url


def _safe_label(value: Any) -> str:
    """A monitor name / target that is safe to print in an audit message.

    The name is operator-provided and is frequently a pasted URL (with
    credentials and query tokens) or a chat id. The audit banner and the
    ``状态卡诊断`` command printed it verbatim, so an invalid configuration
    leaked its credentials on the dashboard (v2.20.17).
    """

    return mask_chat_ids(redact_url(str(value or "")))


@dataclass(frozen=True)
class AuditIssue:
    """A single configuration finding shown to the operator."""

    level: str  # "error", "warning" or "info"
    code: str  # stable identifier the dashboard can key on
    message: str
    # Dynamic values interpolated into ``message`` so the dashboard can render
    # an English sentence without parsing the Chinese one (v2.20.30). Empty
    # for static findings.
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "params": dict(self.params),
        }


def _effective_targets(config: Any, key: str, platform_id: str, kind: str) -> list[str]:
    """Resolve targets for one kind, falling back to the legacy list.

    The notification center takes over a kind only when an enabled rule
    matches it; every other kind keeps its per-module list. Checking only the
    legacy list produced false "no targets" errors for users who moved their
    routing into the notification center.
    """

    picked = resolve_targets(config, kind)
    if picked is not None:
        return picked
    raw = cfg_get(config, key, [])
    if not raw:
        raw = cfg_get(config, "alerts.targets", [])
    return normalize_targets(raw, platform_id)


def _monitor_config_errors(config: Any) -> list[dict[str, str]]:
    """Name, kind and reason of monitor items that cannot be probed."""

    problems: list[dict[str, str]] = []
    items = cfg_get(config, "monitors.items", []) or []
    if not isinstance(items, list):
        return problems
    for item in items:
        if not isinstance(item, dict) or not cfg_bool(item, "enabled", True):
            continue
        kind = str(item.get("__template_key") or item.get("type") or "").lower()
        if kind not in REQUIRED_FIELDS:
            continue
        required = REQUIRED_FIELDS[kind]
        name = _safe_label(item.get("name") or kind)
        if not item.get(required):
            problems.append(
                {
                    "name": name,
                    "kind": KIND_LABELS[kind],
                    "error": f"缺少 {required} 配置",
                    "error_code": "missing_required",
                    "error_params": {"required": required},
                }
            )
        elif kind == "tcp":
            raw_port = item.get("port")
            # cfg_int (the runtime path) rejects booleans before int(): a
            # `port: true` used to pass this audit as int(True)==1 while the
            # probe reported a config error (2026-11-08 audit).
            if isinstance(raw_port, bool):
                problems.append(
                    {
                        "name": name,
                        "kind": KIND_LABELS[kind],
                        "error": f"端口不是数字（当前值 {raw_port!r}），无法探测",
                        "error_code": "invalid_port",
                        "error_params": {"port": str(raw_port)},
                    }
                )
                continue
            try:
                port = int(raw_port or 0)
            except (TypeError, ValueError):
                problems.append(
                    {
                        "name": name,
                        "kind": KIND_LABELS[kind],
                        "error": f"端口不是数字（当前值 {raw_port!r}），无法探测",
                        "error_code": "invalid_port",
                        "error_params": {"port": str(raw_port)},
                    }
                )
                continue
            if not (1 <= port <= 65535):
                problems.append(
                    {
                        "name": name,
                        "kind": KIND_LABELS[kind],
                        "error": f"端口 {port} 超出 1–65535",
                        "error_code": "invalid_port",
                        "error_params": {"port": str(port)},
                    }
                )
    return problems


def _monitor_unsupported_types(config: Any) -> list[dict[str, str]]:
    """Items whose type name the probe loop does not recognise.

    ``_normalize_items`` drops them with one log line. The audit used to skip
    them too (its ``kind not in REQUIRED_FIELDS`` branch), so a typo left the
    user with a "no problems" banner and an item that never ran.
    """

    problems: list[dict[str, str]] = []
    items = cfg_get(config, "monitors.items", []) or []
    if not isinstance(items, list):
        return problems
    for item in items:
        if not isinstance(item, dict) or not cfg_bool(item, "enabled", True):
            continue
        kind = str(item.get("__template_key") or item.get("type") or "").lower()
        if kind in REQUIRED_FIELDS:
            continue
        name = _safe_label(item.get("name") or kind or "(未命名)")
        problems.append(
            {
                "name": name,
                "kind": kind or "(空)",
                "reason": "unsupported_type",
            }
        )
    return problems


def run_audit(
    config: Any,
    *,
    monitor: Any = None,
    platform_instances: dict[str, str] | None = None,
) -> list[AuditIssue]:
    """Collect configuration findings; called once at plugin start-up.

    ``monitor`` (if given) supplies the already-normalized items so the
    audit and the probe loop cannot disagree about what is broken.
    ``platform_instances`` maps live AstrBot platform ids to adapter names; a
    mismatch with ``alerts.platform_id`` makes every scheduled push fail with
    "no matching platform" (the usual state after switching from NapOneBot to
    the QQ official bot), so it is reported here.
    """

    issues: list[AuditIssue] = []
    platform_id = str(cfg_get(config, "alerts.platform_id", "default") or "default")
    alerts_on = cfg_bool(config, "alerts.enabled", False)
    monitors_on = cfg_bool(config, "monitors.enabled", False)
    daily_on = cfg_bool(config, "daily_report.enabled", False)
    weekly_on = cfg_bool(config, "daily_report.weekly_enabled", False)
    alerts_targets = normalize_targets(
        cfg_get(config, "alerts.targets", []), platform_id
    )
    # The daily card and the LLM provider watch run their own loops and are
    # independent of alerts.enabled (alerts.start()), so a stale platform id
    # breaks them while the alerts switch is off. A deployment that only uses
    # one of those still needs the platform checks (v2.21.1 audit).
    daily_image_on = cfg_bool(config, "alerts.daily_summary.enabled", False)
    provider_watch_on = cfg_bool(config, "alerts.provider_watch.enabled", False)
    resource_targets = resolve_targets(config, "resource")
    if resource_targets is None:
        resource_targets = alerts_targets
    offline_targets = resolve_targets(config, "offline")
    if offline_targets is None:
        offline_targets = alerts_targets
    monitor_targets = _effective_targets(
        config, "monitors.targets", platform_id, "service"
    )
    daily_targets = _effective_targets(
        config, "daily_report.targets", platform_id, "daily"
    )
    weekly_targets = _effective_targets(
        config, "daily_report.targets", platform_id, "weekly"
    )
    daily_image_targets = _effective_targets(
        config, "alerts.targets", platform_id, "daily_image"
    )

    if monitors_on and not monitor_targets:
        issues.append(
            AuditIssue(
                "error",
                "monitors_no_targets",
                "服务监测已启用，但没有可用的推送目标（通知中心无 service 规则，"
                "默认推送目标也为空）。为避免无意义探测，监测项不会探测、也不会"
                "记录事件（看板与日报里不会有监测数据）；请先在"
                "「告警与每日定时状态图 → 默认推送目标」填写群号。",
            )
        )
    if monitors_on and monitor is not None and not monitor.items:
        # "Enabled but empty" is the classic silent configuration: the switch
        # looks on, nothing is probed, and only a log line says so. Distinguish
        # "all items disabled" from "all item types unknown/incomplete": the
        # old text told the user to add items even when they had some.
        raw_items = cfg_get(config, "monitors.items", [])
        raw_items = raw_items if isinstance(raw_items, list) else []
        enabled_items = [
            item
            for item in raw_items
            if isinstance(item, dict) and cfg_bool(item, "enabled", True)
        ]
        if enabled_items:
            issues.append(
                AuditIssue(
                    "warning",
                    "monitors_all_invalid",
                    "服务监测已启用，但所有监测项的类型不受支持或配置不完整，"
                    "不会有任何探测或告警；请检查每个条目的类型与必填项。",
                )
            )
        else:
            issues.append(
                AuditIssue(
                    "warning",
                    "monitors_no_items",
                    "服务监测已启用，但监测项列表为空（或全部停用），"
                    "不会有任何探测或告警。",
                )
            )
    if monitors_on:
        config_problems = (
            _monitor_config_errors(config)
            if monitor is None
            else [
                {
                    "name": _safe_label(item.get("name")),
                    "kind": KIND_LABELS.get(str(item.get("__template_key")), "?"),
                    "error": str(item.get("config_error")),
                    "error_code": item.get("config_error_code"),
                    "error_params": item.get("config_error_params") or {},
                }
                for item in monitor.items
                if item.get("config_error")
            ]
        )
        for problem in config_problems:
            issues.append(
                AuditIssue(
                    "warning",
                    "monitor_item_config",
                    f"监测项「{problem['name']}」（{problem['kind']}）"
                    f"{problem['error']}",
                    problem,
                )
            )
        # Items the probe loop dropped entirely (unknown type / exact
        # duplicate). Without this the audit reported a clean configuration
        # while a mistyped item never ran and never alerted (2026-11-06
        # audit). ``skipped_details`` is the structured twin used here; the
        # plain ``skipped_items`` strings remain for logs.
        skipped_details = (
            list(getattr(monitor, "skipped_details", []) or [])
            if monitor is not None
            else _monitor_unsupported_types(config)
        )
        for detail in skipped_details:
            if detail.get("reason") == "duplicate":
                message = f"监测项「{detail.get('name')}」与前面的条目完全重复，已忽略"
            else:
                message = (
                    f"监测项「{detail.get('name')}」的类型「{detail.get('kind')}」"
                    "不受支持，已跳过探测"
                )
            issues.append(
                AuditIssue("warning", "monitor_item_skipped", message, detail)
            )
    if alerts_on and not resource_targets and not offline_targets:
        issues.append(
            AuditIssue(
                "error",
                "alerts_no_targets",
                "资源与掉线告警已启用，但两类都没有可用的推送目标（通知中心无 resource/"
                "offline 规则，默认推送目标也为空），告警不会送达任何会话。"
                "请在「告警与每日定时状态图 → 默认推送目标」填写群号。",
            )
        )
    elif alerts_on:
        if not resource_targets:
            # The resource rule has its own kind: a notification center that
            # only covers "offline" leaves CPU/RAM alerts with nowhere to go,
            # and the old check stayed silent in exactly that case.
            issues.append(
                AuditIssue(
                    "warning",
                    "alerts_resource_no_targets",
                    "资源告警（CPU / 内存 / SWAP / 磁盘 / 负载）没有可用的推送目标"
                    "（通知中心无 resource 规则，默认推送目标也为空），触发时不会送达。",
                )
            )
        if not offline_targets and cfg_bool(config, "alerts.offline_notify", True):
            # The offline rule has its own notification kind: a rule that only
            # covers "resource" leaves disconnect notices without a destination.
            issues.append(
                AuditIssue(
                    "warning",
                    "alerts_offline_no_targets",
                    "掉线 / 恢复通知没有可用的推送目标（通知中心无 offline 规则，"
                    "默认推送目标也为空），掉线时不会收到消息。",
                )
            )
    if daily_on and not daily_targets:
        issues.append(
            AuditIssue(
                "warning",
                "daily_report_no_targets",
                "趋势日报已启用，但没有可用的推送目标（通知中心无 daily 规则，"
                "默认推送目标也为空），日报不会发送。",
            )
        )
    if weekly_on and not weekly_targets:
        issues.append(
            AuditIssue(
                "warning",
                "weekly_report_no_targets",
                "周报已启用，但没有可用的推送目标（通知中心无 weekly 规则，"
                "默认推送目标也为空），周报不会发送。",
            )
        )
    if (
        cfg_bool(config, "alerts.daily_summary.enabled", False)
        and not daily_image_targets
    ):
        issues.append(
            AuditIssue(
                "warning",
                "daily_summary_no_targets",
                "每日定时状态图已启用，但没有可用的推送目标（通知中心无 daily_image 规则，"
                "默认推送目标也为空），到点后会静默跳过且当天不重试。",
            )
        )
    if cfg_bool(config, "alerts.provider_watch.enabled", False):
        # The notification center takes over the "provider" kind once a rule
        # matches; otherwise the alert falls back to alerts.targets.
        provider_targets = resolve_targets(config, "provider")
        if provider_targets is None:
            provider_targets = alerts_targets
        if not provider_targets:
            issues.append(
                AuditIssue(
                    "warning",
                    "provider_watch_no_targets",
                    "LLM 服务告警已启用，但没有可用的推送目标（通知中心无 provider 规则，"
                    "默认推送目标也为空），连续失败时不会送达任何会话。",
                )
            )
    # A Webhook that cannot start is worth more than a log line in the
    # background: the switch looks on, nothing listens, and no other surface
    # reflects the refusal.
    if cfg_bool(config, "alerts.komari_webhook.enabled", False):
        webhook_token = str(
            cfg_get(config, "alerts.komari_webhook.token", "") or ""
        ).strip()
        # Same fallback the runtime uses (strip first, THEN the loopback
        # default): an empty or blank host binds loopback and starts fine --
        # audit used to read it as "" and raise a false "会拒绝启动" error
        # against a perfectly working setup, and a whitespace-only host used
        # to slip past this check as "" while the runtime treated it as a
        # wildcard bind (v2.20.32 normalises both sides identically).
        webhook_host = (
            str(
                cfg_get(config, "alerts.komari_webhook.host", "127.0.0.1") or ""
            ).strip()
            or "127.0.0.1"
        )
        # Same normalisation as the runtime check (_webhook_bind_allowed):
        # "0:0:0:0:0:0:0:0" and "[::]" are wildcard binds too, and the audit
        # used to accept them while the listener refused to start.
        webhook_host_norm = webhook_host.strip("[]").lower()
        if cfg_bool(config, "alerts.komari_webhook.require_token", True) and not (
            webhook_token
        ):
            issues.append(
                AuditIssue(
                    "error",
                    "webhook_no_token",
                    "Komari Webhook 已启用且勾选了「强制要求 Token」，但没有填写 Token，"
                    "Webhook 会拒绝启动。请填写 Token 或关闭该开关。",
                )
            )
        elif webhook_host_norm in {
            "0.0.0.0",
            "::",
            "0:0:0:0:0:0:0:0",
        } and not cfg_bool(config, "alerts.komari_webhook.require_token", True):
            # A token that is configured but not enforced leaves the listener
            # open to anyone who can reach the port, so the runtime refuses to
            # start it; report the same condition here instead of only
            # checking whether a token string exists.
            issues.append(
                AuditIssue(
                    "error",
                    "webhook_open_no_token",
                    f"Komari Webhook 监听所有网卡（{webhook_host}）但没有启用 "
                    "Token 鉴权（未勾选「强制要求 Token」），出于安全考虑会拒绝"
                    "启动。请勾选并填写 Token，或改回 127.0.0.1。",
                    {"host": webhook_host},
                )
            )
    if cfg_bool(config, "alerts.offline_broadcast.enabled", False) and not cfg_bool(
        config, "alerts.enabled", False
    ):
        # Disconnect detection lives inside the alert loop; with the alert
        # switch off the broadcast can never fire.
        issues.append(
            AuditIssue(
                "warning",
                "offline_broadcast_requires_alerts",
                "「掉线恢复全群广播」已开启，但「启用资源与掉线告警」未开启："
                "掉线检测不会运行，广播也不会触发。请先打开告警总开关。",
            )
        )
    # ``alerts.provider_watch`` has its own loop and no longer requires the
    # resource/offline master switch (v2.20.21). The old warning here told the
    # operator to turn the master switch on, which also enabled resource and
    # offline alerts they did not ask for.
    # A hand-edited schedule like "9:00" (or "25:00") only logs a warning and
    # the task never fires, while the diagnostics still show the raw value.
    schedule_defaults = {
        "daily_report.time": "21:00",
        "alerts.daily_summary.time": "09:00",
        "daily_report.weekly_time": "21:30",
    }
    for enabled_key, time_key, label in (
        ("daily_report.enabled", "daily_report.time", "趋势日报"),
        ("alerts.daily_summary.enabled", "alerts.daily_summary.time", "每日定时状态图"),
        ("daily_report.weekly_enabled", "daily_report.weekly_time", "周报"),
    ):
        if not cfg_bool(config, enabled_key, False):
            continue
        raw = str(cfg_get(config, time_key, "") or "").strip()
        if not raw:
            # Runtime reads an empty/missing value as the documented default
            # (21:00 / 09:00 / 21:30). Reporting "定时任务不会触发" here was a
            # false error banner on a perfectly working install.
            raw = schedule_defaults.get(time_key, "")
        # Runtime (alerts/report) parses int(hour)/int(minute), so "9:05"
        # and "9:5" both schedule fine; the audit used to reject them with
        # a false "定时任务不会触发" error (v2.20.0).
        parts = str(raw).strip().split(":")
        valid_time = False
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            hour, minute = int(parts[0]), int(parts[1])
            valid_time = 0 <= hour <= 23 and 0 <= minute <= 59
        if not valid_time:
            issues.append(
                AuditIssue(
                    "error",
                    "invalid_schedule_time",
                    f"{label}的发送时间「{raw}」格式非法（应为 HH:MM），"
                    "定时任务不会触发。",
                    {"label": label, "raw": raw},
                )
            )
    tz_name = str(cfg_get(config, "display.timezone", "Asia/Shanghai") or "").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz_name)
        except Exception:
            issues.append(
                AuditIssue(
                    "warning",
                    "display_timezone_invalid",
                    f"时区「{tz_name}」无法识别，将回退到服务器本地时区；"
                    "状态卡、告警和日报的时间会整体偏移，请检查拼写"
                    "（例如 Asia/Shanghai）。",
                    {"tz_name": tz_name},
                )
            )
    if alerts_on:
        # A threshold of 0 disables the rule (see AlertEngine._evaluate); the
        # audit only needs to make that silent behaviour discoverable.
        for key, label in (
            ("alerts.cpu_threshold", "CPU 使用率"),
            ("alerts.mem_threshold", "内存使用率"),
            ("alerts.swap_threshold", "SWAP 使用率"),
            ("alerts.disk_threshold", "磁盘使用率"),
            ("alerts.load_per_core_threshold", "每核负载"),
        ):
            try:
                value = cfg_float(config, key, 1.0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                # ``NaN <= 0`` is False, so the old check silently skipped a
                # hand-edited NaN/Infinity threshold while clamp_config turns
                # it into the lower bound (0 = the rule is off). The audit
                # exists to make exactly that kind of silent no-op visible.
                issues.append(
                    AuditIssue(
                        "warning",
                        "alert_threshold_zero",
                        f"告警阈值「{label}」不是有效数字（NaN/Infinity），"
                        "运行时会按 0 处理（规则关闭），请改成有效数值。",
                        {"label": label, "variant": "nan"},
                    )
                )
            elif value <= 0:
                issues.append(
                    AuditIssue(
                        "warning",
                        "alert_threshold_zero",
                        f"告警阈值「{label}」为 0：该规则不生效（视为关闭）。"
                        "如需关闭请保持 0；如需告警请设为大于 0 的值。",
                        {"label": label, "variant": "zero"},
                    )
                )
    # The mount list drives the CARD, the report and the dashboard even when
    # resource alerts are off, so a typo here is not an alert-only problem.
    # This check used to sit inside ``if alerts_on`` (off by default), which
    # is exactly when nobody would notice.
    disk_paths = cfg_get(config, "display.disk_paths", [])
    if isinstance(disk_paths, list):
        for raw_path in disk_paths:
            path_text = str(raw_path or "").strip()
            if not path_text:
                continue
            try:
                exists = Path(path_text).exists()
            except OSError:
                continue
            if not exists:
                issues.append(
                    AuditIssue(
                        "warning",
                        "disk_path_missing",
                        f"磁盘挂载点「{path_text}」不存在：状态卡、日报和看板会显示 --，"
                        "请检查「状态卡显示 → 磁盘挂载点」。",
                        {"path": path_text},
                    )
                )
    active_rules = notify_rules_active(config)
    if active_rules:
        # "info": the switch is on and everything works -- it must not be
        # counted as "N 项待处理" in the diagnostics command.
        issues.append(
            AuditIssue(
                "info",
                "notify_center_active",
                f"通知中心已启用（{active_rules} 条有效规则）：规则覆盖的类型走规则目标，"
                "其余类型继续使用「默认推送目标」。这是正常状态，无需处理；"
                "只有当某类型收不到通知时才需要检查这里的覆盖关系。",
                {"count": active_rules},
            )
        )
    # Validate every rule, not just when no rule is active: one working rule
    # used to hide a second rule with a typo or a missing target.
    raw_rules = cfg_get(config, "notify.rules", []) or []
    if not isinstance(raw_rules, list):
        # A hand-edited scalar here used to raise TypeError inside run_audit
        # and abort plugin initialization entirely.
        raw_rules = []
    valid_kinds = {
        "resource",
        "service",
        "offline",
        "provider",
        "daily",
        "weekly",
        "daily_image",
    }
    invalid_rules = 0
    unknown_kinds: set[str] = set()
    for rule in raw_rules:
        if not isinstance(rule, dict) or not cfg_bool(rule, "enabled", True):
            continue
        kinds = cfg_get(rule, "kinds", []) or []
        if isinstance(kinds, str):
            kinds = [kinds]
        if isinstance(kinds, tuple):
            # The resolver accepts a tuple too (hand-edited config); treating
            # it as invalid used to report "missing target or kinds" for a
            # rule that had both -- the opposite of the truth -- while the
            # legacy branch below already accepts (list, tuple, set).
            kinds = list(kinds)
        if not isinstance(kinds, list):
            kinds = []
        if not str(cfg_get(rule, "target", "") or "").strip() or not kinds:
            invalid_rules += 1
            continue
        for kind in kinds:
            # ``resolve_targets`` normalises kinds with strip().lower() before
            # matching, so a hand-edited "Service" DOES take effect at runtime.
            # Validating the raw spelling here reported it as an unknown type
            # that "won't work" -- the opposite of the truth, and it pushed the
            # operator into editing a working rule (2026-11-09 audit).
            normalized = str(kind).strip().lower()
            if normalized and normalized not in valid_kinds:
                unknown_kinds.add(str(kind).strip())
    if invalid_rules:
        issues.append(
            AuditIssue(
                "warning",
                "notify_rule_invalid",
                f"通知中心有 {invalid_rules} 条规则未生效："
                "缺少推送目标或未勾选任何通知类型。",
                {"count": invalid_rules},
            )
        )
    if unknown_kinds:
        issues.append(
            AuditIssue(
                "warning",
                "notify_rule_unknown_kind",
                "通知中心的规则里有无法识别的通知类型："
                + "、".join(sorted(unknown_kinds))
                + "（可选：resource / service / offline / provider / "
                "daily / weekly / daily_image），这些类型不会生效。",
                {"kinds": "、".join(sorted(unknown_kinds))},
            )
        )
    # The per-module target lists are legacy: they still work (and win over
    # the shared default), but they are hidden behind "show advanced" now.
    # Surface them so a user who forgot about them knows why that module
    # ignores the default target. A notification-center rule for the same
    # kind takes over (the legacy list is then ignored), so do not warn in
    # that case (v2.20.5).
    covered_kinds: set[str] = set()
    raw_rules = cfg_get(config, "notify.rules", None)
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, dict) or not cfg_bool(rule, "enabled", True):
                continue
            if not str(cfg_get(rule, "target", "") or "").strip():
                continue
            # ``kinds`` is a list in the schema, but the resolver and the
            # validation above both accept a bare string. Iterating the string
            # here split "service" into characters, so the audit reported the
            # module as uncovered and printed a warning whose claim was the
            # opposite of the truth (v2.20.22).
            kinds = cfg_get(rule, "kinds", []) or []
            if isinstance(kinds, str):
                kinds = [kinds]
            if not isinstance(kinds, (list, tuple, set)):
                continue
            for kind in kinds:
                covered_kinds.add(str(kind).strip().lower())
    legacy_scopes = []
    for key, label, kinds in (
        ("monitors.targets", "服务监测", {"service"}),
        (
            "daily_report.targets",
            "日报/周报",
            {"daily", "weekly", "daily_image"},
        ),
    ):
        if covered_kinds & kinds:
            continue
        raw = cfg_get(config, key, []) or []
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list) and any(
            isinstance(item, str) and item.strip() for item in raw
        ):
            legacy_scopes.append(label)
    if legacy_scopes:
        issues.append(
            AuditIssue(
                "warning",
                "legacy_module_targets",
                f"检测到{'、'.join(legacy_scopes)}单独配置了推送目标：该模块会用它而不是"
                "「默认推送目标」。如果本意就是统一发送，请在对应分组的「显示高级选项」里"
                "清空这一项。",
                {"scopes": "、".join(legacy_scopes)},
            )
        )
    # normalize_rules silently drops unknown values; a typo such as
    # "hostnames" therefore looks like redaction is on while nothing is
    # hidden. Make the ignored values visible.
    for key, label in (
        ("display.redact", "状态卡脱敏"),
        ("display.page_redact", "看板脱敏"),
    ):
        raw = cfg_get(config, key, []) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            # ``normalize_rules`` silently drops a dict/int/bool, so every
            # rule is off while the switch still looks configured. This is a
            # privacy failure: the audit used to stay quiet (v2.21.1 audit).
            issues.append(
                AuditIssue(
                    "warning",
                    "redact_rules_invalid",
                    f"{label}的配置值类型不正确（应为列表），"
                    "当前所有脱敏规则都不会生效。",
                    {"label": label},
                )
            )
            continue
        unknown = [
            str(item).strip()
            for item in raw
            if str(item).strip() and str(item).strip().lower() not in REDACT_RULES
        ]
        if unknown:
            issues.append(
                AuditIssue(
                    "warning",
                    "unknown_redact_rule",
                    f"{label}规则含未知值：{', '.join(sorted(set(unknown)))}；"
                    f"可用值为 {', '.join(REDACT_RULES)}，未知值会被忽略。",
                    {
                        "label": label,
                        "unknown": ", ".join(sorted(set(unknown))),
                        "allowed": ", ".join(REDACT_RULES),
                    },
                )
            )
    # "Enabled but every section unchecked" is the second silent failure:
    # the scheduler skips the report entirely, while the switch still looks
    # on. A legacy "todo" section list also migrates to [].
    daily_sections = cfg_get(config, "daily_report.sections", None)
    if daily_on and isinstance(daily_sections, list) and not daily_sections:
        issues.append(
            AuditIssue(
                "warning",
                "daily_report_sections_empty",
                "「日报内容」全部未勾选，定时日报不会发送。"
                "请勾选至少一个模块，或关闭「启用趋势日报」。",
            )
        )
    # A section list that names only unknown modules (a retired "todo", a
    # typo, a Chinese label) filters down to an empty list at runtime, so
    # the report silently never sends while the switch looks on and the
    # audit stayed quiet. Point at the unrecognised names.
    if daily_on and isinstance(daily_sections, list) and daily_sections:
        from .report_view import VALID_SECTIONS

        allowed = {str(item) for item in daily_sections if isinstance(item, str)}
        unknown = sorted(allowed - set(VALID_SECTIONS))
        if unknown:
            issues.append(
                AuditIssue(
                    "warning",
                    "daily_report_sections_unknown",
                    f"「日报内容」含无法识别的模块：{', '.join(unknown)}；"
                    f"可用模块为 {', '.join(VALID_SECTIONS)}。"
                    "这些名称会被忽略，若没有其它有效模块则日报不会发送。",
                    {
                        "unknown": ", ".join(unknown),
                        "allowed": ", ".join(VALID_SECTIONS),
                    },
                )
            )
    try:
        history_days = int(cfg_get(config, "daily_report.history_days", 7) or 7)
        retention_days = int(cfg_get(config, "daily_report.retention_days", 30) or 30)
    except (TypeError, ValueError):
        history_days = retention_days = 0
    # Compare what the runtime actually uses, not the raw config: the
    # collector clamps retention to 7–365 and the builder clamps the trend
    # window to 7–30. A raw retention=3 or history=40 therefore warned about
    # missing days that the plugin never misses (v2.20.19).
    history_days = max(7, min(30, history_days))
    retention_days = max(7, min(365, retention_days))
    if daily_on and retention_days < history_days:
        issues.append(
            AuditIssue(
                "warning",
                "history_exceeds_retention",
                f"趋势天数（{history_days}）大于资源保留天数（{retention_days}），"
                f"趋势图会缺 {history_days - retention_days} 天数据；"
                "请调大「资源保留天数」或调小「历史趋势天数」。",
                {
                    "history_days": history_days,
                    "retention_days": retention_days,
                    "missing": history_days - retention_days,
                },
            )
        )
    if monitors_on and cfg_bool(config, "monitors.silence_enabled", False):
        silence_until = str(cfg_get(config, "monitors.silence_until", "") or "").strip()
        if not silence_until:
            issues.append(
                AuditIssue(
                    "warning",
                    "silence_enabled_without_time",
                    "已开启「维护静默」但未填写截止时间，静默不会生效（留空表示不静默）。",
                )
            )
        else:
            # The runtime accepts both "HH:MM" and "HH:MM:SS" (monitor.py
            # ``_silence_until``). Checking only the first form reported a
            # configuration that works as "current won't take effect" --
            # the administrator then "fixed" a value that was already valid
            # (v2.20.17). Keep the two parsers in sync.
            parsed_silence = False
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    datetime.strptime(silence_until, fmt)
                    parsed_silence = True
                    break
                except ValueError:
                    continue
            if not parsed_silence:
                issues.append(
                    AuditIssue(
                        "warning",
                        "silence_time_invalid",
                        f"维护静默截止时间「{silence_until}」格式不正确，"
                        "应为 YYYY-MM-DD HH:MM，当前不会生效。",
                        {"value": silence_until},
                    )
                )
    # A mistyped target is the most common "alerts never arrive" cause and
    # previously only produced a runtime log line. The prefix may be any
    # non-space text: QQ official instances are often named in Chinese
    # (e.g. 小辞), and the old ASCII-only pattern flagged them as suspicious.
    target_pattern = re.compile(r"^[^:\s]+:[A-Za-z]+:[^:\s]+$")
    # Notification-center rules are the recommended way to route since
    # v2.18.0, and a wrong target there is exactly where a per-group setup
    # goes wrong -- so their targets run through the same sanity check.
    rule_targets: list[tuple[str, str]] = []
    rules = cfg_get(config, "notify.rules", [])
    if isinstance(rules, list):
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            # cfg_bool understands "false"/"off"/"0" from a hand-edited
            # config; the old literal check treated those as enabled and
            # flagged a disabled rule's target (v2.21.1 audit).
            if not cfg_bool(rule, "enabled", True):
                continue
            text = str(rule.get("target") or "").strip()
            if text:
                rule_targets.append((text, f"通知中心规则 #{index + 1}"))
    for key, label in (
        ("alerts.targets", "默认推送目标"),
        ("monitors.targets", "监测推送目标"),
        ("daily_report.targets", "日报推送目标"),
    ):
        raw = cfg_get(config, key, [])
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, (list, tuple, set)):
            rule_targets.extend((str(item or "").strip(), label) for item in raw)
    # On the official bot a bare group openid is a valid target: the runtime
    # turns it into ``<instance>:GroupMessage:<openid>``. Only OneBot's bare
    # number keeps the "just use a group number" hint; a 16+ hex openid must
    # not be called malformed (v2.21.1 audit).
    configured_official = is_official_platform(
        (platform_instances or {}).get(platform_id, "")
    )

    def _looks_like_openid(value: str) -> bool:
        return len(value) >= 16 and all(ch in "0123456789abcdefABCDEF" for ch in value)

    for text, label in rule_targets:
        if not text or text.isdigit():
            continue
        if configured_official and _looks_like_openid(text):
            continue
        # AstrBot builds the session with ``MessageType(value)``, whose values
        # are case-sensitive. A hand-written "groupmessage" passed the regex
        # and then failed at send time (v2.21.1 audit).
        if target_pattern.match(text) and text.split(":", 2)[1] in (
            "GroupMessage",
            "FriendMessage",
            "OtherMessage",
        ):
            continue
        issues.append(
            AuditIssue(
                "warning",
                "target_format_suspect",
                f"{label}「{_safe_label(text)}」格式可疑：群聊可直接填群号，"
                "私聊填 default:FriendMessage:QQ号。",
                {"label": label, "target": _safe_label(text)},
            )
        )
    # Platform-instance mismatch: the first UMO segment must equal a live
    # platform id. Switching from aiocqhttp to the QQ official bot leaves the
    # old ``default`` targets in place, and every scheduled push then fails
    # with "no matching platform" (2026-11-13 实测：每日定时状态图发送失败).
    # Only warn when a push is actually configured: a user who only runs the
    # status-card command does not care about a stale alert platform id.
    push_enabled = (
        alerts_on
        or monitors_on
        or daily_on
        or weekly_on
        or daily_image_on
        or provider_watch_on
    )
    if platform_instances and push_enabled:
        if platform_id and platform_id not in platform_instances:
            instances = ", ".join(sorted(platform_instances))
            issues.append(
                AuditIssue(
                    "warning",
                    "platform_id_mismatch",
                    f"「告警平台 ID」当前为 {platform_id}，但 AstrBot 中没有这个平台实例"
                    f"（现有：{instances}）。定时状态卡、日报与告警会因找不到平台而发送失败。",
                    {
                        "platform_id": platform_id,
                        "instances": sorted(platform_instances),
                        # A ready-to-print string: the i18n template fills
                        # ``{instances_text}``; formatting the list itself
                        # printed ['a', 'b'] on the dashboard.
                        "instances_text": instances,
                    },
                )
            )
        numeric_hint = (
            "OneBot 群号；QQ 官方机器人的群 ID 是 openid，请在「状态卡诊断」里"
            "查看已知群并改用完整 UMO。"
        )
        for text, label in rule_targets:
            if not text:
                continue
            if ":" not in text:
                # A bare number is the documented way to target a group on
                # OneBot; on the official bot it is silently turned into
                # ``<instance>:GroupMessage:<number>`` and can never match a
                # group openid. This is the most common migration leftover.
                # Only warn when the configured instance really is official:
                # in a dual-platform deployment the same value is a perfectly
                # valid OneBot group (v2.21.1 audit).
                if text.isdigit() and configured_official:
                    issues.append(
                        AuditIssue(
                            "warning",
                            "official_group_id_numeric",
                            f"{label}「{_safe_label(text)}」是{numeric_hint}",
                            {"label": label, "target": _safe_label(text)},
                        )
                    )
                continue
            segments = text.split(":")
            if len(segments) != 3:
                continue
            prefix = segments[0]
            if prefix not in platform_instances:
                issues.append(
                    AuditIssue(
                        "warning",
                        "target_platform_missing",
                        f"{label}「{_safe_label(text)}」的平台前缀 {prefix} "
                        "不是当前运行的平台实例，发送会提示「未找到匹配平台」。",
                        {
                            "label": label,
                            "target": _safe_label(text),
                            "prefix": prefix,
                        },
                    )
                )
                continue
            tail = text.rsplit(":", 1)[-1]
            if (
                is_official_platform(platform_instances.get(prefix, ""))
                and segments[1] == "GroupMessage"
                and tail.isdigit()
            ):
                issues.append(
                    AuditIssue(
                        "warning",
                        "official_group_id_numeric",
                        f"{label}「{_safe_label(text)}」是{numeric_hint}"
                        "（若这是 QQ 频道目标可忽略本条）。",
                        {"label": label, "target": _safe_label(text)},
                    )
                )
    return issues
