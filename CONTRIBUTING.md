# 开发与测试

面向想改代码或跑测试的贡献者。普通使用请看 [README.md](https://github.com/SX0YYYY/astrbot_plugin_statuscard/blob/main/README.md)。

## 运行测试

测试使用 pytest，不依赖 AstrBot 运行时：

```bash
cd astrbot_plugin_statuscard
python -m pytest -q
```

当前测试预期为 `254 passed`。GitHub Actions 工作流见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)，在 push 和 pull request 时安装依赖并运行同一套测试，并校验 `version.py`、`metadata.yaml` 与 README badge 三处的版本号一致。

## 代码结构

| 模块 | 职责 |
|---|---|
| `main.py` | 命令、渲染入口、插件生命周期 |
| `collectors.py` | psutil / OneBot / 数据库采集 |
| `redactor.py` | **脱敏规则的唯一定义**：状态卡、日报、Web 看板共用 |
| `delivery.py` | **投递服务**：发送、重试判定、回执记录、待发队列 |
| `onebot_receipt.py` | OneBot 协议回执解析与「能否安全重发」判定 |
| `alerts.py` | 资源阈值、掉线恢复、每日定时状态图、Komari Webhook |
| `audit.py` | 配置体检：阈值 0、缺目标、脱敏规则拼错、监测项无效等（启动与看板提示） |
| `monitor.py` | 服务可用性监测（HTTP/TCP/Docker/进程/systemd） |
| `report.py` / `report_view.py` / `report_store.py` | 日报的数据采集、聚合与存储 |
| `page_api.py` | Web 看板的 5 个 API（基于官方 `astrbot.api.web`） |
| `sampler.py` / `state.py` | 后台采样与线程安全的本地状态存储 |
| `t2i_renderer.py` | Jinja2 → 单文件 HTML（CSS/图片内联） |
| `local_card.py` | T2I 不可用时用 Pillow 绘制的本地降级卡 |
| `i18n.py` | QQ 命令提示文案（中英） |
| `utils.py` | 通用工具：配置读取、URL 脱敏、文本截断 |

## 代码风格

项目使用 [ruff](https://docs.astral.sh/ruff/) 做格式化和静态检查，配置见 `ruff.toml`：

```bash
python -m ruff format .        # 格式化
python -m ruff check .         # 静态检查
```

`ruff.toml` 里显式豁免了 `BLE001`（捕获 `Exception`）和 `S110`（`try-except-pass`）两条规则：**插件的多个后台循环（采样器、服务监测、告警引擎、日报采集）必须保证单次探测失败不会拖垮整个循环**，这是刻意的容错策略，不是疏忽。豁免的每处宽泛捕获都应当记录日志或由调用方检查结果；`S112`（`try-except-continue` 静默吞错）不在豁免范围内，已全部补上 `logger.debug`。

## 本地验证建议

端到端回归（真实采集 + HTML 渲染）与 T2I 冒烟脚本放在维护者的开发工作区里，不随仓库发布。提交 PR 前跑通 `pytest` 与 `ruff check` / `ruff format --check` 即可；如果改动涉及 AstrBot 兼容性，请在 4.26.7 / 4.27.0 / 4.29.x 上至少验证插件能正常加载。

## 提交与发版约定

- 版本号三处必须一致：`version.py`、`metadata.yaml`、README 顶部 badge。
- 用户可见的行为变化要写进 `CHANGELOG.md`，并在版本段落开头给一句面向用户的摘要。
- 部署与发布由维护者在本地工作区完成（部署用整目录替换并自动备份，发布说明自动取自 CHANGELOG）；贡献者只需保证测试通过、版本一致。
