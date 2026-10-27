#!/bin/bash
set -e
cd /app
# config 目录挂载了宿主机 data/douyin/config，首次启动从镜像内模板复制
[ -f config/config.ini ] || cp /opt/config-examples/config.ini.example config/config.ini
[ -f config/URL_config.ini ] || cp /opt/config-examples/URL_config.ini.example config/URL_config.ini
exec python -u main.py
