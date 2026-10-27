#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书应用身份消息/文件发送器
==========================
用 outer_pusher 自建应用（tenant_access_token 自动刷新）发送文本、文件、图片、视频，
不依赖 lark-cli 用户身份，也不依赖豆包/会话窗口。

可在指定群（chat_id）或与某人的单聊（open_id）发送。
"""
import os
import json
import requests

OPEN = "https://open.feishu.cn"
# 应用上传大小限制（飞书 OpenAPI）：普通文件 30MB，视频/图片约 100MB
FILE_LIMIT = 30 * 1024 * 1024
MEDIA_LIMIT = 100 * 1024 * 1024


class AppMessenger:
    def __init__(self, app, logf=print):
        self.app = app
        self.log = logf

    def _token(self):
        return self.app.tenant_access_token()

    def _send(self, receive_id, id_type, msg_type, content):
        r = requests.post(
            OPEN + "/open-apis/im/v1/messages",
            headers=self.app._headers(),
            params={"receive_id_type": id_type},
            data=json.dumps({
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            }, ensure_ascii=False),
            timeout=30,
        )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("发送失败 code=%s msg=%s" % (d.get("code"), d.get("msg")))
        return d["data"].get("message_id")

    def send_text(self, receive_id, text, id_type="chat_id"):
        return self._send(receive_id, id_type, "text", {"text": text})

    def _upload_file(self, path, file_type):
        with open(path, "rb") as f:
            r = requests.post(
                OPEN + "/open-apis/im/v1/files",
                headers={"Authorization": "Bearer " + self._token()},
                files={"file": (os.path.basename(path), f, "application/octet-stream")},
                data={"file_type": file_type, "file_name": os.path.basename(path)},
                timeout=300,
            )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("上传失败 code=%s msg=%s" % (d.get("code"), d.get("msg")))
        return d["data"]["file_key"]

    def _upload_image(self, path):
        with open(path, "rb") as f:
            r = requests.post(
                OPEN + "/open-apis/im/v1/images",
                headers={"Authorization": "Bearer " + self._token()},
                files={"image": f},
                data={"image_type": "message"},
                timeout=120,
            )
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("图片上传失败 code=%s msg=%s" % (d.get("code"), d.get("msg")))
        return d["data"]["image_key"]

    def send_file(self, receive_id, path, id_type="chat_id"):
        """发送普通文件（doc/xls/pdf/ppt/stream），>30MB 飞书会拒绝。"""
        size = os.path.getsize(path)
        if size > FILE_LIMIT:
            raise RuntimeError("文件 %.1fMB 超过应用上传上限 30MB" % (size / 1048576))
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        ftype = {"pdf": "pdf", "doc": "doc", "docx": "doc", "xls": "xls", "xlsx": "xls",
                 "ppt": "ppt", "pptx": "ppt", "mp3": "stream", "wav": "stream",
                 "m4a": "stream", "csv": "stream", "txt": "stream", "md": "stream",
                 "html": "stream"}.get(ext, "stream")
        fk = self._upload_file(path, ftype)
        return self._send(receive_id, id_type, "file", {"file_key": fk})

    def send_video(self, receive_id, path, id_type="chat_id"):
        """发送视频（mp4），>100MB 飞书会拒绝。"""
        size = os.path.getsize(path)
        if size > MEDIA_LIMIT:
            raise RuntimeError("视频 %.1fMB 超过应用上传上限 100MB" % (size / 1048576))
        fk = self._upload_file(path, "mp4")
        return self._send(receive_id, id_type, "media", {"file_key": fk})

    def send_image(self, receive_id, path, id_type="chat_id"):
        ik = self._upload_image(path)
        return self._send(receive_id, id_type, "image", {"image_key": ik})
