# 系统状态卡片（astrbot_plugin_statuscard）

以华丽的图片卡片展示 AstrBot 所在设备的运行状态，并支持主动告警推送。

## 功能

- **状态卡**：头像、Bot 昵称、AstrBot / NapCat 版本、运行时长、好友/群聊/群员/收发统计
- **概览卡**：系统、主机名、服务器运行时长、插件数
- **资源**：CPU / RAM / SWAP 三环形
- **磁盘**：挂载点进度条 + 磁盘 IO
- **网络**：上行/下行 SVG 折线图、实时速率、累计流量、本月流量
- **网站测试**：可配置目标，显示状态码与延迟
- **进程**：状态统计 + TOP 进程表（Name / Pid / CPU / MEM）
- **系统信息**：OS、Kernel、Boot、Python、Packages、Shell、CPU、GPU、Memory、Network、Arch、Disk、Platform
- **主动告警**：资源阈值、Bot 掉线/恢复、每日状态摘要、Komari Webhook 转发
- **服务监测**：类似 Uptime Kuma，支持 HTTP、TCP、Docker 容器、进程、systemd 服务，异常/恢复推送到 QQ 群

## 平台支持

本插件面向 **QQ / OneBot（aiocqhttp，NapCat）**。

好友数、群聊数、群员数、Bot 头像、NapCat 版本等数据都依赖 OneBot API；Telegram、Discord、飞书、钉钉、企业微信等平台没有做适配和验证，因此已从支持列表中移除。命令在其他平台上可能仍能被触发，但状态卡相关数据会缺失，不作为受支持平台。

## 安装 / 快速开始

### 环境要求

- AstrBot `>=4.26.7,<5`（已在 4.29.0 上验证）
- Python 3.10+
- 一个可用的 QQ / OneBot 连接（推荐 NapCat）
- 能访问 AstrBot 配置的 T2I 渲染服务（默认远程）

### 安装步骤

1. 将 `astrbot_plugin_statuscard` 目录放到 AstrBot 的 `data/plugins/` 下，或通过插件市场安装。
2. 安装依赖：`pip install -r requirements.txt`。部分 AstrBot 版本会自动处理插件依赖。
3. 重启 AstrBot，或在 WebUI 中重载插件。
4. 打开 WebUI 插件配置，至少确认：
   - `display.website_targets`（默认只测 Baidu）
   - `alerts.targets`（告警/每日摘要/监测的推送目标，纯群号或完整 UMO）
   - 可选：在 `avatar.avatar_upload_files` / `background.bg_upload_files` 中直接上传头像和背景图片
5. 由 AstrBot 管理员私聊发 `状态卡`，或在群聊发 `@bot 状态卡`。首次会先收到一条“生成中/隐私提示”，随后收到图片。非管理员触发会被权限拦截。

> 当前仓库为私有仓库；如果通过仓库地址安装失败，需要让仓库成员授权，或手动打包插件目录。

### 第一次使用检查

- 能收到“状态卡生成中”提示，说明命令链路正常；
- 能收到图片，说明采集 + T2I 都正常；
- 好友/群/群员显示 `--`，通常是 OneBot 未连接或查询超时；
- 出图很慢，通常是 T2I 服务排队，不是采集慢。

## 权限与隐私

- **所有命令仅 AstrBot 管理员可触发**：`状态卡`、`状态卡告警测试`、`状态卡告警状态`、`状态卡监测测试`、`状态卡监测状态` 都需要管理员权限；非管理员发送不会执行命令。
- `display.access`：在管理员范围内进一步收紧，默认 `admin`；`all` 表示所有管理员，`allowlist` 表示管理员 + 白名单（不能放行非管理员）。
- `display.allowlist`：可填群号、用户号或完整 UMO；仅 `allowlist` 模式生效。
- `display.redact`：可选 `hostname`、`process`、`network`、`qq_stats`，用于隐藏主机名、进程名、网络数据、好友/群/收发统计。
- `display.privacy_notice`：默认开启；每个会话首次出图前提示状态图会发送到 T2I 服务。
- 状态图 HTML 会发送到 AstrBot 配置的 T2I 服务（默认 `https://t2i.soulter.top/text2img`），其中包含主机名、进程名、磁盘、网络、头像/昵称等；用户上传的自定义背景/头像也会被上传。
- 如果不想把这些数据发往默认公共服务，可以设置自定义 `t2i_endpoint`，或把 `t2i_strategy` 改为本地渲染；服务器内存紧张时请先确认本地渲染依赖和资源占用。
- 插件还会访问 QQ 头像地址 `q1.qlogo.cn`，以及 `display.website_targets` 中配置的网站；这些请求不包含状态图的完整内容。
- `bg_lolicon_r18_type` 不要在有群友的群里开启；公开群建议保持 `0`。

