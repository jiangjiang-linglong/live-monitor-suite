#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回填指定场次的商品图附件：
对 商品讲解 / 商品热度榜 / 爆品时刻 三张表中属于某场次的记录，
按 商品ID（爆品时刻按 序号→surge gid）补写「商品图」附件字段。

用法:
  ./.venv/bin/python backfill_images.py <session_record_id> <signal_state.json 路径>
"""
import sys
import json
import requests

from xhs_base_writer import (BaseWriter, T_GOODS, T_RANK, T_HITS)


def list_records_for_session(bw, table, session_id):
    """列出表里属于该场次的记录（分页拉全量后本地过滤）。"""
    out = []
    page_token = None
    while True:
        url = bw._url(table, "records?page_size=200")
        if page_token:
            url += "&page_token=" + page_token
        r = requests.get(url, headers=bw.app._headers(), timeout=30)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("读取 %s 失败 code=%s msg=%s" % (table, d.get("code"), d.get("msg")))
        data = d["data"]
        for rec in data.get("items", []):
            links = rec["fields"].get("场次") or []
            matched = False
            for l in links:
                if not isinstance(l, dict):
                    continue
                ids = l.get("record_ids") or ([l.get("record_id")] if l.get("record_id") else [])
                if session_id in ids:
                    matched = True
            if matched:
                out.append(rec)
        if data.get("has_more"):
            page_token = data.get("page_token")
        else:
            break
    return out


def batch_update(bw, table, records):
    for i in range(0, len(records), 200):
        chunk = records[i:i + 200]
        r = requests.post(bw._url(table, "records/batch_update"),
                          headers=bw.app._headers(),
                          data=json.dumps({"records": chunk}, ensure_ascii=False), timeout=60)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("更新 %s 失败 code=%s msg=%s" % (table, d.get("code"), d.get("msg")))


def text_of(v):
    """多维表文本字段读取后可能是 str 或 [{'text':...}]。"""
    if isinstance(v, list):
        return "".join(x.get("text", "") for x in v if isinstance(x, dict))
    return v or ""


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    session_id = sys.argv[1]
    state = json.load(open(sys.argv[2], encoding="utf-8"))
    goods = state["goods"]
    surges = state.get("surge_moments", [])
    bw = BaseWriter(logf=print)

    # 1) 商品讲解 & 热度榜：按商品ID
    for table, name in ((T_GOODS, "商品讲解"), (T_RANK, "商品热度榜")):
        recs = list_records_for_session(bw, table, session_id)
        updates = []
        for rec in recs:
            gid = text_of(rec["fields"].get("商品ID"))
            img_url = goods.get(gid, {}).get("image")
            if not img_url:
                continue
            att = bw._img_attach(img_url)
            if att:
                updates.append({"record_id": rec["record_id"], "fields": {"商品图": att}})
        if updates:
            batch_update(bw, table, updates)
        print("%s: %d 条记录，回填 %d 张图" % (name, len(recs), len(updates)))

    # 2) 爆品时刻：按序号对应 surge_moments
    hits = list_records_for_session(bw, T_HITS, session_id)
    updates = []
    for rec in hits:
        idx = rec["fields"].get("序号")
        idx = int(idx) if idx is not None else None
        if not idx or idx > len(surges):
            continue
        ts, base, cnt, gid, gtitle = surges[idx - 1]
        img_url = goods.get(gid, {}).get("image") if gid else None
        if not img_url:
            continue
        att = bw._img_attach(img_url)
        if att:
            updates.append({"record_id": rec["record_id"], "fields": {"商品图": att}})
    if updates:
        batch_update(bw, T_HITS, updates)
    print("爆品时刻: %d 条记录，回填 %d 张图" % (len(hits), len(updates)))


if __name__ == "__main__":
    main()
