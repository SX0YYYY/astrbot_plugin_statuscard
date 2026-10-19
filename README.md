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

## 命令

| 命令 | 说明 |
|---|---|
| `状态卡` / `/状态卡` / `状态卡片` / `sysstatus` / `系统状态` | 生成并发送状态图 |
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

### 背景 / 头像

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
- 每日摘要需要同时开启 `alerts.enabled` 和 `alerts.daily_summary.enabled`。
- Komari Webhook 可独立于资源告警开启；默认只监听 `127.0.0.1`，避免暴露公网。
- 修改 `alerts.enabled`、`daily_summary.enabled` 或 `komari_webhook.enabled` 后，需要重启 AstrBot 或重载本插件才会生效。

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

- 插件内**不启动浏览器**；HTML 渲染交给 AstrBot 的远程 T2I 服务。
- 后台采样只做 psutil 数字计算和 JSON 读写，频率 60 秒。
- 告警默认关闭，首次部署不会主动发送任何消息。

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
