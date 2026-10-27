"""One startup pass that turns configuration mistakes into visible issues.

Most "I configured it but nothing happens" reports come from a handful of
quiet combinations: alerts enabled without a push target, monitor items
missing required fields, a weekly report with nowhere to go. The audit
collects them in one place so they can be logged once at start-up and
shown on the dashboard, instead of being discovered by silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .collectors import cfg_bool, cfg_float, cfg_get
from .monitor import KIND_LABELS, REQUIRED_FIELDS
from .utils import normalize_targets


@dataclass(frozen=True)
class AuditIssue:
    """A single configuration finding shown to the operator."""

    level: str  # "error" or "warning"
    code: str  # stable identifier the dashboard can key on
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


def _effective_targets(config: Any, key: str, platform_id: str) -> list[str]:
    """Resolve a target list that falls back to ``alerts.targets`` when empty."""

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
    monitor_targets = _effective_targets(config, "monitors.targets", platform_id)
    report_targets = _effective_targets(config, "daily_report.targets", platform_id)

    if monitors_on and not monitor_targets:
        issues.append(
            AuditIssue(
                "error",
                "monitors_no_targets",
                "服务监测已启用，但推送目标为空（monitors.targets 与 alerts.targets "
                "均未填写），监测项不会探测也不会告警。请在「服务可用性监测 → 监测推送目标」"
                "或「告警与每日定时状态图 → 推送目标」填写群号。",
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
    if alerts_on and not alerts_targets:
        issues.append(
            AuditIssue(
                "error",
                "alerts_no_targets",
                "资源与掉线告警已启用，但推送目标为空，告警不会送达任何会话。"
                "请在「告警与每日定时状态图 → 推送目标」填写群号。",
            )
        )
    if daily_on and not report_targets:
        issues.append(
            AuditIssue(
                "warning",
                "daily_report_no_targets",
                "每日日报已启用，但推送目标为空（daily_report.targets 与 alerts.targets "
                "均未填写），日报不会发送。",
            )
        )
    if weekly_on and not report_targets:
        issues.append(
            AuditIssue(
                "warning",
                "weekly_report_no_targets",
                "每周周报已启用，但推送目标为空（与每日日报共用），周报不会发送。",
            )
        )
    if cfg_bool(config, "alerts.daily_summary.enabled", False) and not alerts_targets:
        issues.append(
            AuditIssue(
                "warning",
                "daily_summary_no_targets",
                "每日定时状态图已启用，但推送目标为空（与资源告警共用 alerts.targets），"
                "到点后会静默跳过且当天不重试。",
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
    return issues
