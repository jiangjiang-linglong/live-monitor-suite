# encoding: utf-8
"""小红书直播完整监控流水线：录制(外部ffmpeg) + 弹幕/人数采集 + 飞书群实时推送 + 下播交付。

用法:
  ./.venv/bin/python -u xhs_live_pipeline.py <room_id> <host_id> <主播名> <输出目录> <webhook>
"""
import sys, asyncio, time, json, os, re
import requests
from collections import defaultdict
from urllib import request as urlreq
sys.path.insert(0, '.')
from xhs_utils.xhs_auth import XHSUnifiedAuth
from apis.xhs_live import XHSLiveAPI

POLL_INTERVAL = 5
HEARTBEAT_INTERVAL = 15
STALE_TIMEOUT = 180
MAX_BACKOFF = 60
SUMMARY_EVERY = 300          # 每 5 分钟群内汇总一次
SURGE_WINDOW = 60            # 人数突增检测窗口（秒）
SURGE_RATIO = 0.25           # 1 分钟内人数上涨 25% 视为突增
OFFLINE_CONFIRM = 8          # 连续 8 次（40s）下播状态才确认

ROOM_ID = sys.argv[1]
HOST_ID = sys.argv[2]
ANCHOR = sys.argv[3]
OUTDIR = sys.argv[4]
WEBHOOK = sys.argv[5]
CHAT_ID = sys.argv[6] if len(sys.argv) > 6 else ''
LARK_CLI = sys.argv[7] if len(sys.argv) > 7 else 'lark-cli'
COOKIE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'login_cookies.txt')
COOKIE_BAK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'login_cookies.bak')
# 登录态失效签名（已实测，见 xhs_watchdog.HealthProbe）
AUTH_CODES = (-100, -101)
AUTH_KEYWORDS = ('登录已过期', '无登录信息', '登录信息为空', '未登录', '请登录', '471')
RISK_MARKERS = ('getdss', '安全验证', '验证码')
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 飞书应用凭证目录：可用环境变量 FEISHU_APP_DIR 覆盖，默认取仓库内 feishu/
CONFIG_SYNC_DIR = os.environ.get("FEISHU_APP_DIR", os.path.join(REPO_ROOT, "feishu"))
FFMPEG_BIN = os.path.expanduser("~/.local/bin/ffmpeg")
if not os.path.exists(FFMPEG_BIN):
    FFMPEG_BIN = "ffmpeg"
