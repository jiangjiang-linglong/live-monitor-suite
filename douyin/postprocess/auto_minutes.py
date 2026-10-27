#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
整场录像 → 飞书妙记自动转写 → 话术关键词命中时间点（segments.json）。

支持输入：
  - 单个录像文件：   auto_minutes.py 录像.ts
  - 一整个主播目录： auto_minutes.py downloads/抖音直播/XIAOQSTUDIO
                       （自动按日期+时间间隔识别场次，默认处理最新一场；--date 指定）
  - 多个 TS 文件：   auto_minutes.py a.ts b.ts c.ts  （按文件名时间序合并）

流程：
  1. 多 TS 用 ffmpeg concat 无损合并并抽成 16kHz 单声道 m4a（重连碎片也会拼回）
  2. 超过 5.5 小时（妙记单文件上限 6h）自动分段
  3. lark-cli drive +upload 上传，再 lark-cli minutes +upload 生成妙记并自动转写
  4. 轮询转写完成，下载带时间戳/说话人的逐字稿，按全局时间偏移合并
  5. 扫描话术关键词，输出 segments.json（可直接喂给 clip_segments.py 切片）

前置：已安装并登录 lark-cli（飞书）。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"

DEFAULT_KEYWORDS = ["上链接", "1号链接", "库存", "到手价", "最后", "仅剩", "321", "首发",
                    "拍", "券", "秒杀", "抢", "卖完", "补货", "加购", "下单", "价格"]
MAX_SEGMENT_HOURS = 5.5
GAP_HOURS = 2.0  # 同目录下相邻 TS 起始时间间隔超过该值视为另一场
WORK_DIR = "minutes_work"
AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".aac")


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{(p.stderr or p.stdout)[:800]}")
    return p.stdout


