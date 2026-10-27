# live-monitor · 直播无人值守监控（小红书 + 抖音）

一个仓库、**两个平台各自独立目录、两套互相独立的服务**：

| 目录 | 平台 | 能力 | 运行形态 |
|---|---|---|---|
| **`xhs/`** | 小红书 | 原画录制、实时弹幕/在线人数/商品信号采集、下播自动交付飞书多维表（6 张明细表+商品图）、群卡片、报告、人数曲线、逐字稿；登录态保活告警、磁盘监控与录像 24h 清理 | watchdog 常驻（守护 relay/ffmpeg/pipeline） |
| **`douyin/`** | 抖音（及快手/B站/TikTok 等多平台，录播器原生支持） | 多房间原画录制、飞书群开播/下播通知（同一场只推一次）、离线妙记转写+爆品报告+切片后处理 | 录播器常驻轮询 |

两套服务**进程独立、独立重启、互不影响**：抖音侧是开源录播器（只录制，不写多维表），小红书侧是自研实时链路。凭证、配置、录像、日志各自独立。

## 关于 Docker（仓库里有什么）

仓库只保存**源码 + Dockerfile + compose 配置**，不含镜像本体。目标机器上执行 `docker compose up -d --build` 时才会：
1. 拉取 `python:3.11-slim` 基础镜像；
2. 容器内 apt 安装 ffmpeg、node，pip 安装依赖（Dockerfile 已配阿里云/清华源）；
3. 启动服务。

镜像在目标机器上构建一次即可，之后离线运行。凭证、cookie、录像、日志全部通过 `data/` 目录挂载在宿主机，容器删除不丢数据。

## 目录结构

```
live-monitor/
├── docker-compose.yml      # 双服务编排（xhs-monitor + douyin-recorder）
├── data/                   # 【运行时】凭证、cookie、录像、日志全部在这里
│   ├── xhs/
│   └── douyin/
├── docs/                   # 共享文档（飞书自建应用配置指引）
├── xhs/                    # 小红书服务
│   ├── Dockerfile
│   ├── monitor/            # 守护/采集/写入（apis、xhs_utils 签名库、watchdog、pipeline 等）
│   ├── relay/              # HEVC 转封装代理（watchdog 自管）
│   ├── feishu/             # 飞书应用发送器（真实 json 不入库）
│   ├── deliveries/         # 交付物与录像输出
│   ├── deploy/             # macOS launchd 一键安装
│   └── docs/               # 小红书运维说明
└── douyin/                 # 抖音服务
    ├── Dockerfile
    ├── main.py / spider.py / msg_push.py   # 开源录播器 + 飞书推送/小红书适配
    ├── postprocess/        # 离线转写/爆品报告/切片（可选）
    ├── config/*.example    # 配置模板（真实 config.ini 含 cookie，不入库）
    └── deploy/             # macOS launchd 模板
```

## 部署方式一：Docker（推荐，Mac/Linux 通用）

前提：装好 Docker（Mac 为 Docker Desktop / OrbStack，Linux 为 Docker Engine + compose 插件）。

```bash
git clone https://gitee.com/michael_1226/live-monitor.git
cd live-monitor

# —— 小红书 ——
cp data/xhs/feishu_app.example.json data/xhs/feishu_app.json
#   填入 app_id / app_secret / base_token（见 docs/飞书自建应用配置指引.md）
docker compose build xhs-monitor
docker compose run --rm --workdir /data/xhs xhs-monitor python /app/monitor/do_login.py
#   打开 data/xhs/login_qr.png 用小红书 App 扫码（可能含风控验证两步）
#   编辑 xhs/monitor/watchdog_config.json 填监控目标（host_id 或 room_id）

# —— 抖音 ——
docker compose build douyin-recorder
docker compose run --rm --entrypoint bash douyin-recorder -c "mkdir -p /app/config && cp /opt/config-examples/*.example /app/config/ && cp /app/config/config.ini.example /app/config/config.ini && cp /app/config/URL_config.ini.example /app/config/URL_config.ini"
#   编辑 data/douyin/config/config.ini 填抖音 cookie（录制抖音必须）
#   编辑 data/douyin/config/URL_config.ini 填房间链接

# —— 启动（两个服务）——
docker compose up -d
docker compose ps
```

> 首次 `up` 时若 `data/douyin/config` 为空，容器入口会自动从模板复制 config.ini / URL_config.ini。

常用命令：

```bash
docker compose logs -f xhs-monitor       # 看日志
docker compose restart xhs-monitor       # 改配置后重启
docker compose up -d douyin-recorder     # 只起抖音
docker compose down                      # 停止（data/ 里的数据不丢）
```

说明：
- 抖音离线后处理（转写/报告/切片）在容器内手动执行，用法见 `douyin/postprocess/README_离线转写与切片.md`，依赖 lark-cli，容器内没有则跳过。
- 小红书妙记转写同理，容器内无 lark-cli 时自动跳过，其余交付不受影响。

## 部署方式二：macOS launchd（不用 Docker）

### 小红书

```bash
bash xhs/deploy/install.sh
```

自动安装 node/ffmpeg（Homebrew）、建 venv、引导扫码、自检并安装 launchd 常驻。

### 抖音

```bash
cd douyin
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp config/config.ini.example config/config.ini          # 填抖音 cookie
cp config/URL_config.ini.example config/URL_config.ini  # 填房间
# 装为 launchd 常驻：
sed "s#__DOUYIN_DIR__#$(pwd)#g" deploy/com.causmatrix.douyin-recorder.plist.template \
  > ~/Library/LaunchAgents/com.causmatrix.douyin-recorder.plist
launchctl load ~/Library/LaunchAgents/com.causmatrix.douyin-recorder.plist
```

两个服务互不依赖，分别起停。

## 日常运维（小红书服务）

| 操作 | 命令 |
|---|---|
| 状态 | `launchctl print gui/$(id -u)/com.causmatrix.xhs-watchdog \| grep state`（Docker 用 `docker compose ps`） |
| 重启 | `launchctl kickstart -k gui/$(id -u)/com.causmatrix.xhs-watchdog` |
| 日志 | `tail -f xhs/monitor/logs/watchdog.launchd.log` |
| 自检 | `cd xhs/monitor && ./.venv/bin/python xhs_watchdog.py --once` |
| 重新扫码 | `cd xhs/monitor && ./.venv/bin/python do_login.py` |
| 提前截止交付 | `cd xhs/monitor && ./.venv/bin/python cut_deliver.py <room_id> <host_id> <主播名> <输出目录> <webhook> <chat_id> <lark_cli>` |

机制细节（登录态保活、录像清理、告警分级、看板切片器、口径说明）见 `xhs/docs/无人值守运维说明-小红书监控.md`。

## 说明与限制

- **凭证不入库**：`xhs/feishu/feishu_app.json`、`xhs/monitor/login_cookies.txt`、抖音 `config.ini`（含 cookie）均已被 `.gitignore` 排除。
- **抖音录制必须提供 cookie**：从浏览器登录 live.douyin.com 后复制 Cookie 到 `config/config.ini`；小红书实时链路游客即可拉流，但信号采集需要登录态扫码。
- **录像保留**：小红书服务 24h 自动清理 `.ts/.mp4`；抖音服务录像保留在 `data/douyin/downloads`（自行清理）。
- **不要用抖音录播器同时录小红书房间**（会与 xhs 服务重复录制）；小红书统一走 xhs-monitor。
- 均为非官方接口，存在账号风控风险；多账号多房间/代理方案需另行改造。