# 视频文件大小超过该值（MB）不直接发群，仅存档（飞书 IM 文件有大小限制）
VIDEO_SEND_LIMIT_MB = 100
# 信号数据持久化文件（重启 pipeline 时恢复，避免丢失已采集的弹幕/商品/人数）
RECOVERY_PATH = None  # main 中根据 OUTDIR 设置


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def push_card(title, template, elements):
    payload = {"msg_type": "interactive",
               "card": {"config": {"wide_screen_mode": True},
                        "header": {"template": template,
                                   "title": {"tag": "plain_text", "content": title}},
                        "elements": elements}}
    data = json.dumps(payload).encode('utf-8')
    req = urlreq.Request(WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlreq.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except Exception as e:
        log('推送失败: %s' % e)
        return False


BASE_TOKEN = "Uc8Qbxvh0aoeYas9p0icAmpInjh"
BASE_URL = "https://pcnqjzdrux50.feishu.cn/base/%s?table=tbl9T22NF5kxmmpg" % BASE_TOKEN
_IMG_KEY_CACHE = {}


def goods_img_element(image_url):
    """把商品图上传到飞书 IM 图床，返回卡片 img 元素；失败返回 None。同一 URL 缓存。"""
    if not image_url:
        return None
    if image_url in _IMG_KEY_CACHE:
        key = _IMG_KEY_CACHE[image_url]
        return {"tag": "img", "img_key": key,
                "alt": {"tag": "plain_text", "content": "商品图"}} if key else None
    key = None
    try:
        sys.path.insert(0, CONFIG_SYNC_DIR)
        from feishu_client import FeishuApp
        r = requests.get(image_url, timeout=15)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', 'image/webp')
        up = requests.post('https://open.feishu.cn/open-apis/im/v1/images',
                           headers={'Authorization': 'Bearer ' + FeishuApp().tenant_access_token()},
                           data={'image_type': 'message'},
                           files={'image': ('goods.webp', r.content, ct)}, timeout=30)
        d = up.json()
        if d.get('code') == 0:
            key = d['data']['image_key']
    except Exception as e:
        log('商品图卡片上传失败: %s' % str(e)[:100])
    _IMG_KEY_CACHE[image_url] = key
    if key:
        return {"tag": "img", "img_key": key,
                "alt": {"tag": "plain_text", "content": "商品图"}}
    return None


def md(content):
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


class Pipeline:
    def __init__(self, live, auth=None):
        self.live = live
        self.auth = auth              # XHSUnifiedAuth（cookie 续期回写用）
        self.barrages = []          # (ts, who, text)
        self.online = []            # (ts, count, status)
        self.event_counts = defaultdict(int)
        self.ws_reconnects = 0
        self.ws = None
        self.stop = False
        self.last_event = time.time()
        self.last_summary = time.time()
        self.last_surge_alert = 0
        self.auth_fail = 0
        self.risk_fail = 0          # 风控/安全验证
        self.api_fail = 0           # 其他接口异常（结构漂移/非 0 code）
        self.net_fail = 0           # 网络抖动
        self.last_poll_error = ''
        self.alerts_sent = {}       # 告警去重 key -> ts
        self.offline_count = 0
        self.start_ts = time.time()
        # 商品（带货）状态
        self.goods = {}              # goods_id -> {title,image,price,spec,category,shop}
        self.explaining = None       # 当前讲解中的 goods_id
        self.explain_segments = []   # {goods_id,title,start,end}
        self.cart_signals = []       # (ts, desc) “正在去买”
        self.surge_moments = []      # (ts, base, cnt, goods_id, title)
        self.recover()

    def recover(self):
        """从持久化文件恢复已采集信号（重启 pipeline 不丢数据）。"""
        if not RECOVERY_PATH or not os.path.exists(RECOVERY_PATH):
            return
        try:
            s = json.load(open(RECOVERY_PATH, encoding='utf-8'))
            self.barrages = [tuple(x) for x in s.get('barrages', [])]
            self.online = [tuple(x) for x in s.get('online', [])]
            self.goods = s.get('goods', {})
            self.explain_segments = s.get('explain_segments', [])
            self.cart_signals = [tuple(x) for x in s.get('cart_signals', [])]
            self.surge_moments = [tuple(x) for x in s.get('surge_moments', [])]
            self.start_ts = s.get('start_ts', self.start_ts)
            # 恢复当前讲解状态
            if self.explain_segments and self.explain_segments[-1]['end'] is None:
                self.explaining = self.explain_segments[-1]['goods_id']
            log('已恢复信号：弹幕%d 人数样本%d 商品%d 讲解段%d 购买信号%d' %
                (len(self.barrages), len(self.online), len(self.goods),
                 len(self.explain_segments), len(self.cart_signals)))
        except Exception as e:
            log('恢复失败: %s' % e)

    def persist(self):
        """把当前信号写到持久化文件（原子替换）。"""
        if not RECOVERY_PATH:
            return
        try:
            s = {'barrages': self.barrages, 'online': self.online, 'goods': self.goods,
                 'explain_segments': self.explain_segments, 'cart_signals': self.cart_signals,
                 'surge_moments': self.surge_moments, 'start_ts': self.start_ts}
            tmp = RECOVERY_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False)
            os.replace(tmp, RECOVERY_PATH)
        except Exception as e:
            log('持久化失败: %s' % e)

    def _extract_goods(self, custom):
        """从 live_goods_card / live_goods_card_new / goods_card 事件提取商品信息。"""
        goods_info = custom.get('goods_info') or {}
        items_info = custom.get('items_info') or {}
        gvi = items_info.get('goods_view_info') or {}
        shop = items_info.get('shop_info') or {}
        link = goods_info.get('link') or ''
        gid = goods_info.get('goods_id')
        if not gid:
            m = re.search(r'mini_goods_detail/([0-9a-f]+)', link)
            if m: gid = m.group(1)
        if not gid: return None
        title = gvi.get('title') or goods_info.get('title') or ''
        image = gvi.get('image') or goods_info.get('image') or ''
        price = goods_info.get('discount_price')
        if price is None: price = goods_info.get('price')
        spec = gvi.get('specifications_desc') or ''
        cats = gvi.get('goods_categories') or []
        category = cats[0].get('name') if cats else ''
        rec = self.goods.setdefault(gid, {})
        rec.update({k: v for k, v in {
            'title': title, 'image': image, 'price': price,
            'spec': spec, 'category': category,
            'shop': shop.get('seller_id') or '', 'link': link,
        }.items() if v not in (None, '', [])})
        return gid

    def _on_goods_card(self, custom):
        gid = self._extract_goods(custom)
        if not gid: return
        goods_info = custom.get('goods_info') or {}
        items_info = custom.get('items_info') or {}
        gvi = items_info.get('goods_view_info') or {}
        sub = gvi.get('subtitle') or {}
        is_explain = goods_info.get('is_explain') or (1 if sub.get('in_the_review') else 0)
        title = self.goods.get(gid, {}).get('title', '')
        if is_explain and self.explaining != gid:
            # 开始讲解新商品：先关闭上一段
            if self.explaining:
                self._close_explain(time.time())
            self.explaining = gid
            self.explain_segments.append({'goods_id': gid, 'title': title,
                                          'start': time.time(), 'end': None})
            log('开始讲解: %s' % (title or gid))

    def _close_explain(self, ts):
        if self.explaining and self.explain_segments and self.explain_segments[-1]['end'] is None:
            self.explain_segments[-1]['end'] = ts
        self.explaining = None

    def goods_at(self, ts):
        """返回某时刻正在讲解的商品 (goods_id, title)，没有则 (None, None)。"""
        for seg in self.explain_segments:
            if seg['start'] <= ts and (seg['end'] is None or ts <= seg['end']):
                return seg['goods_id'], seg['title']
        return None, None

    async def status_loop(self):
        """每 30 秒写一次 status.json，供无人值守时确认系统在工作（不依赖 stdout）。"""
        while not self.stop:
            try:
                vals = [c for (t, c, s) in self.online if c is not None]
                snap = {
                    '更新时间': time.strftime('%Y-%m-%d %H:%M:%S'),
                    '主播': ANCHOR, 'room_id': ROOM_ID,
                    '已监控分钟': round((time.time() - self.start_ts) / 60),
                    '当前在线': vals[-1] if vals else None,
                    '峰值在线': max(vals) if vals else None,
                    '弹幕总数': len(self.barrages),
                    '人数样本数': len(self.online),
                    'WS重连次数': self.ws_reconnects,
                    'WS事件秒龄': round(time.time() - self.last_event),
                    '鉴权失败次数': self.auth_fail,
                    '风控失败次数': self.risk_fail,
                    '接口异常次数': self.api_fail,
                    '最近轮询错误': self.last_poll_error,
                    '事件统计': dict(self.event_counts),
                    '最近弹幕': [b[2] for b in self.barrages[-10:]],
                }
                with open(os.path.join(OUTDIR, 'status.json'), 'w', encoding='utf-8') as f:
                    json.dump(snap, f, ensure_ascii=False, indent=1)
                self.persist()
                self.persist_cookie()
            except Exception as e:
                log('状态快照失败: %s' % e)
            await asyncio.sleep(30)

    # ---------- WS ----------
    async def ws_loop(self):
        backoff = 1
        while not self.stop:
            hb = None
            try:
                self.ws = await self.live.connect_push_from_storage(room_id=ROOM_ID)
                log('WS 已连接')
                backoff = 1
                self.auth_fail = 0
                hb = asyncio.create_task(self.heartbeat(self.ws))
                async for frame in self.ws.events():
                    self.consume(frame)
                log('WS 事件流结束')
            except asyncio.CancelledError:
                if hb: hb.cancel()
                raise
            except Exception as e:
                self.ws_reconnects += 1
                msg = str(e)
                log('WS 异常: %s: %s' % (type(e).__name__, msg[:100]))
                if any(k in msg for k in ('471', '登录', '未登录', 'login', '401', '403')):
                    self.auth_fail += 1
                    if self.auth_fail >= 3:
                        self.alert_auth()
                elif any(k in msg for k in ('getdss', '安全验证', '验证码')):
                    self.risk_fail += 1
                    if self.risk_fail >= 2:
                        self.alert_risk(msg[:100])
            finally:
                if hb: hb.cancel()
                if self.ws:
                    try: await self.ws.close()
                    except Exception: pass
            if self.stop: break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def heartbeat(self, ws):
        while not self.stop:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if time.time() - self.last_event > STALE_TIMEOUT:
                try: await ws.close()
                except Exception: pass
                return
            try:
                await ws.send_transport_heartbeat()
                await ws.send_state_sync()
            except Exception:
                pass

    def consume(self, frame):
        if not isinstance(frame, dict): return
        events = (frame.get('decoded') or {}).get('room') or []
        if events:
            self.last_event = time.time()
        for ev in events:
            etype = str(ev.get('event_type') or '?')
            self.event_counts[etype] += 1
            payload = ev.get('payload') or {}
            custom = payload.get('customData')
            custom = custom if isinstance(custom, dict) else {}
            if etype == 'text':
                who = (custom.get('profile') or {}).get('nickname') or '?'
                text = custom.get('desc', '')
                self.barrages.append((time.time(), who, text))
            elif etype in ('live_goods_card', 'live_goods_card_new', 'goods_card'):
                self._on_goods_card(custom)
            elif etype == 'goods_explain_finish':
                self._close_explain(time.time())
                log('商品讲解结束')
            elif etype == 'batch_cart':
                self.cart_signals.append((time.time(), custom.get('desc', '')))
            elif etype == 'live_goods_list_change':
                # 商品列表上下架/排序变化，商品明细仍由 goods_card 推送
                pass

    # ---------- 人数轮询 + 下播检测 + 定时汇总 ----------
    async def poll_loop(self):
        while not self.stop:
            try:
                r = self.live.current_room_info(ROOM_ID)
                kind, detail = self.classify_response(r)
                now = time.time()
                if kind != 'ok':
                    self._handle_poll_fault(kind, detail)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                self.net_fail = 0
                info = (r.get('data') or {}).get('room_info') or {}
                raw_cnt = info.get('display_count', info.get('display_member_count'))
                try:
                    cnt = int(raw_cnt) if raw_cnt is not None and str(raw_cnt).strip() != '' else None
                except (ValueError, TypeError):
                    cnt = None
                status = info.get('status')
                self.online.append((now, cnt, status))
                # 下播判定：小红书在播 status=2，其余（如3）为下播
                if self._looks_offline(status, cnt):
                    self.offline_count += 1
                    if self.offline_count >= OFFLINE_CONFIRM:
                        log('确认直播已结束 status=%s' % status)
                        self.stop = True
                        return
                else:
                    self.offline_count = 0
                self.check_surge(now, cnt)
                if now - self.last_summary >= SUMMARY_EVERY:
                    self.periodic_summary(now)
                    self.last_summary = now
            except Exception as e:
                self._handle_poll_exception(e)
            await asyncio.sleep(POLL_INTERVAL)

    def _handle_poll_fault(self, kind, detail):
        """确定性故障（非网络）：连续计数后分级告警。"""
        self.last_poll_error = '%s: %s' % (kind, detail)
        log('轮询故障 %s: %s' % (kind, detail))
        if kind == 'auth':
            self.auth_fail += 1
            if self.auth_fail >= 3:
                self.alert_auth()
        elif kind == 'risk':
            self.risk_fail += 1
            if self.risk_fail >= 2:
                self.alert_risk(detail)
        else:
            self.api_fail += 1
            if self.api_fail >= 6:
                self.alert_api(detail)

    def _handle_poll_exception(self, e):
        name = type(e).__name__
        msg = str(e)
        # JSON 解析失败通常是风控 JS/HTML 页；curl 类异常是网络问题
        if isinstance(e, ValueError):
            if any(k in msg for k in ('getdss', '安全验证', '验证码')):
                self._handle_poll_fault('risk', msg[:100])
            else:
                self._handle_poll_fault('api', '非 JSON 响应 %s' % msg[:80])
        elif name in ('CurlError', 'CurlHttp2Error', 'RecvError', 'Timeout') or \
                any(k in msg.lower() for k in ('timeout', 'timed out', 'connection', 'dns', 'resolve')):
            self.net_fail += 1
            self.last_poll_error = 'net: %s' % msg[:80]
            if self.net_fail % 12 == 0:
                log('轮询连续网络异常 %d 次: %s' % (self.net_fail, msg[:80]))
        else:
            log('轮询异常: %s: %s' % (name, msg[:80]))

    def _looks_offline(self, status, cnt):
        # 小红书：在播 status=2；下播为其他值（如3）且人数为空/0
        if str(status) == '2': return False
        return cnt in (None, 0)

    def check_surge(self, now, cnt):
        if not cnt: return
        past = now - SURGE_WINDOW
        base = [c for (t, c, s) in self.online if t >= past and c]
        if len(base) >= 4:
            b = min(base)
            if b > 0 and (cnt - b) / b >= SURGE_RATIO and now - self.last_surge_alert > 180:
                self.last_surge_alert = now
                gid, gtitle = self.goods_at(now)
                self.surge_moments.append((now, b, cnt, gid, gtitle))
                goods_line = ('**当前讲解商品：** %s' % gtitle) if gtitle else \
                             '**当前讲解商品：** 暂无商品讲解信号（可能非带货时段）'
                els = [
                    md('**%s** 直播间 %d 秒内人数 **%d → %d**（+%d%%）' %
                       (ANCHOR, SURGE_WINDOW, b, cnt, round((cnt - b) / b * 100))),
                    md(goods_line),
                ]
                img_url = self.goods.get(gid, {}).get('image') if gid else None
                img_el = goods_img_element(img_url)
                if img_el:
                    els.append(img_el)
                elif img_url:
                    els.append({"tag": "button", "text": {"tag": "plain_text", "content": "查看商品图"},
                                "type": "default", "url": img_url})
                els.append(md('**近期弹幕：**\n' +
                              ('\n'.join('· %s' % x[2] for x in self.barrages[-8:] if x[2]) or '暂无')))
                push_card('📈 人数突增 · 疑似爆品时刻', 'orange', els)

    def periodic_summary(self, now):
        vals = [c for (t, c, s) in self.online if c]
        window_barrages = [x for x in self.barrages if x[0] >= now - SUMMARY_EVERY]
        push_card('📊 %s · 阶段汇总' % ANCHOR, 'blue', [
            md('已监控 **%d 分钟**　当前在线 **%s**　人数区间 **%s~%s**' %
               (round((now - self.start_ts) / 60), vals[-1] if vals else '-',
                min(vals) if vals else '-', max(vals) if vals else '-')),
            md('近 5 分钟弹幕 **%d 条**　累计弹幕 **%d 条**　WS重连 **%d 次**' %
               (len(window_barrages), len(self.barrages), self.ws_reconnects))
        ])

    def alert_once(self, key, title, template, elements, repeat=1800):
        """同 key 告警限时去重，避免断线重连时刷屏。"""
        now = time.time()
        if now - self.alerts_sent.get(key, 0) < repeat:
            return
        self.alerts_sent[key] = now
        push_card(title, template, elements)

    def alert_auth(self):
        self.alert_once('auth', '🔴 小红书登录态失效 · 需要重新扫码', 'red', [
            md('**%s** 监控连续多次鉴权失败，弹幕/人数/商品信号可能中断。\n\n'
               '请在 Spider_XHS 目录运行 `./.venv/bin/python do_login.py` 重新扫码'
               '（可能含风控码两步），替换 `login_cookies.txt` 后重启监控；'
               '视频录制（游客拉流）不受影响。' % ANCHOR)
        ])

    def alert_risk(self, detail):
        self.alert_once('risk', '🔴 小红书触发风控/安全验证', 'red', [
            md('**%s** 接口返回风控挑战：%s' % (ANCHOR, detail)),
            md('可能需要 App 扫码完成风控验证；录像链路独立，不受影响。')
        ])

    def alert_api(self, detail):
        self.alert_once('api', '🟡 小红书接口疑似变更', 'orange', [
            md('**%s** 接口连续返回异常：%s' % (ANCHOR, detail)),
            md('若持续出现，说明非官方接口字段/签名可能变更，需要跟进。')
        ])

    @staticmethod
    def _cookie_kv(header):
        out = {}
        for item in (header or '').split(';'):
            item = item.strip()
            if '=' in item:
                k, v = item.split('=', 1)
                out[k.strip()] = v.strip()
        return out

    def persist_cookie(self):
        """把服务端续发的 cookie（含 web_session 续期）原子回写 login_cookies.txt。

        常规响应只续 host-only 的 acw_tc（不在共享头里）；一旦 web_session/
        id_token 被轮换，这里保证重启后仍使用新值。"""
        if not self.auth:
            return
        try:
            jar = self.auth.pc.cookies
            if not jar:
                return
            # curl_cffi 的 Cookies 是 name→value 的映射；排除 host-only 的 acw_tc
            if hasattr(jar, 'items'):
                header = '; '.join('%s=%s' % (k, v) for k, v in jar.items()
                                   if k and k != 'acw_tc')
            else:
                header = str(jar)
            if not header:
                return
            old = ''
            if os.path.exists(COOKIE_PATH):
                old = open(COOKIE_PATH, encoding='utf-8').read().strip()
            if self._cookie_kv(old) == self._cookie_kv(header):
                return
            if old:
                import shutil
                shutil.copyfile(COOKIE_PATH, COOKIE_BAK_PATH)
            tmp = COOKIE_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(header.strip() + '\n')
            os.chmod(tmp, 0o600)
            os.replace(tmp, COOKIE_PATH)
            log('cookie 已续期回写')
        except Exception as e:
            log('cookie 回写失败: %s' % e)

    def classify_response(self, resp):
        """对轮询响应做登录态/风控/结构分类，返回 (kind, detail)。
        kind: ok / auth / risk / api"""
        if isinstance(resp, str):
            low = resp
            if any(k in low for k in RISK_MARKERS):
                return 'risk', '非 JSON 风控页（getdss/安全验证）'
            return 'api', '非 JSON 响应：%s' % resp[:80]
        if not isinstance(resp, dict):
            return 'api', '响应类型异常 %s' % type(resp).__name__
        code = resp.get('code')
        msg = str(resp.get('msg') or '')
        if code in AUTH_CODES or any(k in msg for k in AUTH_KEYWORDS):
            return 'auth', 'code=%s %s' % (code, msg)
        if any(k in msg for k in RISK_MARKERS):
            return 'risk', msg[:100]
        if code not in (0, None):
            return 'api', 'code=%s %s' % (code, msg[:100])
        if not isinstance(resp.get('data'), dict):
            return 'api', 'data 字段缺失'
        return 'ok', ''

    # ---------- 交付物 ----------
    def deliver(self):
        os.makedirs(OUTDIR, exist_ok=True)
        # 弹幕 CSV
        with open(os.path.join(OUTDIR, '弹幕记录.csv'), 'w', encoding='utf-8-sig') as f:
            f.write('时间,昵称,弹幕内容\n')
            for ts, who, text in self.barrages:
                f.write('%s,%s,%s\n' % (time.strftime('%H:%M:%S', time.localtime(ts)),
                                        who.replace(',', '，'), text.replace(',', '，')))
        # 人数 CSV
        with open(os.path.join(OUTDIR, '在线人数.csv'), 'w', encoding='utf-8-sig') as f:
            f.write('时间戳,时间,在线人数,状态\n')
            for ts, cnt, status in self.online:
                f.write('%d,%s,%s,%s\n' % (ts, time.strftime('%H:%M:%S', time.localtime(ts)),
                                           cnt if cnt is not None else '', status))
        # 商品讲解时间线 CSV
        with open(os.path.join(OUTDIR, '商品讲解时间线.csv'), 'w', encoding='utf-8-sig') as f:
            f.write('商品ID,商品标题,讲解开始,讲解结束,讲解时长(秒),到手价,规格\n')
            for seg in self.explain_segments:
                g = self.goods.get(seg['goods_id'], {})
                end = seg['end'] or time.time()
                f.write('%s,%s,%s,%s,%s,%s,%s\n' % (
                    seg['goods_id'], (seg['title'] or '').replace(',', '，'),
                    time.strftime('%H:%M:%S', time.localtime(seg['start'])),
                    time.strftime('%H:%M:%S', time.localtime(end)),
                    round(end - seg['start']),
                    g.get('price', ''), (g.get('spec') or '').replace(',', '，')))
        # 爆品时刻对应商品 CSV
        with open(os.path.join(OUTDIR, '爆品时刻-对应商品.csv'), 'w', encoding='utf-8-sig') as f:
            f.write('时间,人数前值,人数峰值,涨幅,对应商品ID,对应商品标题\n')
            for ts, base, cnt, gid, gtitle in self.surge_moments:
                f.write('%s,%s,%s,%s%%,%s,%s\n' % (
                    time.strftime('%H:%M:%S', time.localtime(ts)), base, cnt,
                    round((cnt - base) / base * 100) if base else '',
                    gid or '', (gtitle or '').replace(',', '，')))
        self.chart()
        report = self.report()
        with open(os.path.join(OUTDIR, '监控总结报告.md'), 'w', encoding='utf-8') as f:
            f.write(report)
        # 写入飞书多维表（outer_pusher 自建应用，不依赖 lark-cli / 豆包窗口）
        base_summary = ''
        session_id = ''
        bw = None
        try:
            from xhs_base_writer import BaseWriter
            bw = BaseWriter(logf=log)
            res = bw.write_all(
                ANCHOR, self.start_ts, time.time(), self.online, self.barrages,
                self.surge_moments, self.goods, self.explain_segments,
                self.cart_signals, dict(self.event_counts))
            session_id = res['session_id']
            base_summary = ('**多维表已写入：** 场次、在线人数 %d、互动 %d、爆品 %d、商品讲解 %d、热度榜 %d（含商品图）\n'
                            '[📊 打开多维表查看](%s)' %
                            (res['viewers'], res['events'], res['hits'], res['goods'], res.get('rank', 0),
                             BASE_URL))
            log('多维表写入完成: %s' % res)
        except Exception as e:
            base_summary = '**多维表写入失败：** %s（本地文件已生成，可稍后补写）' % str(e)[:100]
            log('多维表写入异常: %s' % e)
        # 妙记转写（免费，lark-cli 用户身份；飞书无"创建妙记"应用接口）
        minutes_summary = ''
        try:
            import xhs_minutes
            xhs_minutes.LARK_CLI = LARK_CLI
            xhs_minutes.FFMPEG = FFMPEG_BIN
            ts_files = sorted([os.path.join(OUTDIR, f)
                               for f in os.listdir(OUTDIR) if f.endswith('.ts')])
            if ts_files:
                mr = xhs_minutes.transcribe(ts_files, OUTDIR, logf=log)
                if bw and session_id:
                    bw.write_minutes(session_id, mr['hits'], self.start_ts, ANCHOR)
                minutes_summary = '**妙记话术：** 已转写并扫描话术 %d 段，[查看妙记](%s)' % (
                    len(mr['hits']), mr['minute_url'])
                log('妙记转写完成: %s' % mr['minute_url'])
        except Exception as e:
            minutes_summary = '**妙记转写未完成：** %s（本地录像已存档，可稍后补转）' % str(e)[:100]
            log('妙记转写异常: %s' % e)
        # 下播卡片
        vals = [c for (t, c, s) in self.online if c]
        card_els = [
            md('**%s** 直播已结束，本场监控时长 **%d 分钟**。' %
               (ANCHOR, round((time.time() - self.start_ts) / 60))),
            md('**本场数据：**\n峰值在线 **%s**　累计弹幕 **%d 条**　WS重连 **%d 次**' %
               (max(vals) if vals else '-', len(self.barrages), self.ws_reconnects)),
            md(base_summary),
            md(minutes_summary),
            md('**交付物：** 明细数据（人数/弹幕/商品/爆品，含商品图）已在多维表，本群只发总结报告、人数曲线、逐字稿、音频；录像超 100MB 本地存档。')
        ]
        push_card('🔚 直播已结束 · 监控交付', 'green', card_els)
        log('交付物已生成于 %s' % OUTDIR)
        self.send_files_to_chat()

    def _send_file_larkcli(self, name):
        """lark-cli 用户身份兜底发文件（lark-cli 只接受 cwd 相对路径）。"""
        import subprocess
        r = subprocess.run([LARK_CLI, 'im', '+messages-send', '--chat-id', CHAT_ID,
                            '--file', name, '--json'],
                           cwd=OUTDIR, capture_output=True, text=True, timeout=180)
        return r.returncode == 0 and '"ok": true' in r.stdout.replace(' ', '')

    def _send_doc(self, ms, name):
        """优先应用身份，失败回落 lark-cli 用户身份。"""
        path = os.path.join(OUTDIR, name)
        if not os.path.exists(path):
            return
        if ms is not None:
            try:
                ms.send_file(CHAT_ID, path)
                log('已发群: %s' % name)
                return
            except Exception as e:
                log('应用身份发群失败 %s: %s；改用 lark-cli' % (name, str(e)[:100]))
        try:
            if self._send_file_larkcli(name):
                log('已发群(lark-cli): %s' % name)
            else:
                log('lark-cli 发群失败: %s' % name)
        except Exception as e:
            log('lark-cli 发群异常 %s: %s' % (name, str(e)[:100]))

    def send_files_to_chat(self):
        """文档/音频直接发群（应用身份优先，lark-cli 兜底）；视频合并 mp4，
        ≤100MB 发群，否则存档说明。"""
        import subprocess
        if not CHAT_ID:
            log('未配置 CHAT_ID，跳过文件发群')
            return
        ms = None
        try:
            sys.path.insert(0, CONFIG_SYNC_DIR)
            from feishu_client import FeishuApp
            from feishu_app_messenger import AppMessenger
            ms = AppMessenger(FeishuApp(), logf=log)
        except Exception as e:
            log('应用消息器初始化失败，文件将走 lark-cli: %s' % e)
        # CSV 明细（人数/弹幕/商品讲解/爆品）已写入多维表，不再发群，避免重复
        docs = ['监控总结报告.md', '在线人数曲线.html', '整场逐字稿.txt', '整场音频.m4a']
        for name in docs:
            self._send_doc(ms, name)
        # 视频：合并 TS → mp4，按大小决定
        videos = sorted([f for f in os.listdir(OUTDIR) if f.endswith('.ts')])
        total_mb = sum(os.path.getsize(os.path.join(OUTDIR, f)) for f in videos) / 1048576
        mp4_path = os.path.join(OUTDIR, ANCHOR + '_整场.mp4')
        if videos:
            try:
                lst = os.path.join(OUTDIR, '_vlist.txt')
                with open(lst, 'w', encoding='utf-8') as f:
                    for v in videos:
                        f.write("file '%s'\n" % os.path.join(OUTDIR, v))
                subprocess.run([FFMPEG_BIN, '-hide_banner', '-loglevel', 'error', '-y',
                                '-f', 'concat', '-safe', '0', '-i', lst,
                                '-c', 'copy', '-bsf:a', 'aac_adtstoasc', mp4_path],
                               capture_output=True, text=True, timeout=900)
            except Exception as e:
                log('TS 合并 mp4 失败: %s' % e)
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) <= 100 * 1048576:
            try:
                ms.send_video(CHAT_ID, mp4_path)
                log('视频已发群: %s（%.0fMB）' % (os.path.basename(mp4_path),
                      os.path.getsize(mp4_path) / 1048576))
            except Exception as e:
                log('视频发群失败，转存档: %s' % str(e)[:100])
                self._video_archive_card(videos, total_mb)
        else:
            self._video_archive_card(videos, total_mb)

    def _video_archive_card(self, videos, total_mb):
        push_card('🎬 录制视频已存档', 'blue', [
            md('本场共录制 **%d 段** TS，合计 **%.0f MB**，超过 IM 文件大小限制，已本地存档。\n路径：`%s`' %
               (len(videos), total_mb, OUTDIR))
        ])

    def chart(self):
        points = [c for (t, c, s) in self.online if c is not None]
        labels = [time.strftime('%H:%M:%S', time.localtime(t)) for (t, c, s) in self.online]
        data = [c if c is not None else 'null' for (t, c, s) in self.online]
        html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>%s 在线人数曲线</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script></head>
