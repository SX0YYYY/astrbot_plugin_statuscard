# 安全策略

## 支持版本

| 版本 | 支持 |
|---|---|
| 2.19.x – 2.20.x | ✅ |
| 2.18.x | ✅ |
| 2.16 – 2.17 | ⚠️ 仅关键修复 |
| < 2.16 | ❌ |

## 报告问题

有问题请直接提交 issue；代码贡献欢迎提交 PR。

报告中请说明：

- 受影响的插件版本和 AstrBot 版本
- 复现步骤和影响范围
- 是否已在真实环境验证
- 如果涉及密钥或隐私数据，请先脱敏

## 范围

- 插件代码、配置 schema、插件页面 API、Komari Webhook
- 不包含 AstrBot 本体、NapCat、QQ 平台自身的问题

## 已知边界

- 插件只支持 `aiocqhttp`（QQ / OneBot / NapCat）
- Komari Webhook 默认只监听 `127.0.0.1`
- 主动推送依赖 AstrBot 平台适配器；QQ 侧静默吞消息无法从插件侧确认
- `state.json` 的 `delivery` 与 `delivery_real` 会记录最近一次推送的目标和来源（`delivery_real` 只记录真实发出的通知），文件权限为 600
