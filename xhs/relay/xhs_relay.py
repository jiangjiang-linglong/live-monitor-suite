#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书直播流实时转封装代理（xhs_relay）

作用：
  小红书直播原画流是 HEVC，但用"旧版 FLV codec id 12"封装，
  自带的精简 ffmpeg 无法识别（会报 Video: none，几秒后录制失败）。
  本代理在本地起一个 HTTP 服务，把小红书 FLV 实时转成
  ffmpeg 能识别的 enhanced FLV（FourCC "hvc1"），录制端只需
  把房间地址写成 http://127.0.0.1:18080/live/<room_id> 即可。

用法：
  python3 xhs_relay.py            # 默认监听 127.0.0.1:18080
  python3 xhs_relay.py 18080      # 指定端口
"""

import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

HOST = "127.0.0.1"
DEFAULT_PORT = 18080

# 小红书原画流地址模板（游客即可拉取，userId=0）
STREAM_TPL = (
    "https://live-source-play-hw.xhscdn.com/live/{room_id}_orig.flv"
    "?userId=0&abr_pts=-3000"
)


# ----------------------------------------------------------------------
# HEVCDecoderConfigurationRecord 处理（小红书是非标准变体，需重建）
# ----------------------------------------------------------------------
def extract_vps_sps_pps(cfg):
    """从小红书非标准 cfg 提取 VPS/SPS/PPS。
    非标准处：general_constraint_indicator_flags 只有 5 字节，
    故 level 在偏移 11（标准为 12）。"""
    idx = 11
    # level(1) min_spatial(2) parallelism(1) chroma(1)
    # bdLuma(1) bdChroma(1) avgFR(2) flags(1)
    idx += 1 + 2 + 1 + 1 + 1 + 1 + 2 + 1
    narr = cfg[idx]
    idx += 1
    out = {}
    for _ in range(narr):
        naltype = cfg[idx] & 0x3F
        idx += 3  # 1字节类型 + 2字节 numNalus
        nl = int.from_bytes(cfg[idx:idx + 2], "big")
        idx += 2
        nalu = cfg[idx:idx + nl]
        idx += nl
        out[naltype] = nalu
    return out


def build_standard_cfg(vps, sps, pps, src):
    """重建为标准 HEVCDecoderConfigurationRecord（constraint 6 字节）。"""
    version = src[0]
    profile = src[1]
    compat = src[2:6]
    constraint5 = src[6:11]
    level = src[11]
    min_spatial = src[12:14]
    parallelism = src[14]
    chroma = src[15]
    bd_luma = src[16]
    bd_chroma = src[17]
    avg_fr = src[18:20]
    flags = src[20]
    constraint = constraint5 + b"\x00"  # 补成标准 6 字节
    out = (
        bytes([version, profile]) + compat + constraint
        + bytes([level]) + min_spatial
        + bytes([parallelism, chroma, bd_luma, bd_chroma])
        + avg_fr + bytes([flags, 3])
    )
    for naltype, nalu in [(32, vps), (33, sps), (34, pps)]:
        out += bytes([naltype]) + b"\x00\x01" + len(nalu).to_bytes(2, "big") + nalu
    return out


# ----------------------------------------------------------------------
# 单个视频 tag 转换
# ----------------------------------------------------------------------
def convert_video_tag(body):
    b0 = body[0]
    pkt = body[1]
    frame = b0 >> 4
    if pkt == 0:
        # 序列头：旧 1c 00 [00 00 00 01] 非标准cfg
        src = body[2 + 4:]
        v = extract_vps_sps_pps(src)
        std_cfg = build_standard_cfg(v[32], v[33], v[34], src)
        return bytes([0x90]) + b"hvc1" + std_cfg
    if pkt == 1:
        # NALU：旧 1c/2c 01 [3字节comp] [NALU...]
        comp3 = body[2:5]
        nalu = body[5:]
        ex = 0x91 if frame == 1 else 0xa1
        return bytes([ex]) + b"hvc1" + comp3 + nalu
    return body


# ----------------------------------------------------------------------
# 流式拉取 + 转封装，产出可直接发给 ffmpeg 的字节
# ----------------------------------------------------------------------
class StreamReader:
    """从 requests 响应流里精确读取指定字节数。"""

    def __init__(self, resp):
        self.resp = resp
        self.buf = b""

    def read(self, n):
        while len(self.buf) < n:
            chunk = next(self.resp.iter_content(chunk_size=8192), None)
            if not chunk:
                break
            self.buf += chunk
        data = self.buf[:n]
        self.buf = self.buf[n:]
        return data

    def leftover(self):
        return self.buf


def relay_stream(room_id, write):
    """拉取一个房间的流并实时转封装，write(bytes) 输出。"""
    url = STREAM_TPL.format(room_id=room_id)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.xiaohongshu.com/",
    }
    with requests.get(url, headers=headers, stream=True, timeout=15) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"上游 HTTP {resp.status_code}")
        reader = StreamReader(resp)

        # FLV 头（9 字节）+ PreviousTagSize0（4 字节）
        flv_header = reader.read(9)
        if flv_header[:3] != b"FLV":
            raise RuntimeError("上游不是 FLV 流")
        flv_header = flv_header[:4] + bytes([flv_header[4] | 0x01]) + flv_header[5:]
        prev0 = reader.read(4)
        write(flv_header + prev0)

        while True:
            tag_header = reader.read(11)
            if len(tag_header) < 11:
                break
            tag_type = tag_header[0]
            dlen = int.from_bytes(tag_header[1:4], "big")
            body = reader.read(dlen)
            if len(body) < dlen:
                break

            if tag_type == 9:
                new_body = convert_video_tag(body)
            else:
                new_body = body  # 音频 / script 直接透传

            nl = len(new_body)
            new_tag = (
                bytes([tag_type]) + nl.to_bytes(3, "big")
                + tag_header[4:7] + tag_header[7:8]
                + b"\x00\x00\x00" + new_body
            )
            write(new_tag)
            write(nl.to_bytes(4, "big"))  # PreviousTagSize

            # 读掉上游的 4 字节 PreviousTagSize
            reader.read(4)


# ----------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默默认访问日志

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "live" or not parts[1].isdigit():
            self.send_error(404, "用法: /live/<room_id>")
            return
        room_id = parts[1]
        self.send_response(200)
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            relay_stream(room_id, self.wfile.write)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            sys.stderr.write(f"[xhs_relay] room {room_id} 结束: {e}\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    print(f"[xhs_relay] 小红书转封装代理已启动: http://{HOST}:{port}/live/<room_id>")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