<body style="margin:0;font-family:sans-serif;">
<div id="c" style="width:100%%;height:520px;"></div>
<script>
var chart=echarts.init(document.getElementById('c'));
chart.setOption({title:{text:'%s · 在线人数曲线',left:'center'},
 tooltip:{trigger:'axis'},grid:{left:50,right:30,top:60,bottom:50},
 xAxis:{type:'category',data:%s,axisLabel:{interval:Math.floor(%s/8)}},
 yAxis:{type:'value',name:'在线人数'},
 series:[{name:'在线',type:'line',smooth:true,data:%s,
 areaStyle:{color:'rgba(255,120,120,0.25)'},lineStyle:{color:'#e8554e'},
 itemStyle:{color:'#e8554e'},symbol:'none'}]});
window.addEventListener('resize',function(){chart.resize();});
</script></body></html>""" % (ANCHOR, ANCHOR, json.dumps(labels), len(labels), json.dumps(data))
        with open(os.path.join(OUTDIR, '在线人数曲线.html'), 'w', encoding='utf-8') as f:
            f.write(html)

    def report(self):
        vals = [c for (t, c, s) in self.online if c]
        # 热词（简单：弹幕高频 2-4 字片段）
        words = defaultdict(int)
        for ts, who, text in self.barrages:
            for w in re.findall(r'[\u4e00-\u9fa5]{2,4}', text):
                words[w] += 1
        top = sorted(words.items(), key=lambda x: -x[1])[:15]
        lines = ['# %s · 小红书直播监控总结报告' % ANCHOR, '',
                 '- 监控时间：%s ~ %s' % (time.strftime('%Y-%m-%d %H:%M', time.localtime(self.start_ts)),
                                       time.strftime('%H:%M', time.localtime())),
                 '- 监控时长：约 %d 分钟' % round((time.time() - self.start_ts) / 60),
                 '- 峰值在线：%s' % (max(vals) if vals else '-'),
                 '- 累计弹幕：%d 条' % len(self.barrages),
                 '- WS 重连：%d 次' % self.ws_reconnects, '',
                 '## 弹幕高频词', '']
        lines += ['- %s（%d）' % (w, n) for w, n in top] or ['- 无']
        # 商品讲解
        lines += ['', '## 商品讲解时间线', '']
        if self.explain_segments:
            for seg in self.explain_segments:
                g = self.goods.get(seg['goods_id'], {})
                end = seg['end'] or time.time()
                price = ('　到手价 ¥%s' % g['price']) if g.get('price') not in (None, '') else ''
                spec = ('　%s' % g['spec']) if g.get('spec') else ''
                lines.append('- **%s**（%s ~ %s，%d 分钟）%s%s' % (
                    seg['title'] or seg['goods_id'],
                    time.strftime('%H:%M', time.localtime(seg['start'])),
                    time.strftime('%H:%M', time.localtime(end)),
                    round((end - seg['start']) / 60), price, spec))
        else:
            lines.append('- 本场未采集到商品讲解信号（可能非带货场）')
        # 爆品时刻对应商品
        lines += ['', '## 爆品时刻对应商品', '']
        if self.surge_moments:
            for ts, base, cnt, gid, gtitle in self.surge_moments:
                ratio = round((cnt - base) / base * 100) if base else 0
                lines.append('- `%s` 人数 %d→%d（+%d%%）：**%s**' % (
                    time.strftime('%H:%M', time.localtime(ts)), base, cnt, ratio,
                    gtitle or '当时无商品讲解信号'))
        else:
            lines.append('- 本场未触发人数突增阈值')
        # 购买意向
        if self.cart_signals:
            lines += ['', '## 实时购买意向（“正在去买”）', '',
                      '- 共采集到 **%d 次** 点击购买信号，示例：' % len(self.cart_signals)]
            lines += ['  - %s' % d for (t, d) in self.cart_signals[-10:] if d]
        lines += ['', '## 弹幕原文（节选）', '']
        lines += ['- `%s` **%s**：%s' % (time.strftime('%H:%M', time.localtime(ts)), who, text)
                  for ts, who, text in self.barrages[-50:]]
        return '\n'.join(lines)


async def main():
    global RECOVERY_PATH
    RECOVERY_PATH = os.path.join(OUTDIR, 'signal_state.json')
    cookie = open(COOKIE_PATH).read().strip()
    auth = XHSUnifiedAuth.from_cookie(cookie)
    live = XHSLiveAPI(auth.pc)
    p = Pipeline(live, auth=auth)
    log('开始监控 %s（%s）' % (ANCHOR, ROOM_ID))
    tasks = [asyncio.create_task(p.ws_loop()), asyncio.create_task(p.poll_loop()),
             asyncio.create_task(p.status_loop())]
    while not p.stop:
        await asyncio.sleep(2)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    p.deliver()
    auth.close()


if __name__ == '__main__':
    asyncio.run(main())
