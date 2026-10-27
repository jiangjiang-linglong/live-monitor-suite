#!/bin/bash
# 抖音录播器常驻启动脚本（供 launchd / 手动调用）
# 注意：本服务仅录制抖音（及其他平台）。小红书实时监控请使用 xhs 服务。
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
mkdir -p logs

# 首次运行：由模板生成配置（cookie 与监控房间需自行填写）
[ -f config/config.ini ] || cp config/config.ini.example config/config.ini
[ -f config/URL_config.ini ] || cp config/URL_config.ini.example config/URL_config.ini

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export TERM="${TERM:-xterm-256color}"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
exec "$PY" -u main.py
