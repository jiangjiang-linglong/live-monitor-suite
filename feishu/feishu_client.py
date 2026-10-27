#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书企业自建应用直连客户端
==========================
用 app_id / app_secret 自行获取并刷新 tenant_access_token，直接调用飞书开放平台
HTTP API 读写多维表格，不依赖 lark-cli，也不依赖任何会话环境变量。

凭证文件：config_sync/feishu_app.json（权限 600）
{
  "app_id": "cli_xxxxxxxx",
  "app_secret": "xxxxxxxx",
  "base_token": "Uc8Qbxvh0aoeYas9p0icAmpInjh"
}

前置条件：
  1. 应用已开通多维表格权限（bitable:app）；
  2. 应用已被「添加文档应用」为目标多维表格的协作者。
"""
import json
import os
import time

import requests

OPEN_HOST = "https://open.feishu.cn"
APP_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feishu_app.json")


class FeishuAppError(RuntimeError):
    pass


class FeishuApp:
    def __init__(self, config_path=APP_CONFIG_PATH):
        if not os.path.exists(config_path):
            raise FeishuAppError(
                "缺少凭证文件 %s；请先创建飞书自建应用并填入 app_id/app_secret" % config_path
            )
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.app_id = cfg.get("app_id", "").strip()
        self.app_secret = cfg.get("app_secret", "").strip()
        self.base_token = cfg.get("base_token", "").strip()
        if not (self.app_id and self.app_secret and self.base_token):
            raise FeishuAppError("feishu_app.json 需包含 app_id、app_secret、base_token")
        self._token = None
        self._token_expire_at = 0.0

    # ---------------- 鉴权 ----------------
    def tenant_access_token(self):
        """获取 tenant_access_token，内存缓存，过期前 5 分钟自动刷新。"""
        if self._token and time.time() < self._token_expire_at - 300:
            return self._token
        resp = requests.post(
            OPEN_HOST + "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuAppError(
                "获取 tenant_access_token 失败：code=%s msg=%s" % (data.get("code"), data.get("msg"))
            )
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200))
        return self._token

    def _headers(self):
        return {"Authorization": "Bearer " + self.tenant_access_token(),
                "Content-Type": "application/json; charset=utf-8"}

    def _check(self, data, action):
        if data.get("code") != 0:
            raise FeishuAppError(
                "%s失败：code=%s msg=%s" % (action, data.get("code"), data.get("msg"))
            )

    # ---------------- 多维表格记录 ----------------
    def list_records(self, table_id, page_size=100):
        """分页列出某数据表全部记录（原生富结构）。"""
        url = "%s/open-apis/bitable/v1/apps/%s/tables/%s/records" % (
            OPEN_HOST, self.base_token, table_id)
        items, page_token, has_more = [], None, True
        while has_more:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            data = resp.json()
            self._check(data, "列出记录")
            d = data.get("data", {})
            items.extend(d.get("items", []))
            has_more = bool(d.get("has_more"))
            page_token = d.get("page_token")
        return items

    def batch_update_records(self, table_id, records):
        """批量更新。records: [{"record_id": "...", "fields": {字段名: 值}}]"""
        if not records:
            return
        url = "%s/open-apis/bitable/v1/apps/%s/tables/%s/records/batch_update" % (
            OPEN_HOST, self.base_token, table_id)
        resp = requests.post(url, headers=self._headers(),
                             data=json.dumps({"records": records}, ensure_ascii=False), timeout=30)
        self._check(resp.json(), "批量更新")

    def batch_create_records(self, table_id, records):
        """批量创建。records: [{"fields": {字段名: 值}}]"""
        if not records:
            return
        url = "%s/open-apis/bitable/v1/apps/%s/tables/%s/records/batch_create" % (
            OPEN_HOST, self.base_token, table_id)
        resp = requests.post(url, headers=self._headers(),
                             data=json.dumps({"records": records}, ensure_ascii=False), timeout=30)
        self._check(resp.json(), "批量创建")
