#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下播后自动生成「直播爆品监控报告」，并合并话术/弹幕/人数三类信号为统一切片清单。

输入：主播录像目录（auto_minutes.py 已产出 <主播>_<日期>_整场_segments.json / _parts.json / _逐字稿.txt）
     信号采集目录（signals/DouyinLiveWebFetcher/data/<主播>/<日期>/，含 hits.jsonl / viewers.csv / events.jsonl）

输出（写在主播录像目录下）：
  - <主播>_<日期>_整场_爆品报告.md
  - <主播>_<日期>_整场_segments_final.json   （喂给 clip_segments.py 切片，顺序即 clip_001、clip_002…）

用法：
  python build_report.py downloads/抖音直播/XIAOQSTUDIO
  python build_report.py downloads/抖音直播/XIAOQSTUDIO --date 2026-10-24
切片：
  python clip_segments.py "<主播>_<日期>_整场_parts.json" "<主播>_<日期>_整场_segments_final.json"
"""
import argparse
import csv
import glob
import json
import os
import re
from collections import Counter
from datetime import datetime

# 项目根（postprocess 的上两级），用于定位 signals 采集目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SIGNALS = os.path.join(PROJECT_ROOT, "signals", "DouyinLiveWebFetcher", "data")

# 转化/价格类表达统计（业务报告用）
PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")
LINK_RE = re.compile(r"(\d+)\s*号链接")
DEAL_WORDS = ["券", "折", "包邮", "送", "赠", "抢", "首发", "到手", "限量", "库存", "最低价", "福利", "加购", "下单"]
# 话术/弹幕统计关键词（与采集侧保持一致的大词）
KEY_WORDS = ["上链接", "链接", "库存", "加库存", "抢", "拍", "券", "到手价", "到手", "最低价", "首发",
             "福利", "秒杀", "限量", "最后", "仅剩", "卖完", "补货", "下单", "加购", "包邮", "送", "价格", "折"]


def hhmmss(sec):
    sec = int(round(sec))
    return f"{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}"


def to_sec(t):
    h, m, s = str(t).split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def ts_datetime_from_name(path):
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", os.path.basename(path))
    if not m:
        return None
    d, h, mi, s = m.groups()
    return datetime.strptime(f"{d} {h}:{mi}:{s}", "%Y-%m-%d %H:%M:%S")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_transcript(path):
    """解析整场逐字稿（含 '# 分段 N 偏移 HH:MM:SS' 头），返回 [(全局start, speaker, text), ...]。"""
    if not path or not os.path.exists(path):
        return []
    raw = open(path, encoding="utf-8").read()
    blocks = re.split(r"\n(?=Speaker \d+ \d{2}:\d{2}:\d{2})", raw)
    offset = 0.0
    out = []
    for b in blocks:
        head = re.search(r"# 分段 \d+ 偏移 (\d{2}:\d{2}:\d{2})", b)
        if head:
            offset = to_sec(head.group(1))
        m = re.match(r"Speaker (\d+) (\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*\n(.*)", b, re.S)
        if m:
            sp, loc, text = m.group(1), m.group(2), m.group(3).strip()
            out.append((offset + to_sec(loc), sp, text))
    return out


def load_viewers(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({"epoch": float(r["epoch"]), "rel": float(r["rel_sec"]),
                             "current": int(r["current"]), "total": str(r["total"]),
                             "wall": r["wall_time"]})
            except (ValueError, KeyError):
                continue
    return rows


def load_hits(path):
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def count_events(path):
    c = Counter()
    anchor_chats = 0
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            c[rec.get("type", "?")] += 1
            if rec.get("type") == "chat" and rec.get("is_anchor"):
                anchor_chats += 1
    return c, anchor_chats


def merge_intervals(items, gap=45):
    """items: [{start,end,sources:set,notes:list,kws:set}]，按 start 排序合并重叠/相邻。"""
    items = sorted(items, key=lambda x: x["start"])
    out = []
    for it in items:
        if out and it["start"] <= out[-1]["end"] + gap:
            o = out[-1]
            o["end"] = max(o["end"], it["end"])
            o["sources"] |= it["sources"]
            o["notes"] = [n for n in (o["notes"] + it["notes"]) if n][:4]
            o["kws"] |= it["kws"]
        else:
            out.append(dict(it))
    return out


def main():
    ap = argparse.ArgumentParser(description="生成直播爆品监控报告")
    ap.add_argument("anchor_dir", help="主播录像目录")
    ap.add_argument("--date", help="日期 YYYY-MM-DD，默认取目录里最新一场")
    ap.add_argument("--signals-dir", default=DEFAULT_SIGNALS, help="信号采集 data 根目录")
    ap.add_argument("--merge-gap", type=int, default=45, help="相邻信号合并间隔秒数")
    args = ap.parse_args()

    d = os.path.abspath(args.anchor_dir)
    anchor = os.path.basename(d)
    parts_files = sorted(glob.glob(os.path.join(d, "*_parts.json")))
    if not parts_files:
        raise SystemExit(f"未找到 *_parts.json，请先在 {anchor} 目录运行 auto_minutes.py（整场）")
    # 选场次
    if args.date:
        cand = [p for p in parts_files if f"_{args.date}_整场" in p]
        parts_path = cand[-1] if cand else parts_files[-1]
    else:
        parts_path = parts_files[-1]
    base = os.path.basename(parts_path).replace("_parts.json", "")
    date = re.search(r"(\d{4}-\d{2}-\d{2})", base).group(1)
    seg_path = os.path.join(d, base + "_segments.json")
    txt_path = os.path.join(d, base + "_逐字稿.txt")
    parts = load_json(parts_path)
    talk_segments = load_json(seg_path) if os.path.exists(seg_path) else []
    transcripts = parse_transcript(txt_path)

    # 录像元数据（墙钟跨度，含重连墙缝；parts.offset 已按墙钟对齐）
    start_dt = ts_datetime_from_name(parts[0]["path"])
    total_dur = max(p["offset"] + p["duration"] for p in parts)
    start_epoch = start_dt.timestamp() if start_dt else None

    # 信号采集
    sig_dir = os.path.join(args.signals_dir, anchor, date)
    hits = load_hits(os.path.join(sig_dir, "hits.jsonl"))
    viewers = load_viewers(os.path.join(sig_dir, "viewers.csv"))
    ev_counts, anchor_chats = count_events(os.path.join(sig_dir, "events.jsonl"))

    # ---------- 汇总三类信号到统一时间轴 ----------
    intervals = []
    for s in talk_segments:
        st, en = to_sec(s["start"]), to_sec(s["end"])
        kw = s.get("keyword", "")
        intervals.append({
            "start": st, "end": max(en, st + 10),
            "sources": {"妙记话术"}, "kws": {k for k in kw.split("/") if k},
            "notes": [f"话术命中「{kw}」"] if kw else [],
        })
    for h in hits:
        if start_epoch is None:
            break
        g = h["epoch"] - start_epoch
        if g < 0 or g > total_dur + 600:
            continue
        if h.get("signal") == "弹幕关键词":
            who = "主播口播" if h.get("is_anchor") else "观众弹幕"
            kws = set(h.get("keywords", []))
            intervals.append({
                "start": g - 10, "end": g + 30,
                "sources": {"弹幕关键词"}, "kws": kws,
                "notes": [f"{who}「{h.get('content','')[:40]}」"],
            })
        elif h.get("signal") == "在线人数突增":
            intervals.append({
                "start": g - 30, "end": g + 30,
                "sources": {"人数突增"}, "kws": set(),
                "notes": [f"在线人数 {h.get('from')}→{h.get('to')}（+{h.get('delta')}，{h.get('pct')}%）"],
            })
    for it in intervals:
        it["start"] = max(0, it["start"])
        it["end"] = max(it["end"], it["start"] + 5)
    merged = merge_intervals(intervals, args.merge_gap)

    # 统一切片清单（兼容 clip_segments.py：start/end/reason/keyword）
    final_segments = []
    for i, it in enumerate(merged, 1):
        kws = sorted(it["kws"])
        reason = "+".join(sorted(it["sources"]))
        if kws:
            reason += ":" + "/".join(kws[:3])
        final_segments.append({
            "start": hhmmss(it["start"]), "end": hhmmss(it["end"]),
            "reason": reason, "keyword": "/".join(kws[:3]),
            "sources": sorted(it["sources"]), "notes": it["notes"],
        })
    final_path = os.path.join(d, base + "_segments_final.json")
    with open(final_path, "w", encoding="utf-8") as f:
        json.dump(final_segments, f, ensure_ascii=False, indent=2)

    # ---------- 人数统计 ----------
    vstat = {"peak": None, "avg": None, "first": None, "last": None, "cum": None}
    if viewers:
        peak = max(viewers, key=lambda r: r["current"])
        if start_epoch:
            peak["global"] = peak["epoch"] - start_epoch
        vstat["peak"] = peak
        vstat["avg"] = round(sum(r["current"] for r in viewers) / len(viewers))
        vstat["first"] = viewers[0]
        vstat["last"] = viewers[-1]
        vstat["cum"] = viewers[-1]["total"]

    surge_hits = [h for h in hits if h.get("signal") == "在线人数突增"]

    # ---------- 话术统计 ----------
    full_text = "\n".join(t[2] for t in transcripts)
    word_freq = Counter()
    for w in KEY_WORDS:
        n = full_text.count(w)
        if n:
            word_freq[w] = n
    prices = Counter(PRICE_RE.findall(full_text))
    links = Counter(LINK_RE.findall(full_text))
    deal_freq = {w: full_text.count(w) for w in DEAL_WORDS if full_text.count(w)}

    # ---------- 生成 Markdown ----------
    L = []
    L.append(f"# {anchor} 直播爆品监控报告（{date}）\n")
    L.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}　|　数据来源：本地直播录制 + 飞书妙记转写 + 弹幕/在线人数采集\n")

    L.append("## 一、直播概览\n")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| 主播 | {anchor} |")
    L.append(f"| 开播时间（开始录制） | {start_dt:%Y-%m-%d %H:%M:%S} |" if start_dt else "| 开播时间 | 无法识别 |")
    L.append(f"| 整场时长 | {total_dur/3600:.2f} 小时（{hhmmss(total_dur)}） |")
    L.append(f"| 录像分段数 | {len(parts)} 段 |")
    if vstat["peak"]:
        L.append(f"| 在线人数峰值 | {vstat['peak']['current']} 人（{hhmmss(vstat['peak']['global'])}） |")
        L.append(f"| 在线人数均值 | {vstat['avg']} 人 |")
        L.append(f"| 累计观看 | {vstat['cum']} |")
    L.append(f"| 弹幕/聊天 | {ev_counts.get('chat', 0)} 条（其中主播口播 {anchor_chats} 条） |")
    L.append(f"| 进入直播间 | {ev_counts.get('enter', 0)} 人次 |")
    L.append(f"| 关注 | {ev_counts.get('follow', 0)} 次 |")
    L.append(f"| 点赞 | {ev_counts.get('like', 0)} 次（消息条数，非总点赞数） |")
    L.append(f"| 礼物消息 | {ev_counts.get('gift', 0)} 条 |")
    L.append("")

    L.append("## 二、在线人数表现\n")
    if vstat["peak"]:
        L.append(f"- 峰值 **{vstat['peak']['current']} 人**，出现于 {hhmmss(vstat['peak']['global'])}；"
                 f"区间均值约 {vstat['avg']} 人；累计观看 {vstat['cum']}。")
        L.append(f"- 监测到 **{len(surge_hits)} 次在线人数突增**（阈值默认 15% 且 +100 人）。")
        if surge_hits and start_epoch:
            L.append("\n| 时间 | 人数变化 | 增幅 |")
            L.append("| --- | --- | --- |")
            for h in surge_hits:
                g = h["epoch"] - start_epoch
                L.append(f"| {hhmmss(g)} | {h.get('from')} → {h.get('to')} | +{h.get('delta')}（{h.get('pct')}%） |")
        L.append("")
    else:
        L.append("- 本场无在线人数数据（信号采集未覆盖或未开播）。\n")

    L.append("## 三、爆品时刻时间线（建议重点复盘）\n")
    if final_segments:
        L.append("> 合并「妙记话术命中 + 弹幕关键词 + 在线人数突增」，相邻 45 秒内信号归并为同一时刻。")
        L.append("> 序号与切片文件 `clip_001 / clip_002 …` 一一对应。\n")
        L.append("| 序号 | 时间 | 时长 | 信号来源 | 触发内容 / 关键词 |")
        L.append("| --- | --- | --- | --- | --- |")
        for i, s in enumerate(final_segments, 1):
            dur = to_sec(s["end"]) - to_sec(s["start"])
            note = "；".join(s.get("notes", [])) or s.get("keyword", "")
            L.append(f"| {i:03d} | {s['start']}–{s['end']} | {dur:.0f}s | {'、'.join(s['sources'])} | {note} |")
        L.append("")
    else:
        L.append("- 本场未命中任何爆品信号，可检查关键词配置或降低人数突增阈值。\n")

    L.append("## 四、高频转化话术\n")
    if word_freq:
        L.append("**口播关键词出现次数（整场逐字稿）**\n")
        L.append("| 关键词 | 次数 |")
        L.append("| --- | --- |")
        for w, n in word_freq.most_common(15):
            L.append(f"| {w} | {n} |")
        L.append("")
    if links:
        L.append(f"**口播主推链接位**：{ '、'.join(f'{n}号链接×{c}次' for n, c in links.most_common(8)) }\n")
    if prices:
        L.append(f"**口播提及价格点**：{ '、'.join(f'{p}元×{c}次' for p, c in prices.most_common(10)) }\n")
    if deal_freq:
        L.append("**优惠/促单词**：" + "、".join(f"{w}×{c}" for w, c in
                 sorted(deal_freq.items(), key=lambda x: -x[1])))
        L.append("")

    L.append("## 五、说明与局限\n")
    L.append("- 本报告的「热度」来自**在线人数曲线、弹幕互动、口播话术**三类可直接观测的公开信号；")
    L.append("- 竞品直播间**没有公开的实时成交/销量数据源**，销量跳涨无法直接采集，建议对上述爆品时刻的")
    L.append("  商品卡做抽帧 OCR 或用蝉妈妈事后核对销量；")
    L.append("- 弹幕为服务端推送给网页观众的可见消息，在线人数约每 5–10 秒刷新一次；")
    L.append("- 切片视频在录像目录的 `clips/` 下，按本报告序号对应；如需逐字稿原文见同目录 `_逐字稿.txt`。\n")

    report_path = os.path.join(d, base + "_爆品报告.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print(f"报告已生成：{report_path}")
    print(f"合并切片清单：{final_path}（共 {len(final_segments)} 个爆品时刻）")
    print("切片命令：")
    print(f"  python postprocess/clip_segments.py '{parts_path}' '{final_path}'")


if __name__ == "__main__":
    main()
