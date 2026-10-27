#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一场直播的原始数据（viewers.csv / events.jsonl / segments_final.json / 逐字稿）
转换成飞书多维表各明细表的「批量写入」payload 文件（每批 ≤200 条）。

用法：
  python build_base_records.py <主播录像目录> <场次record_id> [--date YYYY-MM-DD]
产物：<主播录像目录>/base_payload/<table_id>_batchNN.json，内容 {"create_records":[...]}
随后用 lark-cli base +record-batch-create 逐文件串行写入。
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime, timedelta

# 表 ID（抖音直播爆品监控 Base）
T_VIEWERS = "tblwZpGYkQvTtJhB"   # 在线人数明细
T_EVENTS = "tblruHHI6yfHspnW"    # 互动明细
T_HITS = "tblCmcOpmQma2PP9"      # 爆品时刻
T_WORDS = "tblz3Jm73jNPhH2B"     # 话术词

# signals 采集 data 根（相对项目根）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIGNALS_DATA = os.path.join(PROJECT_ROOT, "signals", "DouyinLiveWebFetcher", "data")

# 与 auto_minutes.py 一致的默认话术关键词
TALK_KEYWORDS = ["上链接", "1号链接", "库存", "到手价", "最后", "仅剩", "321", "首发",
                 "拍", "券", "秒杀", "抢", "卖完", "补货", "加购", "下单", "价格"]

BATCH = 200


def to_sec(t):
    h, m, s = str(t).split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def write_batches(out_dir, table_id, records):
    n = 0
    files = []
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        p = os.path.join(out_dir, f"{table_id}_batch{i//BATCH+1:02d}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"create_records": chunk}, f, ensure_ascii=False)
        files.append(p)
        n += len(chunk)
    return files, n


def classify(k):
    if "链接" in k:
        return "链接位"
    if "价格" in k or "到手" in k:
        return "价格"
    if "首发" in k:
        return "首发"
    if "送" in k or "赠" in k:
        return "赠品"
    return "促销词"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("anchor_dir")
    ap.add_argument("session_rid")
    ap.add_argument("--date")
    args = ap.parse_args()

    d = os.path.abspath(args.anchor_dir)
    anchor = os.path.basename(d)
    parts_files = sorted(os.path.join(d, x) for x in os.listdir(d) if x.endswith("_parts.json"))
    parts_path = parts_files[-1]
    base = os.path.basename(parts_path).replace("_parts.json", "")
    date = args.date or re.search(r"(\d{4}-\d{2}-\d{2})", base).group(1)

    parts = json.load(open(parts_path, encoding="utf-8"))
    base_dt = datetime.strptime(parts[0]["wall_start"], "%Y-%m-%d %H:%M:%S")
    link = [{"id": args.session_rid}]

    def wall(rel):
        return (base_dt + timedelta(seconds=rel)).strftime("%Y-%m-%d %H:%M:%S")

    sig_dir = os.path.join(SIGNALS_DATA, anchor, date)
    out_dir = os.path.join(d, "base_payload")
    os.makedirs(out_dir, exist_ok=True)
    summary = {}

    # 1) 在线人数明细（全局秒 = epoch - 开播墙起）
    vrows = list(csv.DictReader(open(os.path.join(sig_dir, "viewers.csv"), encoding="utf-8")))
    vrecs = []
    for r in vrows:
        g = float(r["epoch"]) - base_dt.timestamp()
        vrecs.append({"场次": link, "墙钟时间": r["wall_time"], "全局秒": round(g, 1),
                      "当前人数": int(r["current"]), "累计观看": r["total"]})
    fs, n = write_batches(out_dir, T_VIEWERS, vrecs)
    summary["在线人数明细"] = (n, fs)

    # 2) 互动明细（排除 roomstats / room_end）
    ev_recs = []
    for line in open(os.path.join(sig_dir, "events.jsonl"), encoding="utf-8"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        if t in ("roomstats", "room_end"):
            continue
        if t == "chat":
            typ = "主播口播" if r.get("is_anchor") else "弹幕"
        elif t == "enter":
            typ = "进场"
        elif t == "follow":
            typ = "关注"
        elif t == "like":
            typ = "点赞"
        elif t == "gift":
            typ = "礼物"
        elif t == "fansclub":
            typ = "粉丝团"
        else:
            continue
        ev_recs.append({"场次": link, "时间": r.get("wall_time"), "类型": [typ],
                        "用户": r.get("name", "") or "", "内容": r.get("content", "") or "",
                        "是否主播": bool(r.get("is_anchor", False))})
    fs, n = write_batches(out_dir, T_EVENTS, ev_recs)
    summary["互动明细"] = (n, fs)

    # 3) 爆品时刻
    final_path = os.path.join(d, base + "_segments_final.json")
    final = json.load(open(final_path, encoding="utf-8"))
    hrecs = []
    for i, s in enumerate(final, 1):
        st, en = to_sec(s["start"]), to_sec(s["end"])
        notes = s.get("notes", [])
        danmu = next((x for x in notes if "弹幕" in x or "口播" in x), "")
        ren = next((x for x in notes if "在线人数" in x), "")
        huashu = "；".join(x for x in notes if "话术命中" in x)
        hrecs.append({"序号": i, "场次": link, "开始": wall(st), "结束": wall(en),
                      "信号来源": s.get("sources", []), "命中关键词": s.get("keyword", ""),
                      "弹幕原话": danmu, "人数变化": ren, "话术摘要": huashu})
    fs, n = write_batches(out_dir, T_HITS, hrecs)
    summary["爆品时刻"] = (n, fs)

    # 4) 话术词（从整场逐字稿按段落命中，带原句）
    txt_path = os.path.join(d, base + "_逐字稿.txt")
    raw = open(txt_path, encoding="utf-8").read()
    blocks = re.split(r"\n(?=Speaker \d+ \d{2}:\d{2}:\d{2})", raw)
    offset = 0.0
    wrecs, seen = [], set()
    for b in blocks:
        head = re.search(r"# 分段 \d+ 偏移 (\d{2}:\d{2}:\d{2})", b)
        if head:
            offset = to_sec(head.group(1))
        m = re.match(r"Speaker \d+ (\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*\n(.*)", b, re.S)
        if not m:
            continue
        t = offset + to_sec(m.group(1))
        text = m.group(2).strip()
        for k in TALK_KEYWORDS:
            if k in text and (t, k) not in seen:
                seen.add((t, k))
                wrecs.append({"场次": link, "时间": wall(t), "类型": [classify(k)],
                              "命中词": k, "原句": text[:300]})
    fs, n = write_batches(out_dir, T_WORDS, wrecs)
    summary["话术词"] = (n, fs)

    print(f"开播墙钟：{base_dt:%Y-%m-%d %H:%M:%S}")
    for name, (n, fs) in summary.items():
        print(f"{name}: {n} 条，{len(fs)} 个批次 -> {os.path.relpath(out_dir)}")


if __name__ == "__main__":
    main()
