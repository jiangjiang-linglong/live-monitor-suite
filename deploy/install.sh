#!/bin/bash
# =====================================================================
# 小红书直播监控 · 一键部署（macOS）
# 用法：在仓库根目录执行  bash deploy/install.sh
# 完成：依赖检查/安装 -> Python 虚拟环境 -> 凭证模板 -> launchd 常驻
# =====================================================================
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_DIR="$REPO_DIR/monitor"
PLIST="$HOME/Library/LaunchAgents/com.causmatrix.xhs-watchdog.plist"

echo "==> 仓库目录：$REPO_DIR"

# ---------- 1. 系统依赖：node / ffmpeg / python3 ----------
need_brew=""
command -v node >/dev/null 2>&1 || need_brew="$need_brew node"
command -v ffmpeg >/dev/null 2>&1 || need_brew="$need_brew ffmpeg"
if [ -n "$need_brew" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "==> 用 Homebrew 安装:$need_brew"
    brew install $need_brew
  else
    echo "缺少:$need_brew"
    echo "请先安装 Homebrew（https://brew.sh）后重跑本脚本，或自行安装 node 与 ffmpeg。"
    exit 1
  fi
fi
PY="$(command -v python3)"
echo "==> python3: $PY / node: $(node -v) / ffmpeg: $(ffmpeg -version | head -1 | awk '{print $3}')"

# ---------- 2. Python 虚拟环境 + 依赖 ----------
cd "$MONITOR_DIR"
if [ ! -d ".venv" ]; then
  echo "==> 创建虚拟环境 .venv"
  python3 -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install -r "$REPO_DIR/requirements.txt"

# ---------- 3. 飞书凭证 ----------
if [ ! -f "$REPO_DIR/feishu/feishu_app.json" ]; then
  cp "$REPO_DIR/feishu/feishu_app.example.json" "$REPO_DIR/feishu/feishu_app.json"
  chmod 600 "$REPO_DIR/feishu/feishu_app.json"
  echo "==> 已生成 feishu/feishu_app.json 模板，请填入 app_id / app_secret / base_token 后重跑本脚本"
  echo "    （配置指引见 docs/飞书自建应用配置指引.md，如随部署包未附带则找交付人索取）"
  exit 1
fi
if grep -q "在这里填" "$REPO_DIR/feishu/feishu_app.json"; then
  echo "==> feishu/feishu_app.json 仍是模板，请先填好 app_id / app_secret / base_token"
  exit 1
fi
chmod 600 "$REPO_DIR/feishu/feishu_app.json"

# ---------- 4. 小红书登录 cookie ----------
if [ ! -f "$MONITOR_DIR/login_cookies.txt" ]; then
  echo "==> 开始小红书扫码登录（会打开/生成二维码 login_qr.png，用小红书 App 扫码，可能含风控验证两步）"
  (cd "$MONITOR_DIR" && ./.venv/bin/python do_login.py)
fi
if [ ! -f "$MONITOR_DIR/login_cookies.txt" ]; then
  echo "==> 登录未完成，未生成 login_cookies.txt；请稍后手动执行："
  echo "    cd $MONITOR_DIR && ./.venv/bin/python do_login.py"
  exit 1
fi
chmod 600 "$MONITOR_DIR/login_cookies.txt"

# ---------- 5. 监控目标 ----------
if command -v "$MONITOR_DIR/.venv/bin/python" >/dev/null 2>&1; then
  if ! "$MONITOR_DIR/.venv/bin/python" -c "import json,sys; c=json.load(open('$MONITOR_DIR/watchdog_config.json')); sys.exit(0 if c['target'].get('host_id') or c['target'].get('room_id') else 1)"; then
    echo "==> 提示：monitor/watchdog_config.json 的 target 尚未配置。"
    echo "    可先不填，守护会启动但不会自动监控；拿到主播 host_id 或房间 room_id 后填入再重启。"
  fi
fi

# ---------- 6. 自检一轮 ----------
echo "==> 自检（relay/登录态/磁盘/开播发现）"
(cd "$MONITOR_DIR" && ./.venv/bin/python xhs_watchdog.py --once) || true

# ---------- 7. 安装 launchd 常驻 ----------
echo "==> 安装 launchd：$PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s#__MONITOR_DIR__#$MONITOR_DIR#g" \
  "$REPO_DIR/deploy/com.causmatrix.xhs-watchdog.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "==> 已启动。常用命令："
echo "    查看状态: launchctl list | grep xhs-watchdog"
echo "    停止:     launchctl unload $PLIST"
echo "    重启:     launchctl unload $PLIST && launchctl load $PLIST"
echo "    日志:     tail -f $MONITOR_DIR/logs/watchdog.launchd.log"
echo ""
echo "部署完成。"
