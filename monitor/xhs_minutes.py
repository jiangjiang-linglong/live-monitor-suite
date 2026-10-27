#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书直播录像 → 飞书妙记转写（免费）→ 话术时间轴
================================================
飞书妙记转写不收费（飞书内建能力），但飞书未开放「创建妙记」的应用身份接口，
因此这一步用 lark-cli（用户身份）；用户登录态（refresh token）通常可维持数周~数月。

流程：
  1. 多 TS 合并抽成 16kHz 单声道 m4a（音频很小，几分钟即转写完）
  2. lark-cli drive +upload 上传，再 minutes +upload 生成妙记
  3. 轮询 minutes +detail --transcript 拿带时间戳逐字稿
  4. 解析为 [(墙钟秒, 文本)]，扫描话术关键词
返回 minute_url、逐字稿、话术命中段落。
"""
import os
import re
import json
import glob
import subprocess
import time

# 妙记转写依赖 lark-cli（用户身份），可用环境变量 LARK_CLI 指定绝对路径
LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")
FFMPEG = os.path.expanduser("~/.local/bin/ffmpeg")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"

KEYWORDS = ["上链接", "链接", "库存", "到手价", "最后", "仅剩", "321", "首发",
            "拍", "券", "秒杀", "抢", "卖完", "补货", "加购", "下单", "价格",
            "优惠", "折扣", "赠品", "包邮", "现货", "退", "换"]


def _run(cmd, timeout=900):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(cmd), (p.stderr or p.stdout)[:500]))
    return p.stdout


def merge_to_audio(ts_files, out_m4a):
    """多 TS 合并抽 16kHz 单声道 m4a。用 concat demuxer，逐段无损拼接。"""
    work = os.path.dirname(out_m4a)
    lst = os.path.join(work, "_audio_concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for t in ts_files:
            f.write("file '%s'\n" % t)
    _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
          "-f", "concat", "-safe", "0", "-i", lst,
          "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "32k", out_m4a])
    return out_m4a


def _upload_drive(path):
    out = _run([LARK_CLI, "drive", "+upload", "--file", path,
                "--name", os.path.splitext(os.path.basename(path))[0], "--json"])
    d = json.loads(out)
    return d["data"]["file_token"]


def _create_minute(file_token):
    out = _run([LARK_CLI, "minutes", "+upload", "--file-token", file_token, "--json"])
    d = json.loads(out)["data"]
    return d.get("minute_token"), d.get("minute_url")


def _wait_transcript(minute_token, outdir, timeout=1800):
    start = time.time()
    while time.time() - start < timeout:
        subprocess.run([LARK_CLI, "minutes", "+detail", "--minute-tokens", minute_token,
                        "--transcript", "--overwrite", "--output-dir",
                        os.path.join(outdir, "minutes")],
                       capture_output=True, text=True)
        # transcript 实际落在 minutes/**/transcript.txt（目录名含 artifact 前缀）
        for tf in glob.glob(os.path.join(outdir, "minutes", "**", "transcript.txt"),
                            recursive=True):
            if os.path.getmtime(tf) >= start - 60:
                txt = open(tf, encoding="utf-8").read()
                if "Speaker" in txt and len(txt.strip()) > 50:
                    return txt
        time.sleep(15)
    raise TimeoutError("妙记转写超时")


def parse_transcript(transcript):
    """Speaker N HH:MM:SS(.ms) \\n 文本 → [(start_sec, text)]。"""
    blocks = re.split(r"\n(?=Speaker \d+ \d{2}:\d{2}:\d{2})", transcript)
    res = []
    for b in blocks:
        m = re.match(r"Speaker \d+ (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*\n(.*)", b, re.S)
        if m:
            hh, mm, ss, text = m.groups()
            sec = int(hh) * 3600 + int(mm) * 60 + int(ss)
            res.append((sec, text.strip()))
    return res


def transcribe(ts_files, outdir, logf=print):
    os.makedirs(outdir, exist_ok=True)
    audio = os.path.join(outdir, "整场音频.m4a")
    logf("合并 %d 段 TS 抽音频..." % len(ts_files))
    merge_to_audio(ts_files, audio)
    logf("上传并生成妙记...")
    ft = _upload_drive(audio)
    minute_token, minute_url = _create_minute(ft)
    logf("妙记已创建，等待转写：%s" % minute_url)
    transcript = _wait_transcript(minute_token, outdir)
    parsed = parse_transcript(transcript)
    # 话术命中
    hits = []
    for sec, text in parsed:
        kw = [k for k in KEYWORDS if k in text]
        if kw:
            hits.append({"sec": sec, "keywords": kw[:4], "text": text[:120]})
    # 保存逐字稿
    with open(os.path.join(outdir, "整场逐字稿.txt"), "w", encoding="utf-8") as f:
        f.write(transcript)
    return {"minute_url": minute_url, "minute_token": minute_token,
            "transcript": transcript, "parsed": parsed, "hits": hits}
