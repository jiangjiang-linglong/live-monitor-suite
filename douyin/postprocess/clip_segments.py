#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按信号时间点从直播录像切片（离线爆品信号片段归档）。

支持两种源：
  1. 单个录像文件：
       python clip_segments.py 录像.ts segments.json
  2. 整场多 TS（auto_minutes.py 目录模式产出的 *_parts.json）：
       python clip_segments.py 主播_日期_整场_parts.json 主播_日期_整场_segments.json
     脚本会把全局时间戳自动定位到对应 TS 分段，做段内切片，无需先合并整场视频。

segments.json 格式：
[
  {"start": "00:01:23", "end": "00:02:10", "reason": "话术关键词:上链接", "keyword": "上链接"},
  {"start": 185, "end": 240, "reason": "弹幕关键词:库存告急"}
]

时间支持 HH:MM:SS 或秒数。片段自动向前后各留 2 秒余量。默认重编码（切点精确）；
加 --copy 为流拷贝（秒级完成，但切点对齐关键帧、不精确）。
"""
import json
import os
import re
import subprocess
import sys

PAD = 2.0  # 片段前后余量（秒）


def to_sec(t):
    if isinstance(t, (int, float)):
        return float(t)
    parts = [float(p) for p in str(t).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', str(s)).strip('_')[:40]


def find_ffmpeg():
    ff = os.path.expanduser("~/.local/bin/ffmpeg")
    return ff if os.path.exists(ff) else "ffmpeg"


def load_source(arg):
    """返回 (parts, single)。parts 为 [{path,offset,duration}] 或 None；single 为文件路径或 None。"""
    if arg.lower().endswith(".json") and os.path.isfile(arg):
        with open(arg, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data and "path" in data[0] and "offset" in data[0]:
            return data, None
    return None, arg


def locate(parts, t):
    """全局秒（墙钟轴）→ (part, 段内秒)。落在重连墙缝时 clamp 到下一段段头。"""
    for p in parts:
        o, d = p["offset"], p["duration"]
        if o <= t < o + d:
            return p, t - o
        if t < o:
            return p, 0.0
    last = parts[-1]
    return last, min(max(t - last["offset"], 0.0), last["duration"])


def main():
    if len(sys.argv) < 3:
        print("用法: python clip_segments.py <录像文件 或 parts.json> <segments.json> [--copy]")
        sys.exit(1)

    src, seg_file = sys.argv[1], sys.argv[2]
    stream_copy = "--copy" in sys.argv
    parts, single = load_source(src)

    if parts is None and not os.path.isfile(single):
        print(f"找不到录像: {single}")
        sys.exit(1)

    with open(seg_file, encoding="utf-8") as f:
        segments = json.load(f)

    if parts:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(src)), "clips")
    else:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(single)), "clips")
    os.makedirs(out_dir, exist_ok=True)
    ffmpeg = find_ffmpeg()

    ok = 0
    for i, seg in enumerate(segments, 1):
        gs = max(0.0, to_sec(seg["start"]) - PAD)
        ge = to_sec(seg["end"]) + PAD
        label = safe_name(seg.get("reason") or seg.get("keyword") or "片段")
        stamp = f"{int(gs)//3600:02d}-{int(gs)%3600//60:02d}-{int(gs)%60:02d}"
        name = f"clip_{i:03d}_{stamp}_{label}.mp4"
        out = os.path.join(out_dir, name)

        if parts:
            p0, s0 = locate(parts, gs)
            p1, e1 = locate(parts, ge)
            if p0 is p1:
                duration = max(1.0, e1 - s0)
            else:
                # 片段横跨两个分段，先取起始段到段尾（极少数情况，重连间隙无内容）
                duration = max(1.0, p0["duration"] - s0)
                print(f"[{i}] 提示：跨分段，仅取起始段 {os.path.basename(p0['path'])} 内的部分")
            video, inner_start = p0["path"], s0
        else:
            video, inner_start = single, gs
            duration = max(1.0, ge - gs)

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{inner_start:.2f}", "-i", video, "-t", f"{duration:.2f}"]
        if stream_copy:
            cmd += ["-c", "copy"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "96k"]
        cmd.append(out)

        print(f"[{i}/{len(segments)}] {name}")
        r = subprocess.run(cmd)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            ok += 1
        else:
            print(f"   切片失败，跳过：{os.path.basename(video)}")

    print(f"完成，共 {ok} 个片段，输出目录: {out_dir}")


if __name__ == "__main__":
    main()
