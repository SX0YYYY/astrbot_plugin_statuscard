"""One startup pass that turns configuration mistakes into visible issues.

Most "I configured it but nothing happens" reports come from a handful of
quiet combinations: alerts enabled without a push target, monitor items
missing required fields, a weekly report with nowhere to go. The audit
collects them in one place so they can be logged once at start-up and
shown on the dashboard, instead of being discovered by silence.
"""

from __future__ import annotations

from dataclasses import dataclass
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
from .redactor import RULES as REDACT_RULES
from .utils import normalize_targets


@dataclass(frozen=True)
class AuditIssue:
    """A single configuration finding shown to the operator."""

    level: str  # "error" or "warning"
    code: str  # stable identifier the dashboard can key on
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


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


def _monitor_config_errors(config: Any) -> list[str]:
    """Names and reasons of monitor items that cannot be probed."""

    problems: list[str] = []
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
        name = str(item.get("name") or kind)
        if not item.get(required):
            problems.append(
                f"监测项「{name}」（{KIND_LABELS[kind]}）缺少 {required} 配置"
            )
        elif kind == "tcp":
            try:
                port = int(item.get("port") or 0)
            except (TypeError, ValueError):
                problems.append(
                    f"监测项「{name}」（TCP）端口不是数字（当前值 {item.get('port')!r}），无法探测"
                )
                continue
            if not (1 <= port <= 65535):
                problems.append(f"监测项「{name}」（TCP）端口 {port} 超出 1–65535")
    return problems


def run_audit(config: Any, *, monitor: Any = None) -> list[AuditIssue]:
    """Collect configuration findings; called once at plugin start-up.

    ``monitor`` (if given) supplies the already-normalized items so the
    audit and the probe loop cannot disagree about what is broken.
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
    resource_targets = resolve_targets(config, "resource")
    if resource_targets is None:
        resource_targets = alerts_targets
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
                "默认推送目标也为空），监测项不会探测也不会告警。"
                "请在「告警与每日定时状态图 → 默认推送目标」填写群号。",
            )
        )
    if monitors_on:
        for problem in (
            _monitor_config_errors(config)
            if monitor is None
            else [
                f"监测项「{item.get('name')}」（{KIND_LABELS.get(str(item.get('__template_key')), '?')}）"
                f"{item.get('config_error')}"
                for item in monitor.items
                if item.get("config_error")
            ]
        ):
            issues.append(AuditIssue("warning", "monitor_item_config", problem))
    if alerts_on and not resource_targets:
        issues.append(
            AuditIssue(
                "error",
                "alerts_no_targets",
                "资源与掉线告警已启用，但没有可用的推送目标（通知中心无 resource 规则，"
                "默认推送目标也为空），告警不会送达任何会话。"
                "请在「告警与每日定时状态图 → 默认推送目标」填写群号。",
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
                if cfg_float(config, key, 1.0) <= 0:
                    issues.append(
                        AuditIssue(
                            "warning",
                            "alert_threshold_zero",
                            f"告警阈值「{label}」为 0：该规则不生效（视为关闭）。"
                            "如需关闭请保持 0；如需告警请设为大于 0 的值。",
                        )
                    )
            except (TypeError, ValueError):
                continue
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
                            f"磁盘挂载点「{path_text}」不存在：磁盘使用率告警会静默跳过，"
                            "请检查「状态卡显示 → 磁盘挂载点」。",
                        )
                    )
    active_rules = notify_rules_active(config)
    if active_rules:
        issues.append(
            AuditIssue(
                "warning",
                "notify_center_active",
                f"通知中心已启用（{active_rules} 条有效规则）：规则覆盖的类型走规则目标，"
                "其余类型继续使用「默认推送目标」。这是正常状态，无需处理；"
                "只有当某类型收不到通知时才需要检查这里的覆盖关系。",
            )
        )
    else:
        raw_rules = cfg_get(config, "notify.rules", []) or []
        for rule in raw_rules:
            if not isinstance(rule, dict) or not cfg_bool(rule, "enabled", True):
                continue
            if not str(cfg_get(rule, "target", "") or "").strip() or not (
                cfg_get(rule, "kinds", []) or []
            ):
                issues.append(
                    AuditIssue(
                        "warning",
                        "notify_rule_invalid",
                        "通知中心存在未生效的规则：缺少推送目标或未勾选任何通知类型。",
                    )
                )
                break
    # The per-module target lists are legacy: they still work (and win over
    # the shared default), but they are hidden behind "show advanced" now.
    # Surface them so a user who forgot about them knows why that module
    # ignores the default target.
    legacy_scopes = []
    for key, label in (
        ("monitors.targets", "服务监测"),
        ("daily_report.targets", "日报/周报"),
    ):
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
                )
            )
    return issues