## 命令

| 命令 | 说明 |
|---|---|
| `状态卡` / `/状态卡` / `状态卡片` / `sysstatus` / `系统状态` | 生成并发送状态图（仅管理员） |
| `状态卡告警测试`（管理员） | 向当前会话发送一条测试告警 |
| `状态卡告警状态`（管理员） | 查看告警配置与运行状态 |
| `状态卡监测状态`（管理员） | 查看服务监测目标与最近状态 |
| `状态卡监测测试`（管理员） | 立即执行一轮服务监测并返回结果（不发送告警） |

### 关于唤醒前缀

AstrBot 的命令触发方式受 `wake_prefix` 影响。常见的 `wake_prefix` 是 `["/"]`；如果你的配置是 `["*"]`、唤醒词或其他值，触发方式会不同：

- 私聊：通常直接发 `状态卡` 或 `/状态卡` 都可以；
- 群聊：通常需要 `@bot 状态卡` 或 `@bot /状态卡`；
- 插件额外注册了带斜杠的别名 `/状态卡`，不需要修改全局 `wake_prefix`。

## 数据口径

- **发**：读取 AstrBot 数据库 `platform_stats` 的累计值（AstrBot 只在发送路径累加该表）。
- **收**：读取 `astrbot_plugin_message_stats` 的群统计 JSON（只覆盖该插件跟踪的群），并在图上标注口径。
- **群员**：`get_group_list` 返回的各群 `member_count` 之和；同一用户跨群会重复计数。
- **月流量/网络折线**：插件后台每 60 秒采样一次 `psutil.net_io_counters`，状态保存在 `data/plugin_data/astrbot_plugin_statuscard/state.json`，重启不丢。
- **网络折线**：最近 60 个采样点（约 1 小时），纯 SVG 绘制，不依赖 JS。

## 配置重点

配置文件由 AstrBot 在首次加载时生成：`data/config/astrbot_plugin_statuscard_config.json`。

### 状态卡显示

- `display.website_targets`：网站测试目标，模板项支持 `name`、`url`、`timeout_seconds`（默认 3 秒）；默认只测 Baidu。
- `display.website_cache_seconds`：网站测试缓存，默认 60 秒；0 表示每次重新请求。
- `display.cooldown_seconds`：同一会话的状态卡冷却，默认 10 秒；0 表示关闭。同一会话已有一张卡在生成时，后续请求会直接提示“正在生成”，不会重复占用 T2I。
- `display.process_top_n`：进程表显示条数，默认 8；表按 CPU 降序，CPU 为采样区间平均，多核可超过 100%。
- `display.access` / `display.allowlist`：管理员内部的额外访问控制；所有命令本身已强制仅管理员，默认 `admin`。
- `display.redact`：脱敏项，可选 `hostname`、`process`、`network`、`qq_stats`。
- `display.privacy_notice`：每个会话首次出图前的隐私提示，默认开启。
- `display.progress_notice`：出图前的“生成中”提示，默认开启。
- `display.render_quality` / `display.render_timeout`：T2I 清晰度（`normal`/`high`/`ultra`）与超时（默认 180 秒）。

### 背景 / 头像

- `avatar.avatar_upload_files`：在 WebUI 插件配置页直接上传头像图片，取列表第一张生效；优先级高于本地路径、头像 URL 和 QQ 头像。
- `background.bg_upload_files`：在 WebUI 插件配置页直接上传背景图片，取列表第一张生效。优先级为：聊天中随命令发送的图片 > 上传的背景 > `bg_provider`。
- 上传文件保存在 `data/plugin_data/astrbot_plugin_statuscard/files/...`，单张上限 10 MB；删除时可在上传对话框中操作。
- `background.bg_provider` 默认 `none`（内置背景），可选 `local`、`loli`、`lolicon`。
- `avatar.avatar_text` 留空时自动读取 OneBot 登录昵称；头像默认使用 QQ 头像。

### 主动告警（默认关闭）

