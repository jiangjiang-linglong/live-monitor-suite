# encoding: utf-8
"""手动截止并交付一场监控。

用法（参数与 xhs_live_pipeline.py 完全一致）:
  ./.venv/bin/python cut_deliver.py <room_id> <host_id> <主播名> <输出目录> <webhook> [chat_id] [lark_cli]

场景：直播未下播但需要提前收尾。先停掉该房间的 ffmpeg/pipeline，再运行本脚本：
它会从输出目录的 signal_state.json 恢复全部已采集信号，执行与正常下播完全相同的
交付流程（CSV/报告/曲线图、多维表、妙记转写、群卡片、文件推群）。
"""
import os
import sys

# 把 pipeline 的 argv 就位后再导入（模块级全局从 argv 读取）
sys.argv = ['xhs_live_pipeline.py'] + sys.argv[1:]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xhs_live_pipeline as xp

xp.RECOVERY_PATH = os.path.join(xp.OUTDIR, 'signal_state.json')

if not os.path.exists(xp.RECOVERY_PATH):
    print('未找到 %s，无法恢复信号，已中止' % xp.RECOVERY_PATH)
    sys.exit(1)


def main():
    cookie = open(xp.COOKIE_PATH, encoding='utf-8').read().strip()
    auth = xp.XHSUnifiedAuth.from_cookie(cookie)
    live = xp.XHSLiveAPI(auth.pc)
    p = xp.Pipeline(live, auth=auth)
    # 收尾仍在讲解中的商品段
    for seg in p.explain_segments:
        if seg.get('end') is None:
            seg['end'] = __import__('time').time()
    print('手动截止交付：%s（%s），已恢复弹幕%d 人数样本%d 商品%d 讲解段%d' %
          (xp.ANCHOR, xp.ROOM_ID, len(p.barrages), len(p.online),
           len(p.goods), len(p.explain_segments)))
    try:
        p.deliver()
    finally:
        auth.close()


if __name__ == '__main__':
    main()
