# 安全策略

## 支持版本

| 版本 | 支持 |
|---|---|
| 2.12.x | ✅ |
| 2.10 – 2.11 | ⚠️ 仅关键修复 |
| < 2.10 | ❌ |

## 报告漏洞

请不要在公开 issue 中披露可被利用的细节。漏洞请走 GitHub 私有漏洞报告：在仓库 **Security** 标签页使用 **“Report a vulnerability”**（内容仅维护者可见）；修复版本发布后会通过 Security Advisory 公开详情与致谢。

一般问题、使用疑问和功能建议请提交 issue；代码贡献欢迎提交 PR。

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
- `state.json` 的 `delivery` 会记录最近一次推送的目标和来源，文件权限为 600
