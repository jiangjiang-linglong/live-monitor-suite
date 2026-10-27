#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回填某场次所有子表记录的 主播名 / 场次名称 文本字段（切片器联动用）。
用法:
  ./.venv/bin/python backfill_slicer_fields.py <session_record_id> <主播名> <场次名称>
"""
import sys
import json
import requests

from xhs_base_writer import (BaseWriter, T_VIEWERS, T_EVENTS, T_HITS,
                             T_GOODS, T_RANK, T_TALK)

CHILD_TABLES = [
    (T_VIEWERS, '在线人数明细'),
    (T_EVENTS, '互动明细'),
    (T_HITS, '爆品时刻'),
    (T_GOODS, '商品讲解'),
    (T_RANK, '商品热度榜'),
    (T_TALK, '话术词'),
]


def list_records_for_session(bw, table, session_id):
    out = []
    page_token = None
    while True:
        url = bw._url(table, "records?page_size=500")
        if page_token:
            url += "&page_token=" + page_token
        r = requests.get(url, headers=bw.app._headers(), timeout=30)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("读取 %s 失败 code=%s msg=%s" % (table, d.get("code"), d.get("msg")))
        data = d["data"]
        for rec in data.get("items", []):
            for l in rec["fields"].get("场次") or []:
                if isinstance(l, dict) and (
                        session_id in (l.get("record_ids") or [])
                        or l.get("record_id") == session_id):
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


def main():
    session_id, anchor, session_name = sys.argv[1], sys.argv[2], sys.argv[3]
    bw = BaseWriter(logf=print)
    fields = {"主播名": anchor, "场次名称": session_name}
    for table, name in CHILD_TABLES:
        recs = list_records_for_session(bw, table, session_id)
        updates = [{"record_id": r["record_id"], "fields": fields} for r in recs]
        batch_update(bw, table, updates)
        print("%s: %d 条" % (name, len(updates)), flush=True)


if __name__ == "__main__":
    main()