def get_duration(path):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", p.stderr)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def ts_datetime(path):
    """从文件名 主播_YYYY-MM-DD_HH-MM-SS_000.ts 解析开始时间。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", os.path.basename(path))
    if not m:
        return None
    d, h, mi, s = m.groups()
    return datetime.strptime(f"{d} {h}:{mi}:{s}", "%Y-%m-%d %H:%M:%S")


def group_sessions(ts_files):
    """按日期分组，同日内相邻片段间隔 > GAP_HOURS 切成不同场。返回 [[ts,...], ...]，按时间升序。"""
    labeled = [(ts_datetime(f), f) for f in ts_files]
    labeled = [(t, f) for t, f in labeled if t]
    labeled.sort(key=lambda x: x[0])
    sessions, cur, last_t = [], [], None
    for t, f in labeled:
        if last_t and (t - last_t) > timedelta(hours=GAP_HOURS):
            if cur:
                sessions.append(cur)
            cur = []
        cur.append(f)
        last_t = t
    if cur:
        sessions.append(cur)
    return sessions


def collect_inputs(inputs, date_filter):
    """返回 (待合并 ts 列表 or None, 单个媒体文件 or None, 输出基名, 所在目录)。"""
    if len(inputs) == 1 and os.path.isdir(inputs[0]):
        d = inputs[0]
        ts_files = sorted(glob.glob(os.path.join(d, "*.ts")))
        if not ts_files:
            raise RuntimeError(f"目录 {d} 下没有 .ts 文件")
        sessions = group_sessions(ts_files)
        print(f"在 {os.path.basename(d)} 下识别到 {len(sessions)} 场：")
        for i, sess in enumerate(sessions):
            t0 = ts_datetime(sess[0])
            print(f"  [{i}] {t0:%Y-%m-%d %H:%M}，{len(sess)} 段")
        chosen = None
        if date_filter:
            chosen = [s for s in sessions if ts_datetime(s[0]).strftime("%Y-%m-%d") == date_filter]
            if not chosen:
                raise RuntimeError(f"没有 {date_filter} 的场次")
            chosen = chosen[-1]
        else:
            chosen = sessions[-1]
            print(f"  默认处理最新一场（用 --date 可指定）")
        anchor = os.path.basename(os.path.normpath(d))
        date = ts_datetime(chosen[0]).strftime("%Y-%m-%d")
        return chosen, None, f"{anchor}_{date}_整场", d

    if len(inputs) == 1 and not os.path.isdir(inputs[0]):
        f = inputs[0]
        base = os.path.splitext(os.path.basename(f))[0]
        return None, f, base, os.path.dirname(os.path.abspath(f))

    # 多个文件
    files = sorted(inputs, key=lambda x: ts_datetime(x) or datetime.min)
    base = os.path.splitext(os.path.basename(files[0]))[0] + "_合并"
    return files, None, base, os.path.dirname(os.path.abspath(files[0]))


def make_silence(path, seconds, ffmpeg=FFMPEG):
    """生成指定时长的 16kHz 单声道 PCM 静音 WAV。"""
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000",
         "-t", f"{seconds:.2f}", "-c:a", "pcm_s16le", path])


def concat_ts_to_audio(ts_files, out_dir, base):
    """多 TS 合并成整场 m4a，时间轴对齐墙钟（用于和弹幕/人数的真实时间对齐）。

    各 TS 是独立录制进程产生、PTS 都从 0 开始，直接 concat 会只取第一段；因此先逐段抽
    统一 PCM WAV（无时间戳、断流碎片互不影响）再 concat 重编码。
    两类时间：
      - 同一进程的连续分段，文件名时间戳相同、后缀递增（_000/_001…），PTS 连续、无缝；
      - 不同进程的重连碎片时间戳不同，墙钟之间存在录制中断缝隙。
    段间按墙钟缝隙插入等长静音，使整场音频/逐字稿/弹幕/人数共用同一墙钟时间轴。
    返回 (整场m4a, parts_meta)；parts_meta.offset 为相对首段墙钟起点的秒数。
    """
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    wav_dir = os.path.join(out_dir, base + "_wav")
    os.makedirs(wav_dir, exist_ok=True)
    wav_list, ok = [], 0
    parts_meta, offset_clock = [], 0.0
    base_wall = None
    prev_wall_start = None
    prev_dur = 0.0
    prev_named = None
    print(f"① 逐段抽音频（{len(ts_files)} 个分段）...")
    for idx, ts in enumerate(ts_files):
        named = ts_datetime(ts)
        # 同名时间戳的连续分段（_000/_001…）PTS 连续；否则按文件名墙钟起点
        if prev_named is not None and named == prev_named:
            wall_start = prev_wall_start + timedelta(seconds=prev_dur)
        else:
            wall_start = named
        if base_wall is None:
            base_wall = wall_start
        if prev_wall_start is not None:
            gap = (wall_start - (prev_wall_start + timedelta(seconds=prev_dur))).total_seconds()
        else:
            gap = 0.0

        wav = os.path.join(wav_dir, f"seg_{idx:03d}.wav")
        p = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", ts,
                            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
                           capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(wav):
            print(f"   [跳过] 第 {idx+1} 段抽取失败：{os.path.basename(ts)}")
            prev_named = named
            continue
        dur = get_duration(wav)
        off = (wall_start - base_wall).total_seconds()
        # 墙缝：插入静音，使音频时间轴与墙钟一致
        if gap > 2.0:
            sil = os.path.join(wav_dir, f"gap_{idx:03d}.wav")
            make_silence(sil, gap)
            wav_list.append(sil)
            offset_clock += gap
            print(f"   第 {idx+1} 段前墙缝 {gap:.0f}s，插入静音对齐")
        wav_list.append(wav)
        parts_meta.append({"path": os.path.abspath(ts), "duration": round(dur, 3),
                           "offset": round(off, 3),
                           "wall_start": wall_start.strftime("%Y-%m-%d %H:%M:%S")})
        offset_clock = off + dur
        ok += 1
        prev_wall_start, prev_dur, prev_named = wall_start, dur, named
    if not wav_list:
        raise RuntimeError("没有任何分段能成功抽取音频")
    list_file = os.path.join(out_dir, base + "_concat.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for w in wav_list:
            f.write(f"file '{w}'\n")
    whole = os.path.join(out_dir, base + ".m4a")
    print(f"   合并 {ok}/{len(ts_files)} 段（含墙缝静音）为整场音频 ...")
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", list_file, "-c:a", "aac", "-b:a", "32k", whole])
    # 清理临时 WAV（保留 concat 清单便于复核）
    for w in wav_list:
        try:
            os.remove(w)
        except OSError:
            pass
    try:
        os.rmdir(wav_dir)
    except OSError:
        pass
    return whole, parts_meta


def split_audio(whole, out_dir, base):
    """整场音频超过 5.5h 则按 5.5h 切段，返回分段列表（单段则原样返回）。"""
    dur = get_duration(whole)
    if dur <= MAX_SEGMENT_HOURS * 3600:
        return [whole]
    print(f"② 整场音频 {dur/3600:.1f}h 超 6h，按 {MAX_SEGMENT_HOURS}h 分段 ...")
    pattern = os.path.join(out_dir, base + "_part%02d.m4a")
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", whole,
         "-c", "copy", "-f", "segment", "-segment_time", str(int(MAX_SEGMENT_HOURS * 3600)), pattern])
    return sorted(glob.glob(os.path.join(out_dir, base + "_part*.m4a")))


def prepare_audio(ts_files, single, out_dir, base):
    """统一入口：返回 (妙记分段 m4a 列表, 总时长, 源TS段映射parts_meta)。"""
    os.makedirs(out_dir, exist_ok=True)
    parts_meta = None
    if ts_files:
        whole, parts_meta = concat_ts_to_audio(ts_files, out_dir, base)
    else:
        if single.lower().endswith(AUDIO_EXTS):
            return [single], get_duration(single), None
        whole = os.path.join(out_dir, base + ".m4a")
        if not os.path.exists(whole):
            print("① 抽取音频 ...")
            run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", single,
                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", whole])
    return split_audio(whole, out_dir, base), get_duration(whole), parts_meta


def upload_to_minute(m4a_path):
    name = os.path.splitext(os.path.basename(m4a_path))[0]
    print(f"③ 上传 {name} 到云空间 ...")
    up = json.loads(run(["lark-cli", "drive", "+upload", "--file", m4a_path, "--name", name]))
    file_token = up["data"]["file_token"]
    print("④ 生成妙记并触发转写 ...")
    mn = json.loads(run(["lark-cli", "minutes", "+upload", "--file-token", file_token]))
    return mn["data"]["minute_token"], mn["data"]["minute_url"]


def fetch_transcript(minute_token, timeout=1800):
    print(f"⑤ 等待妙记 {minute_token} 转写完成 ...")
    start = time.time()
    tf = os.path.join("minutes", minute_token, "transcript.txt")
    while time.time() - start < timeout:
        # 转写未完成时该命令返回非 0，属正常等待，继续轮询
        subprocess.run(["lark-cli", "minutes", "+detail", "--minute-tokens", minute_token,
                        "--transcript", "--overwrite"],
                       capture_output=True, text=True)
        if os.path.exists(tf):
            txt = open(tf, encoding="utf-8").read()
            if "Speaker" in txt and len(txt.strip()) > 50:
                print("   转写完成")
                return txt
        print("   转写进行中，20 秒后重试 ...")
        time.sleep(20)
    raise TimeoutError(f"妙记 {minute_token} 转写超时")


def to_seconds(hhmmss):
    h, m, s = hhmmss.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def hhmmss(sec):
    sec = int(sec)
    return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"


def scan_keywords(transcript, offset, keywords):
    """解析带时间戳逐字稿，命中关键词的段落生成 segment（offset 为该段音频相对整场的秒偏移）。"""
    blocks = re.split(r"\n(?=Speaker \d+ \d{2}:\d{2}:\d{2})", transcript)
    parsed = []
    for b in blocks:
        m = re.match(r"Speaker \d+ (\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*\n(.*)", b, re.S)
        if m:
            parsed.append((to_seconds(m.group(1)), m.group(2).strip()))
    segments = []
    for i, (start, content) in enumerate(parsed):
        end = parsed[i + 1][0] if i + 1 < len(parsed) else start + 30
        hits = [k for k in keywords if k in content]
        if hits:
            segments.append({
                "start": hhmmss(offset + start),
                "end": hhmmss(offset + max(end, start + 10)),
                "reason": "话术关键词:" + "/".join(hits[:3]),
                "keyword": "/".join(hits[:3]),
            })
    return segments


def main():
    ap = argparse.ArgumentParser(description="整场录像 → 妙记转写 → 关键词片段")
    ap.add_argument("inputs", nargs="+", help="录像文件、多个文件，或主播目录")
    ap.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS), help="逗号分隔的关键词")
    ap.add_argument("--date", help="目录模式下指定日期 YYYY-MM-DD（默认最新一场）")
    ap.add_argument("--wait-timeout", type=int, default=1800, help="单个妙记等待转写秒数")
    args = ap.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    ts_files, single, base, parent = collect_inputs(args.inputs, args.date)
    out_dir = os.path.join(parent, WORK_DIR)
    parts, dur, parts_meta = prepare_audio(ts_files, single, out_dir, base)
    print(f"   整场时长约 {dur/3600:.2f}h，共 {len(parts)} 个妙记分段")

    all_segments, transcript_dump = [], []
    for idx, part in enumerate(parts):
        offset = idx * MAX_SEGMENT_HOURS * 3600
        minute_token, url = upload_to_minute(part)
        print(f"   妙记链接: {url}")
        transcript = fetch_transcript(minute_token, args.wait_timeout)
        transcript_dump.append(f"# 分段 {idx+1} 偏移 {hhmmss(offset)} 妙记 {url}\n\n{transcript}\n")
        segs = scan_keywords(transcript, offset, keywords)
        print(f"   命中 {len(segs)} 个关键词片段")
        all_segments.extend(segs)

    seg_file = os.path.join(parent, f"{base}_segments.json")
    with open(seg_file, "w", encoding="utf-8") as f:
        json.dump(all_segments, f, ensure_ascii=False, indent=2)
    txt_file = os.path.join(parent, f"{base}_逐字稿.txt")
    with open(txt_file, "w", encoding="utf-8") as f:
        f.write("\n".join(transcript_dump))

    print(f"\n完成：共 {len(all_segments)} 个候选片段")
    print(f"  片段清单：{seg_file}")
    print(f"  整场逐字稿：{txt_file}")
    if ts_files:
        # 多 TS：写段映射，clip_segments 据此定位到具体 TS 做段内切片（无需合并整场视频）
        parts_file = os.path.join(parent, f"{base}_parts.json")
        with open(parts_file, "w", encoding="utf-8") as f:
            json.dump(parts_meta, f, ensure_ascii=False, indent=2)
        print(f"  分段映射：{parts_file}")
        print("切片（自动定位到对应 TS 分段）：")
        print(f"  python postprocess/clip_segments.py '{parts_file}' '{seg_file}'")
    elif single:
        print("切片：")
        print(f"  python postprocess/clip_segments.py '{single}' '{seg_file}'")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)
