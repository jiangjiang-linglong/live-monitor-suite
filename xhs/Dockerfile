# 小红书实时监控服务（relay + watchdog + pipeline + ffmpeg）
# 构建上下文为 xhs/ 目录：在仓库根目录执行 docker compose build xhs-monitor
FROM python:3.11-slim

# 国内镜像源（apt 阿里云 + pip 清华）；海外机器可删本段 sed 与 -i 参数
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        tzdata \
        ca-certificates \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /app/requirements.txt

COPY monitor /app/monitor
COPY relay /app/relay
COPY feishu /app/feishu

# 运行时数据目录（建议挂载宿主机卷）
RUN mkdir -p /app/deliveries /data/xhs/logs
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    XHS_COOKIE_PATH=/data/xhs/login_cookies.txt \
    XHS_LOGIN_QR=/data/xhs/login_qr.png

# watchdog 内部自管 relay、ffmpeg 与 pipeline；容器即守护进程
CMD ["python", "-u", "monitor/xhs_watchdog.py"]
