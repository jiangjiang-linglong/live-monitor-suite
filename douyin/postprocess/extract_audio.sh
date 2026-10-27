#!/bin/bash
# 从直播录像抽取 ASR 专用音频（16kHz 单声道 32kbps m4a，妙记/火山 ASR 通用）
# 用法:
#   ./extract_audio.sh 单个录像.ts
#   ./extract_audio.sh downloads/        # 递归处理目录下所有 ts（已存在同名 m4a 则跳过）
set -e

FFMPEG="$HOME/.local/bin/ffmpeg"
[ -x "$FFMPEG" ] || FFMPEG="ffmpeg"

extract_one() {
  local f="$1"
  local out="${f%.*}.m4a"
  if [ -f "$out" ]; then
    echo "跳过(已存在): $out"
    return
  fi
  echo "抽取: $f"
  "$FFMPEG" -hide_banner -loglevel error -y -i "$f" \
    -vn -ac 1 -ar 16000 -c:a aac -b:a 32k "$out"
}

TARGET="$1"
if [ -z "$TARGET" ]; then
  echo "用法: $0 <录像.ts 或 目录>"
  exit 1
fi

if [ -d "$TARGET" ]; then
  while IFS= read -r f; do extract_one "$f"; done < <(find "$TARGET" -name '*.ts')
else
  extract_one "$TARGET"
fi
echo "全部完成"
