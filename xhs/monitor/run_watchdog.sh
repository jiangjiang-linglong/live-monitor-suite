#!/bin/bash
# 小红书无人值守守护 launchd 入口
# 由 com.causmatrix.xhs-watchdog.plist 拉起；进程崩溃 launchd 自动重启，
# 开机登录后自动运行（RunAtLoad）。单实例保护由 Python 主程序 pidfile 完成。
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
mkdir -p logs

# launchd 环境 PATH 极简，需补齐：
# - node：小红书签名 b1/ssk 运行时依赖（brew install node）
# - ffmpeg：录像与音频转写（brew install ffmpeg）
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
if ! command -v node >/dev/null 2>&1; then
  echo "$(date '+%F %T') 未找到 node，签名运行时不可用，请执行 brew install node" >> logs/watchdog.launchd.err.log
fi

exec "$SCRIPT_DIR/.venv/bin/python" -u "$SCRIPT_DIR/xhs_watchdog.py"
