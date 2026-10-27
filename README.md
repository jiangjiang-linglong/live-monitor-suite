# live-monitor · 小红书直播无人值守监控

对指定小红书直播间实现 **开播自动监控 → 原画录制 → 弹幕/在线人数/商品信号采集 → 下播自动交付到飞书** 的全链路无人值守。

下播后自动产出：飞书多维表（场次 + 在线人数/互动/爆品/商品讲解/商品热度榜/话术词 6 张明细表，含商品图）、飞书群卡片（蓝/黄/红/绿分级告警）、监控总结报告、在线人数曲线、整场逐字稿与音频、录像本地存档。

## 工作原理

```
小红书直播流(HEVC 非标 FLV)
        │  http://127.0.0.1:18080/live/<room_id>
        ▼
 relay/xhs_relay.py ── 本地转封装代理（标准 enhanced FLV，游客即可拉流）
        │
        ▼
 ffmpeg 30 分钟分段录制（.ts）
        │
        ▼
 monitor/xhs_live_pipeline.py
   ├─ WebSocket 弹幕（评论/点赞/关注/进店）
   ├─ 每 5 秒轮询在线人数/商品列表
   └─ 下播自动交付：多维表 / CSV / 报告 / 曲线 / 妙记转写 / 群卡片
        ▲
 monitor/xhs_watchdog.py（launchd 常驻守护）
   ├─ relay 守护、按 host_id 自动发现开播、拉起 ffmpeg+pipeline、崩溃重启
   ├─ 每 20 分钟真实签名探测登录态（失效红卡/恢复绿卡），同时保活并回写 cookie
   └─ 每 5 分钟磁盘检查：录像保留 24h 自动删除，80/90/92% 分级告警
```

## 目录结构

| 路径 | 说明 |
|---|---|
| `relay/xhs_relay.py` | 直播流转封装代理（监听 127.0.0.1:18080） |
| `monitor/apis/`、`monitor/xhs_utils/` | 小红书签名/登录/接口基础库（含 node 执行的 JS 签名） |
| `monitor/do_login.py` | 扫码登录，生成 login_cookies.txt |
| `monitor/xhs_watchdog.py` | 常驻守护主程序 |
| `monitor/xhs_live_pipeline.py` | 单场录制+采集+交付 |
| `monitor/xhs_minutes.py` | 妙记转写（可选，依赖 lark-cli） |
| `monitor/xhs_base_writer.py` | 多维表写入 |
| `monitor/cut_deliver.py` | 手动提前截止并交付 |
| `monitor/backfill_images.py` / `backfill_slicer_fields.py` | 历史场次补商品图 / 补切片器字段 |
| `monitor/watchdog_config.json` | 守护配置（目标房间、群、阈值） |
| `feishu/` | 飞书应用凭证与消息/文件发送器（真实 json 不入库） |
| `deploy/install.sh` | 一键部署脚本 |
| `deploy/com.causmatrix.xhs-watchdog.plist.template` | launchd 模板 |
| `docs/` | 运维说明、飞书自建应用配置指引 |
| `deliveries/` | 交付物与录像输出目录（运行时生成） |

## 部署（macOS）

### 1. 克隆

```bash
git clone https://gitee.com/michael_1226/live-monitor.git
cd live-monitor
```

### 2. 准备飞书凭证

按 `docs/飞书自建应用配置指引.md` 创建自建应用并开通多维表/消息/云文档权限，把 AppID、AppSecret、多维表 base_token 填入（install.sh 会生成模板）：

```
feishu/feishu_app.json
```

### 3. 一键安装

```bash
bash deploy/install.sh
```

脚本会：检查并安装 `node`（JS 签名运行时必需）与 `ffmpeg`（录像/转码必需）→ 创建 Python 虚拟环境并安装依赖 → 引导小红书 App 扫码登录 → 自检一轮 → 安装 launchd 常驻服务（开机自启 + 崩溃拉起）。

### 4. 配置监控目标

编辑 `monitor/watchdog_config.json`：

```json
"target": {
  "host_id": "主播的用户ID（稳定，用于从直播广场自动发现开播）",
  "name": "主播昵称",
  "room_id": "直播间ID（每场变化；可留空，靠 host_id 发现）"
}
```

重启守护：

```bash
launchctl kickstart -k gui/$(id -u)/com.causmatrix.xhs-watchdog
```

## 日常运维

| 操作 | 命令 |
|---|---|
| 查看状态 | `launchctl print gui/$(id -u)/com.causmatrix.xhs-watchdog \| grep -E "state\|pid"` |
| 重启 | `launchctl kickstart -k gui/$(id -u)/com.causmatrix.xhs-watchdog` |
| 停止 | `launchctl bootout gui/$(id -u)/com.causmatrix.xhs-watchdog` |
| 日志 | `tail -f monitor/logs/watchdog.launchd.log` |
| 一键自检 | `cd monitor && ./.venv/bin/python xhs_watchdog.py --once` |
| 只跑一次清理 | `cd monitor && ./.venv/bin/python xhs_watchdog.py --cleanup` |
| 重新扫码登录 | `cd monitor && ./.venv/bin/python do_login.py` |
| 提前截止并交付 | `cd monitor && ./.venv/bin/python cut_deliver.py <room_id> <host_id> <主播名> <输出目录> <webhook> <chat_id> <lark_cli>` |

完整机制（登录态保活、录像清理、告警分级、看板切片器、口径说明）见 `docs/无人值守运维说明-小红书监控.md`。

## 说明与限制

- **运行环境**：仅适配 macOS（守护用 launchd）；需 Python 3.10+、node、ffmpeg。
- **妙记转写可选**：整场逐字稿依赖 `lark-cli`（用户身份）。未安装 lark-cli 时其余交付全部正常，仅跳过转写。
- **凭证不入库**：`feishu/feishu_app.json`、`monitor/login_cookies.txt` 已被 `.gitignore` 排除，换机各自重新生成。
- **录像保留 24 小时**：`.ts/.mp4` 自动清理；CSV/报告/逐字稿/音频永久保留。
- 小红书非官方接口，存在账号风控风险；账号与房间按 1:1 使用，多账号多房间需另行改造。
