#!/bin/bash
# 一次性串行创建「直播爆品监控看板」的全部组件，收集 block_id 到 /tmp/block_ids.txt
set -u
BT="Uc8Qbxvh0aoeYas9p0icAmpInjh"
DASH="blkWUuBjeJqExtiz"
IDFILE="/tmp/block_ids.txt"
: > "$IDFILE"

mk() {
  local name="$1" typ="$2" cfg="$3" pos="$4"
  local out bid
  out=$(lark-cli base +dashboard-block-create --base-token "$BT" --dashboard-id "$DASH" \
    --name "$name" --type "$typ" --data-config "$cfg" --position "$pos" --as user 2>&1)
  bid=$(echo "$out" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if d.get('ok'):
        def f(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == 'block_id' and isinstance(v, str):
                        print(v)
                    f(v)
            elif isinstance(o, list):
                [f(x) for x in o]
        f(d['data'])
    else:
        print('FAIL:' + json.dumps(d, ensure_ascii=False)[:200])
except Exception:
    print('PARSEFAIL')
")
  echo "$name -> $bid"
  echo "$name|$bid" >> "$IDFILE"
}

# 顶部 6 个指标卡（h=6，每个 w=8）
mk "峰值人数" statistics '{"table_name":"场次","series":[{"field_name":"峰值人数","rollup":"MAX"}]}' '{"x":0,"y":0,"w":8,"h":6}'
mk "均值人数" statistics '{"table_name":"场次","series":[{"field_name":"均值人数","rollup":"MAX"}]}' '{"x":8,"y":0,"w":8,"h":6}'
mk "弹幕数" statistics '{"table_name":"互动明细","count_all":true,"filter":{"conjunction":"and","conditions":[{"field_name":"类型","operator":"is","value":"弹幕"}]}}' '{"x":16,"y":0,"w":8,"h":6}'
mk "进场数" statistics '{"table_name":"互动明细","count_all":true,"filter":{"conjunction":"and","conditions":[{"field_name":"类型","operator":"is","value":"进场"}]}}' '{"x":24,"y":0,"w":8,"h":6}'
mk "关注数" statistics '{"table_name":"互动明细","count_all":true,"filter":{"conjunction":"and","conditions":[{"field_name":"类型","operator":"is","value":"关注"}]}}' '{"x":32,"y":0,"w":8,"h":6}'
mk "爆品时刻数" statistics '{"table_name":"爆品时刻","count_all":true}' '{"x":40,"y":0,"w":8,"h":6}'

# 中部分栏：左侧在线人数折线（高图，跨右侧两个矮图）
mk "在线人数曲线" line '{"table_name":"在线人数明细","series":[{"field_name":"当前人数","rollup":"MAX"}],"group_by":[{"field_name":"墙钟时间","mode":"integrated","sort":{"type":"group","order":"asc"}}]}' '{"x":0,"y":6,"w":32,"h":16}'
mk "互动类型分布" pie '{"table_name":"互动明细","count_all":true,"group_by":[{"field_name":"类型","mode":"integrated"}]}' '{"x":32,"y":6,"w":16,"h":8}'
mk "话术词云" wordCloud '{"table_name":"话术词","count_all":true,"group_by":[{"field_name":"命中词","mode":"integrated"}]}' '{"x":32,"y":14,"w":16,"h":8}'

# 底部表格：爆品时刻列表
mk "爆品时刻列表" view '{"table_name":"爆品时刻","view_type":"grid","visible_fields":["序号","开始","结束","信号来源","命中关键词","弹幕原话","人数变化","话术摘要"]}' '{"x":0,"y":22,"w":48,"h":16}'

echo "=== 全部完成，block_id 已写入 $IDFILE ==="