```json
"alerts": {
  "enabled": false,
  "targets": [],
  "interval_seconds": 60,
  "sustain_samples": 3,
  "cooldown_seconds": 1800,
  "cpu_threshold": 85,
  "mem_threshold": 90,
  "swap_threshold": 90,
  "disk_threshold": 85,
  "load_per_core_threshold": 2.0,
  "recover_notify": true,
  "offline_notify": true,
  "daily_summary": {"enabled": false, "time": "09:00", "with_image": true},
  "komari_webhook": {
    "enabled": false,
    "host": "127.0.0.1",
    "port": 9968,
    "path": "/webhook",
    "token": ""
  }
}
```

- `targets` 支持完整 UMO（`platform_id:GroupMessage:群号`）或纯群号；留空时不会主动发送。
- 资源告警需要连续 `sustain_samples` 次超过阈值才会发送；恢复后发送一次“已恢复”。
- 同一规则在 `cooldown_seconds` 内不会重复发送。
- 掉线告警依赖 OneBot `get_status`；如果 Bot 已离线，告警会排队到恢复后补发。
- 每日摘要由 `alerts.daily_summary.enabled` 独立控制，**不需要**打开 `alerts.enabled`；发送目标复用 `alerts.targets`。
- Komari Webhook 可独立于资源告警开启；默认只监听 `127.0.0.1`，避免暴露公网。
- 修改 `alerts.enabled`、`daily_summary.enabled` 或 `komari_webhook.enabled` 后，建议重启 AstrBot 或重载本插件；从关闭改为开启每日摘要时必须重载，任务才会创建。

### 服务监测（类似 Uptime Kuma）

```json
"monitors": {
  "enabled": true,
  "interval_seconds": 60,
  "fail_threshold": 2,
  "cooldown_seconds": 1800,
  "recover_notify": true,
  "targets": [],
  "items": [
    {"__template_key": "docker", "name": "NapCat", "container": "napcat", "timeout_seconds": 10, "enabled": true},
    {"__template_key": "systemd", "name": "Nginx", "unit": "nginx.service", "timeout_seconds": 10, "enabled": true},
    {"__template_key": "http", "name": "Uptime Kuma", "url": "http://127.0.0.1:3002", "expected_status": 0, "timeout_seconds": 5, "enabled": true},
    {"__template_key": "tcp", "name": "GsCore", "host": "127.0.0.1", "port": 8765, "timeout_seconds": 5, "enabled": true}
  ]
}
```

- `items` 是 `template_list`，每一项必须带 `__template_key`，可取 `http` / `tcp` / `docker` / `process` / `systemd`。
- `targets` 留空时复用 `alerts.targets`，即推送到同一个 QQ 群。
- 连续 `fail_threshold` 次失败才告警；恢复后发送一次“服务恢复”。
- HTTP 的 `expected_status` 为 `0` 时，任意 2xx/3xx 都算正常。
- 配置了 `keyword` 时最多读取 `monitors.max_response_bytes`（默认 262144 字节），避免误配大文件占满内存。
- Docker 检查需要运行 AstrBot 的系统用户有 Docker 权限；systemd 检查使用 `systemctl is-active`。
- AstrBot 自身无法监测“自己进程被杀”，因为进程不在时监测任务也不会运行；这种情况建议用外部 Uptime Kuma 或 Komari 监测 AstrBot。

### Komari Webhook 配置示例

Komari 后台 → 通知渠道 → Webhook：

- URL：`http://127.0.0.1:9968/webhook`
- Method：`POST`
- Content-Type：`application/json`
- Headers：若配置了 token，填 `{"Authorization":"Bearer <token>"}`
- Body：`{"title":"{{title}}","message":"{{message}}"}`

## 运行约束

- 插件内**不启动浏览器**；HTML 渲染交给 AstrBot 配置的 T2I 服务。默认使用远程 `https://t2i.soulter.top/text2img`。
- 后台采样只做 psutil 数字计算和 JSON 读写，频率 60 秒。
- 告警默认关闭，首次部署不会主动发送任何消息。

## 常见问题 / 排错

### 好友/群/群员显示 `--`

通常是 NapCat/OneBot 未连接，或 `get_friend_list`/`get_group_list` 超时。插件会每 30 秒重试失败字段；检查：

```bash
systemctl status napcat
journalctl -u astrbot -n 100 --no-pager | grep -i statuscard
```

### 出图很慢

