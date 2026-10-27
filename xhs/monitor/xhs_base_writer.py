#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书直播数据 → 飞书多维表写入器
================================
用 outer_pusher 自建应用（FeishuApp，tenant_access_token 自动刷新）写入，
不依赖 lark-cli 用户身份，也不依赖豆包/会话窗口。

写入表：
  场次 / 在线人数明细 / 互动明细 / 爆品时刻 / 商品讲解
返回场次 record_id，供各明细关联。
"""
import os
import sys
import time
import json
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 飞书应用凭证目录：可用环境变量 FEISHU_APP_DIR 覆盖，默认取仓库内 feishu/
FEISHU_DIR = os.environ.get("FEISHU_APP_DIR", os.path.join(REPO_ROOT, "feishu"))
sys.path.insert(0, FEISHU_DIR)
from feishu_client import FeishuApp  # noqa

T_SESSION = "tbl9T22NF5kxmmpg"   # 场次
T_VIEWERS = "tblwZpGYkQvTtJhB"   # 在线人数明细
T_EVENTS = "tblruHHI6yfHspnW"    # 互动明细
T_HITS = "tblCmcOpmQma2PP9"      # 爆品时刻
T_GOODS = "tbl3tGy66wuIVh32"     # 商品讲解
T_RANK = "tbl0shgWE5s3eFiS"      # 商品热度榜
T_TALK = "tblz3Jm73jNPhH2B"      # 话术词

OPEN = "https://open.feishu.cn"
BATCH = 200


def _ms(ts):
    return int(ts * 1000)


class BaseWriter:
    def __init__(self, logf=print):
        self.app = FeishuApp()
        self.base = self.app.base_token
        self.log = logf
        self._img_cache = {}

    def _url(self, table, suffix="records"):
        return "%s/open-apis/bitable/v1/apps/%s/tables/%s/%s" % (
            OPEN, self.base, table, suffix)

    def _upload_image(self, url):
        """下载小红书商品图并上传到多维表素材（bitable_image），返回 file_token。
        同一 URL 仅上传一次；失败返回 None（不阻断写入）。"""
        if not url:
            return None
        if url in self._img_cache:
            return self._img_cache[url]
        token = None
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            data = r.content
            ext = ".png"
            ct = r.headers.get("Content-Type", "")
            if "webp" in url or "webp" in ct:
                ext = ".webp"
            elif "jpeg" in ct or "jpg" in url:
                ext = ".jpg"
            elif "gif" in ct:
                ext = ".gif"
            up = requests.post(
                OPEN + "/open-apis/drive/v1/medias/upload_all",
                headers={"Authorization": "Bearer " + self.app.tenant_access_token()},
                data={"file_name": "goods%s" % ext, "parent_type": "bitable_image",
                      "parent_node": self.base, "size": str(len(data))},
                files={"file": ("goods%s" % ext, data, ct or "application/octet-stream")},
                timeout=60)
            d = up.json()
            if d.get("code") == 0:
                token = d["data"]["file_token"]
            else:
                self.log("商品图上传失败: code=%s msg=%s" % (d.get("code"), d.get("msg")))
        except Exception as e:
            self.log("商品图下载/上传异常: %s" % str(e)[:120])
        self._img_cache[url] = token
        return token

    def _img_attach(self, url):
        """附件字段值，token 为空时返回 None。"""
        tok = self._upload_image(url)
        return [{"file_token": tok}] if tok else None

    def _create_one(self, table, fields):
        """创建单条记录，返回 record_id。"""
        r = requests.post(self._url(table), headers=self.app._headers(),
                          data=json.dumps({"fields": fields}, ensure_ascii=False), timeout=20)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("写入 %s 失败 code=%s msg=%s" % (table, d.get("code"), d.get("msg")))
        return d["data"]["record"]["record_id"]

    def _batch_create(self, table, records):
        n = 0
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            r = requests.post(self._url(table, "records/batch_create"),
                              headers=self.app._headers(),
                              data=json.dumps({"records": chunk}, ensure_ascii=False), timeout=30)
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError("批量写入 %s 失败 code=%s msg=%s" %
                                   (table, d.get("code"), d.get("msg")))
            n += len(chunk)
        return n

    def write_all(self, anchor, start_ts, end_ts, online, barrages,
                  surge_moments, goods, explain_segments, cart_signals,
                  event_counts):
        """
        online: [(ts,count,status)]
        barrages: [(ts,who,text)]
        surge_moments: [(ts,base,cnt,gid,gtitle)]
        goods: {gid:{title,image,price,spec,category,shop}}
        explain_segments: [{goods_id,title,start,end}]
        cart_signals: [(ts,desc)]
        """
        # 统计
        vals = [c for (t, c, s) in online if c]
        peak = max(vals) if vals else 0
        avg = round(sum(vals) / len(vals)) if vals else 0
        mins = round((end_ts - start_ts) / 60)
        session_name = "%s_%s" % (anchor, time.strftime("%m%d%H%M", time.localtime(start_ts)))

        # 1) 场次（“主播”是关联字段，需先有主播记录，这里不写，仅在名称里体现）
        fields = {
            "场次名称": session_name,
            "主播名": anchor,
            "日期": _ms(start_ts),
            "开播时间": _ms(start_ts),
            "下播时间": _ms(end_ts),
            "墙钟总时长(分)": mins,
            "峰值人数": peak,
            "均值人数": avg,
            "弹幕数": len(barrages),
            "进场": event_counts.get("audience_join_v2", 0),
            "点赞": event_counts.get("praise", 0),
            "爆品时刻数": len(surge_moments),
            "监测时段说明": "小红书 · 自动监控 · 主播 %s" % anchor,
        }
        sid = self._create_one(T_SESSION, fields)
        self.log("场次记录已建: %s" % sid)
        link = [sid]

        # 2) 在线人数明细
        vrecs = []
        for idx, (ts, cnt, status) in enumerate(online, 1):
            vrecs.append({"fields": {
                "场次": link,
                "主播名": anchor,
                "场次名称": session_name,
                "墙钟时间": _ms(ts),
                "全局秒": round(ts - start_ts, 1),
                "当前人数": cnt if cnt is not None else 0,
                "时间点": time.strftime("%H:%M:%S", time.localtime(ts)),
            }})
        n_v = self._batch_create(T_VIEWERS, vrecs)
        self.log("在线人数明细: %d 条" % n_v)

        # 3) 互动明细（弹幕）；类型为单选 -> 字符串
        erecs = []
        for ts, who, text in barrages:
            erecs.append({"fields": {
                "场次": link,
                "主播名": anchor,
                "场次名称": session_name,
                "时间": _ms(ts),
                "类型": "弹幕",
                "用户": who or "",
                "内容": text or "",
                "是否主播": False,
            }})
        n_e = self._batch_create(T_EVENTS, erecs)
        self.log("互动明细: %d 条" % n_e)

        # 4) 爆品时刻（含对应商品）
        hrecs = []
        for i, (ts, base, cnt, gid, gtitle) in enumerate(surge_moments, 1):
            ratio = round((cnt - base) / base * 100) if base else 0
            hrecs.append({"fields": {
                "序号": i,
                "场次": link,
                "主播名": anchor,
                "场次名称": session_name,
                "开始": _ms(ts),
                "结束": _ms(ts + 60),
                "信号来源": ["人数突增"],
                "命中关键词": gtitle or "无商品讲解",
                "人数变化": "%d→%d（+%d%%）" % (base, cnt, ratio),
                "话术摘要": ("对应商品：%s" % gtitle) if gtitle else "当时无商品讲解信号",
            }})
            if gid:
                img = self._img_attach(goods.get(gid, {}).get("image"))
                if img:
                    hrecs[-1]["fields"]["商品图"] = img
        n_h = self._batch_create(T_HITS, hrecs)
        self.log("爆品时刻: %d 条" % n_h)

        # 5) 商品讲解
        # 统计每个商品的“去买次数”：cart 落在该商品讲解区间
        def cart_count(g):
            st, en = g["start"], (g["end"] or end_ts)
            return sum(1 for (t, d) in cart_signals if st <= t <= en)
        grecs = []
        for seg in explain_segments:
            g = goods.get(seg["goods_id"], {})
            st = seg["start"]
            en = seg["end"] or end_ts
            price = g.get("price")
            row = {
                "商品ID": seg["goods_id"],
                "商品标题": seg["title"] or g.get("title", ""),
                "到手价": float(price) if price not in (None, "") else 0,
                "规格": g.get("spec", ""),
                "商品分类": g.get("category", ""),
                "讲解开始": _ms(st),
                "讲解结束": _ms(en),
                "讲解时长(秒)": round(en - st),
                "去买次数": cart_count(seg),
                "场次": link,
                "主播名": anchor,
                "场次名称": session_name,
            }
            if g.get("image"):
                row["商品主图"] = {"link": g["image"], "text": "商品主图"}
                img = self._img_attach(g["image"])
                if img:
                    row["商品图"] = img
            grecs.append({"fields": row})
        n_g = self._batch_create(T_GOODS, grecs)
        self.log("商品讲解: %d 条" % n_g)

        # 6) 商品热度榜（按商品聚合讲解/购买/弹幕，加权排名）
        rank_rows = self.build_rank(goods, explain_segments, cart_signals,
                                    barrages, online, end_ts, link,
                                    anchor, session_name)
        n_r = self._batch_create(T_RANK, rank_rows)
        self.log("商品热度榜: %d 条" % n_r)

        return {"session_id": sid, "viewers": n_v, "events": n_e,
                "hits": n_h, "goods": n_g, "rank": n_r}

    def build_rank(self, goods, explain_segments, cart_signals, barrages,
                   online, end_ts, link, anchor=None, session_name=None):
        # 收集所有出现过的商品（讲解段里的 + goods 里的）
        gids = []
        for seg in explain_segments:
            if seg["goods_id"] not in gids:
                gids.append(seg["goods_id"])
        for gid in goods:
            if gid not in gids:
                gids.append(gid)

        # 人数按时间升序，便于查区间峰值
        ol = sorted(online, key=lambda x: x[0])

        def count_in_windows(windows, items):
            return sum(1 for it in items
                       if any(a <= it[0] <= b for a, b in windows))

        def peak_in_windows(windows):
            vals = [c for (t, c, s) in ol
                    if c is not None and any(a <= t <= b for a, b in windows)]
            return max(vals) if vals else 0

        agg = []
        for gid in gids:
            g = goods.get(gid, {})
            segs = [s for s in explain_segments if s["goods_id"] == gid]
            windows = [(s["start"], s["end"] or end_ts) for s in segs]
            total_dur = sum(en - st for st, en in windows)
            carts = count_in_windows(windows, cart_signals)
            danmu = count_in_windows(windows, barrages)
            peak = peak_in_windows(windows)
            # 热度分：购买信号权重最高，其次弹幕、讲解时长、讲解次数
            score = carts * 10 + danmu * 3 + total_dur * 0.1 + len(segs) * 2
            title = (segs[0]["title"] if segs and segs[0].get("title")
                     else g.get("title", ""))
            agg.append({"gid": gid, "g": g, "title": title, "times": len(segs),
                        "dur": round(total_dur), "carts": carts, "danmu": danmu,
                        "peak": peak, "score": round(score)})
        agg.sort(key=lambda x: -x["score"])

        rows = []
        for i, a in enumerate(agg, 1):
            price = a["g"].get("price")
            row = {
                "排名": i,
                "商品ID": a["gid"],
                "商品标题": a["title"],
                "到手价": float(price) if price not in (None, "") else 0,
                "讲解次数": a["times"],
                "总讲解时长(秒)": a["dur"],
                "购买信号数": a["carts"],
                "相关弹幕数": a["danmu"],
                "讲解期峰值人数": a["peak"],
                "热度分": a["score"],
                "场次": link,
                "主播名": anchor,
                "场次名称": session_name,
            }
            if a["g"].get("image"):
                row["商品主图"] = {"link": a["g"]["image"], "text": "商品主图"}
                img = self._img_attach(a["g"]["image"])
                if img:
                    row["商品图"] = img
            rows.append({"fields": row})
        return rows

    def write_minutes(self, session_id, minute_hits, start_ts, anchor=None):
        """话术命中段写入「话术词」表。
        minute_hits: [{'sec':录制相对秒,'keywords':[...],'text':...}]
        时间戳 = 录制开始时间 + 相对秒。"""
        link = [session_id]
        session_name = ("%s_%s" % (anchor, time.strftime("%m%d%H%M", time.localtime(start_ts)))) if anchor else ""
        rows = []
        for h in minute_hits:
            wall = start_ts + int(h["sec"])
            rows.append({"fields": {
                "场次": link,
                "主播名": anchor,
                "场次名称": session_name,
                "时间": _ms(wall),
                "类型": "话术",
                "命中词": "/".join(h.get("keywords", [])),
                "原句": h.get("text", ""),
            }})
        n = self._batch_create(T_TALK, rows)
        self.log("话术词: %d 条" % n)
        return n
