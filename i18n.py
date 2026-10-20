from __future__ import annotations

from typing import Any

from .collectors import cfg_get

DEFAULT_LANGUAGE = "zh-CN"
LANGUAGES = ("zh-CN", "en-US")

MESSAGES: dict[str, dict[str, str]] = {
    "zh-CN": {
        "platform_unsupported": "当前平台不受支持：状态卡仅支持 QQ/OneBot（aiocqhttp）。",
        "admin_only": "查看状态仅管理员可用。",
        "generating": "状态卡正在生成，请稍候。",
        "cooldown": "状态卡冷却中，请 {seconds} 秒后再试。",
        "privacy_notice": "提示：状态图包含主机名/进程等信息，并会发送到 T2I 服务渲染。",
        "progress_notice": "状态卡生成中，通常需要 1–3 分钟，请稍候…",
        "image_failed": "生成状态图失败，以下为文本摘要：\n{summary}",
        "summary_failed": "获取系统状态失败，请检查 AstrBot 后台日志。",
        "alert_disabled": "告警推送未启用。请先在配置页打开“启用资源与掉线告警”。",
        "test_sent": "测试通知已发送到当前会话。",
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
        "report_preview_send_failed": "日报预览发送失败，请查看后台日志。",
        "todo_empty": "📝 暂无未完成待办。\n用法：待办 添加 <内容> / 待办 完成 <编号> / 待办 删除 <编号>",
        "todo_list_title": "📝 待办（{count} 项未完成）",
        "todo_usage": "用法：待办 添加 <内容> / 待办 完成 <编号> / 待办 删除 <编号> / 待办 全部",
        "todo_all_empty": "📝 暂无待办记录。",
        "todo_all_title": "📝 全部待办",
        "todo_add_usage": "用法：待办 添加 <内容>",
        "todo_added": "✅ 已添加待办 #{id}：{content}",
        "todo_add_failed": "添加失败，请查看后台日志。",
        "todo_complete_usage": "用法：待办 完成 <编号>",
        "todo_completed": "✅ 已完成待办 #{id}。",
        "todo_not_found_pending": "未找到未完成的待办 #{id}。",
        "todo_delete_usage": "用法：待办 删除 <编号>",
        "todo_deleted": "🗑️ 已删除待办 #{id}。",
        "todo_not_found": "未找到待办 #{id}。",
        "todo_help": (
            "📝 待办用法\n"
            "待办 / 待办 列表：查看未完成\n"
            "待办 全部：查看全部\n"
            "待办 添加 <内容>：新增\n"
            "待办 完成 <编号>：标记完成\n"
            "待办 删除 <编号>：删除"
        ),
    },
    "en-US": {
        "platform_unsupported": "Unsupported platform: the status card only supports QQ/OneBot (aiocqhttp).",
        "admin_only": "Only AstrBot administrators can use this command.",
        "generating": "The status card is being generated, please wait.",
        "cooldown": "Status card cooldown, try again in {seconds} seconds.",
        "privacy_notice": "Note: the status image includes the hostname/processes and is sent to the configured T2I service for rendering.",
        "progress_notice": "Generating the status card; this usually takes 1–3 minutes…",
        "image_failed": "Failed to generate the status image. Text summary:\n{summary}",
        "summary_failed": "Failed to collect system status; check the AstrBot backend log.",
        "alert_disabled": "Alert delivery is disabled. Enable resource/offline alerts in the plugin settings first.",
        "test_sent": "Test notification sent to this chat.",
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
        "report_preview_send_failed": "Failed to send the report preview; check the backend log.",
        "todo_empty": "📝 No open to-dos.\nUsage: todo add <content> / todo complete <id> / todo delete <id>",
        "todo_list_title": "📝 To-dos ({count} open)",
        "todo_usage": "Usage: todo add <content> / todo complete <id> / todo delete <id> / todo all",
        "todo_all_empty": "📝 No to-dos yet.",
        "todo_all_title": "📝 All to-dos",
        "todo_add_usage": "Usage: todo add <content>",
        "todo_added": "✅ Added to-do #{id}: {content}",
        "todo_add_failed": "Failed to add; check the backend log.",
        "todo_complete_usage": "Usage: todo complete <id>",
        "todo_completed": "✅ Completed to-do #{id}.",
        "todo_not_found_pending": "No open to-do found for #{id}.",
        "todo_delete_usage": "Usage: todo delete <id>",
        "todo_deleted": "🗑️ Deleted to-do #{id}.",
        "todo_not_found": "To-do #{id} not found.",
        "todo_help": (
            "📝 To-do usage\n"
            "todo / todo list: show open items\n"
            "todo all: show all items\n"
            "todo add <content>: add an item\n"
            "todo complete <id>: mark done\n"
            "todo delete <id>: delete an item"
        ),
    },
}


def get_language(config: Any = None) -> str:
    value = str(
        cfg_get(config, "display.language", DEFAULT_LANGUAGE)
        or DEFAULT_LANGUAGE
    ).strip()
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


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
