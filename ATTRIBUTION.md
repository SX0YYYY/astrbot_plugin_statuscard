# Attribution / 署名

本项目的状态卡模板、渲染管线与部分系统采集代码基于以下开源项目二次开发：

- **项目**：astrbot_plugin_picstatus
- **作者**：薄暝（exynos967）
- **仓库**：https://github.com/exynos967/astrbot_plugin_picstatus
- **许可证**：GNU Affero General Public License v3.0（AGPL-3.0）

上游代码的版权归原作者所有。

## 本仓库的主要修改

- 重写状态卡模板与 CSS，补齐概览、网络折线、进程统计、系统信息等模块
- 扩展数据采集：OneBot 好友/群/群员、收发统计、月流量、磁盘 IO
- 新增状态采样器与 JSON 状态持久化
- 新增主动告警引擎：资源阈值、掉线/恢复、每日摘要、Komari Webhook
- 新增管理员诊断命令

## 本仓库版权

本仓库的修改与新增部分：

- Copyright (C) 2026 烬辞
- 许可证：AGPL-3.0

再分发本项目时，请保留本文件、`LICENSE` 以及上游署名，并按 AGPL-3.0 的要求提供完整源代码。