状态图主要耗时在远程 T2I。可以在配置里把 `display.render_quality` 从 `ultra` 降到 `high` 或 `normal`，并保留 `display.progress_notice=true`。采集本身通常只需 1 秒左右。

### 每日摘要没有发

确认：

- `alerts.daily_summary.enabled=true`
- `alerts.daily_summary.time` 是 `HH:MM`
- `display.timezone` 是合法时区
- `alerts.targets` 不为空
- 日志：`journalctl -u astrbot --since "10 minutes ago" | grep 每日摘要`

### 告警/监测没有收到

- 阈值告警看 `alerts.enabled`；
- 服务监测看 `monitors.enabled` 和 `monitors.items`；
- 目标看 `alerts.targets` / `monitors.targets`，纯群号会按 `alerts.platform_id` 转换；
- 管理员可发 `状态卡告警测试`、`状态卡监测测试` 验证链路。

### 掉线/恢复告警为什么没收到

- 掉线检测依赖同一个 QQ/OneBot 通道。如果 AstrBot、NapCat 或 QQ 连接整体断开，告警本身也发不出去。
- 插件会在连接恢复后补发“离线 + 恢复”合并通知；如果 AstrBot 进程被重启/杀掉，插件不在运行，无法自己告警自己，请用外部 Komari/Uptime Kuma 监测 AstrBot。
- 连续 `alerts.sustain_samples` 次检测失败才触发（默认 3 次，约 3 分钟；服务器当前设为 2 次，约 2 分钟），更短的抖动不会告警。
- 如果只是短暂重启 NapCat，可能还没达到连续失败次数；可以在恢复后查看 `状态卡告警状态` 里的 `offline` 状态和日志 `journalctl -u astrbot | grep 离线`。

### WebChat/其他平台显示 `--`

本插件只支持 QQ/OneBot（aiocqhttp）。其他平台可以触发命令，但好友/群/群员/昵称等 OneBot 数据不可用，这是预期行为。

### 上传头像/背景后没有生效

- 确认在 WebUI 配置页保存后已重载插件；
- `avatar.avatar_upload_files` / `background.bg_upload_files` 取列表第一张，列表为空时会回退到原有逻辑；
- 上传文件位于 `data/plugin_data/astrbot_plugin_statuscard/files/...`，单张上限 10 MB；
- 头像优先级：上传头像 > 本地路径 > 头像 URL > QQ 头像；背景优先级：聊天中随命令发送的图片 > 上传背景 > `bg_provider`。

### 如何填写推送目标

完整 UMO 形如 `default:GroupMessage:群号`；也可以直接填纯群号。`platform_id` 是 AstrBot 平台实例 ID，默认通常是 `default`。填错时告警会静默失败，建议先用管理员测试命令验证。

### 如何限制谁能看状态卡

- `display.access=admin`：仅管理员；
- `display.access=allowlist` + `display.allowlist=[群号/用户号/UMO]`：管理员 + 白名单；
- `display.redact`：隐藏主机名、进程名、网络、QQ 统计等敏感项。

### 卸载和数据清理

删除插件目录、`data/config/astrbot_plugin_statuscard_config.json` 和 `data/plugin_data/astrbot_plugin_statuscard/` 即可。卸载前建议先备份 `state.json`。

## 许可证与署名

本项目基于 [astrbot_plugin_picstatus](https://github.com/exynos967/astrbot_plugin_picstatus)（作者：薄暝 / exynos967）二次开发。上游项目采用 AGPL-3.0 许可证。

本项目的修改与新增（状态卡布局、数据采集扩展、告警引擎、Komari Webhook 等）由 烬辞 完成，同样以 **AGPL-3.0** 发布。

详见 [LICENSE](LICENSE) 与 [ATTRIBUTION.md](ATTRIBUTION.md)。再分发时请保留上游署名与完整源代码。

## 目录

```
main.py            插件入口、命令、渲染与发送
collectors.py      psutil / OneBot / 数据库采集
sampler.py         网络、磁盘 IO、接收统计采样器
alerts.py          阈值告警、掉线恢复、每日摘要、Komari Webhook
monitor.py         Uptime Kuma 式服务监测（HTTP/TCP/Docker/进程/systemd）
state.py           JSON 状态存储（原子写入）
t2i_renderer.py    Jinja2 → 单文件 HTML（CSS/图片内联）
templates/         模板与 CSS
res/assets/        内置背景与默认头像
```
