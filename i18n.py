from __future__ import annotations

from typing import Any

from astrbot.api import logger

from .collectors import cfg_get

DEFAULT_LANGUAGE = "zh-CN"
LANGUAGES = ("zh-CN", "en-US")

MESSAGES: dict[str, dict[str, str]] = {
    "zh-CN": {
        "platform_unsupported": "当前平台不受支持：状态卡仅支持 QQ/OneBot（aiocqhttp）。",
        "generating": "状态卡正在生成，请稍候。",
        "cooldown": "状态卡冷却中，请 {seconds} 秒后再试。",
        "privacy_notice": "提示：状态图包含主机名/进程等信息，并会发送到 T2I 服务渲染。",
        "progress_notice": "状态卡生成中，通常十几秒到一分钟，请稍候…",
        "image_failed": "生成状态图失败（渲染服务 T2I 暂时不可用，稍后重试通常可恢复），以下为文本摘要：\n{summary}",
        "summary_failed": "获取系统状态失败，请检查 AstrBot 后台日志。",
        "test_sent": "测试通知已发送（当前会话 + 实际推送目标，重复目标已去重）。",
        "test_failed": "测试通知发送失败，请查看后台日志。",
        "monitor_no_targets": "没有配置监测目标。请在配置页的“服务可用性监测”中添加。",
        "monitor_result_title": "🩺 服务检测结果",
        "monitor_result_summary": "✅ 正常 {ok} 项 · ❌ 异常 {failed} 项",
        "monitor_result_item": "❌ {name}（{kind}）\n   {detail}",
        "monitor_all_ok": "✅ 全部正常。",
        "report_no_session": "无法确定当前会话，日报预览未发送。",
        "report_preview_failed": "日报预览生成失败，请查看后台日志。",
        "report_preview_sent": "日报预览已发送。",
        "report_preview_sent_text": "日报预览已发送（文字）。",
        "report_preview_unconfirmed": "日报预览已发送（回执未确认，可能未送达）。",
        "report_preview_send_failed": "日报预览发送失败，请查看后台日志。",
        "weekly_preview_failed": "周报预览生成失败，请查看后台日志。",
        "weekly_preview_sent": "周报预览已发送。",
        "weekly_preview_sent_text": "周报预览已发送（文字）。",
        "weekly_preview_unconfirmed": "周报预览已发送（回执未确认，可能未送达）。",
        "weekly_preview_send_failed": "周报预览发送失败，请查看后台日志。",
        "report_generating": "日报生成中，通常十几秒到一分钟，请稍候…",
        "weekly_generating": "周报生成中，通常十几秒到一分钟，请稍候…",
        "report_retry_give_up": "⚠️ 日报（{day}）连续 3 次发送失败，已停止重试；可发送「预览日报」手动补发。",
        "report_db_unavailable": "⚠️ 日报数据库暂时不可用，本次日报未发送（{day}）。请检查后台日志；数据恢复后下一次定时发送会继续。",
        "report_db_unavailable_preview": "⚠️ 报表数据库暂时不可用，无法生成预览。请检查后台日志；数据恢复后再试。",
        "weekly_retry_give_up": "⚠️ 周报连续 3 次发送失败，已停止重试；可发送「预览周报」手动补发。",
        "summary_host": "主机",
        "summary_disk": "磁盘",
        "summary_network": "网络",
        "summary_month": "本月流量",
        "summary_process": "进程",
        "summary_friends": "好友",
        "summary_groups": "群聊",
        "summary_members": "群员",
        "summary_recv": "收发",
    },
    "en-US": {
        "platform_unsupported": "Unsupported platform: the status card only supports QQ/OneBot (aiocqhttp).",
        "generating": "The status card is being generated, please wait.",
        "cooldown": "Status card cooldown, try again in {seconds} seconds.",
        "privacy_notice": "Note: the status image includes the hostname/processes and is sent to the configured T2I service for rendering.",
        "progress_notice": "Generating the status card; this usually takes seconds to a minute…",
        "image_failed": "Failed to generate the status image (the T2I rendering service is temporarily unavailable; retrying later usually works). Text summary:\n{summary}",
        "summary_failed": "Failed to collect system status; check the AstrBot backend log.",
        "test_sent": "Test notification sent (this chat plus the effective push targets, duplicates removed).",
        "test_failed": "Test notification failed; check the backend log.",
        "monitor_no_targets": "No monitor targets configured. Add them under Service Monitoring in the plugin settings.",
        "monitor_result_title": "🩺 Service check results",
        "monitor_result_summary": "✅ OK {ok} · ❌ Failed {failed}",
        "monitor_result_item": "❌ {name} ({kind})\n   {detail}",
        "monitor_all_ok": "✅ All targets are healthy.",
        "report_no_session": "Cannot determine the current chat; report preview was not sent.",
        "report_preview_failed": "Failed to build the report preview; check the backend log.",
        "report_preview_sent": "Report preview sent.",
        "report_preview_sent_text": "Report preview sent as text.",
        "report_preview_unconfirmed": "Report preview sent (delivery unconfirmed).",
        "report_preview_send_failed": "Failed to send the report preview; check the backend log.",
        "weekly_preview_failed": "Failed to build the weekly report preview; check the backend log.",
        "weekly_preview_sent": "Weekly report preview sent.",
        "weekly_preview_sent_text": "Weekly report preview sent as text.",
        "weekly_preview_unconfirmed": "Weekly report preview sent (delivery unconfirmed).",
        "weekly_preview_send_failed": "Failed to send the weekly report preview; check the backend log.",
        "report_generating": "Generating the daily report; this usually takes seconds to a minute…",
        "weekly_generating": "Generating the weekly report; this usually takes seconds to a minute…",
        "report_retry_give_up": "⚠️ The daily report ({day}) failed to send after 3 attempts; use the preview command（预览日报）to resend manually.",
        "report_db_unavailable": "⚠️ The report database is temporarily unavailable, so the daily report was not sent ({day}). Check the backend log; the next scheduled run will continue after recovery.",
        "report_db_unavailable_preview": "⚠️ The report database is temporarily unavailable, so the preview cannot be generated. Check the backend log and try again after it recovers.",
        "weekly_retry_give_up": "⚠️ The weekly report failed to send after 3 attempts; use the preview command（预览周报）to resend manually.",
        "summary_host": "Host",
        "summary_disk": "Disk",
        "summary_network": "Network",
        "summary_month": "Month traffic",
        "summary_process": "Processes",
        "summary_friends": "Friends",
        "summary_groups": "Groups",
        "summary_members": "Members",
        "summary_recv": "Sent/Recv",
    },
}


def get_language(config: Any = None) -> str:
    value = str(
        cfg_get(config, "display.language", DEFAULT_LANGUAGE) or DEFAULT_LANGUAGE
    )
    normalized = value.strip().lower().replace("_", "-")
    for lang in LANGUAGES:
        if normalized == lang.lower():
            return lang
    if value.strip():
        logger.warning(
            "display.language 值 %r 不是 %s 之一，已回退 %s",
            value,
            "/".join(LANGUAGES),
            DEFAULT_LANGUAGE,
        )
    return DEFAULT_LANGUAGE


def tr(config: Any, key: str, **kwargs: Any) -> str:
    language = get_language(config)
    table = MESSAGES.get(language) or MESSAGES[DEFAULT_LANGUAGE]
    text = table.get(key) or MESSAGES[DEFAULT_LANGUAGE].get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text
