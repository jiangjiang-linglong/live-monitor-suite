# encoding: utf-8
"""小红书直播监控 · 无人值守守护（watchdog）

职责（对应能力缺口 P1 运维四项）：
  1. 会话守护：relay 拉起、按目标自动发现开播 → 启动 ffmpeg + pipeline；
     崩溃重启（pipeline 从 signal_state.json 自愈）；下播后等待下一场。
  2. 登录态：周期性用真实接口探测 cookie 健康；失效立即飞书红色告警；
     每次探测即"保活"（服务端滑动会话），并把服务端续发的 cookie 原子回写，
     让 web_session 连续保持数周~数月。
  3. 接口监控：非官方接口返回非 JSON（风控 JS 挑战/HTML）、结构漂移、
     code 异常、WS 事件静默 → 区分网络抖动与接口变更，分级告警。
  4. 磁盘监控：磁盘阈值告警；视频文件（.ts/.mp4）24 小时后自动删除；
     磁盘告急时按最旧优先应急清理（保护正在录制的目录）。

用法:
  ./.venv/bin/python -u xhs_watchdog.py            # 常驻（launchd 拉起）
  ./.venv/bin/python -u xhs_watchdog.py --once     # 跑一轮检查后退出（自检）
  ./.venv/bin/python -u xhs_watchdog.py --cleanup  # 只执行一次录像清理
  ./.venv/bin/python -u xhs_watchdog.py --resolve-link "<链接>"  # 验证链接能否识别
配置：同目录 watchdog_config.json（首次运行自动生成默认值）。
"""
import os
import re
import sys
import json
import time
import socket
import shutil
import signal
import threading
import subprocess
from urllib import request as urlreq

SPIDER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SPIDER_DIR)
LOG_DIR = os.path.join(SPIDER_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(SPIDER_DIR, 'watchdog_config.json')
COOKIE_PATH = os.environ.get('XHS_COOKIE_PATH', os.path.join(SPIDER_DIR, 'login_cookies.txt'))
COOKIE_BAK_PATH = os.path.join(SPIDER_DIR, 'login_cookies.bak')

DEFAULT_CONFIG = {
    # 告警群机器人 webhook 与群 chat_id（可改成你自己的群）
    'webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/0929d006-2582-4b9b-b65e-1d2e2af3e71a',
    'chat_id': 'oc_de6681ba19dc2ee37ee942727272',
    # 妙记转写/发文件兜底用的 lark-cli；PATH 中能找到时直接填 lark-cli
    'lark_cli': 'lark-cli',
    # 交付与录像目录（默认全部在仓库内，可改成大容量磁盘路径）
    'delivery_root': os.path.join(REPO_ROOT, 'deliveries'),
    'extra_video_dirs': [],
    'relay_dir': os.path.join(REPO_ROOT, 'relay'),
    'relay_port': 18080,
    'ffmpeg_bin': os.path.expanduser('~/.local/bin/ffmpeg'),
    # 监控目标：host_id 稳定，room_id 每场变化（留空则从直播广场按 host_id 发现）
    # 也可在 url 里直接粘贴直播间链接/分享文本（含 xhslink 短链），自动解析
    'target': {'host_id': '', 'name': '', 'room_id': '', 'url': ''},
    'interval_sec': 60,              # 会话守护轮询
    'auth_probe_sec': 1200,          # 登录态/接口探测（20 分钟，同时承担保活）
    'disk_check_sec': 300,           # 磁盘检查（5 分钟）
    'video_retention_hours': 24,     # 视频文件保留时长
    'disk_warn_pct': 80,
    'disk_crit_pct': 90,
    'disk_emergency_pct': 92,
    'disk_emergency_target_pct': 88,
    'active_grace_sec': 900,         # 视频文件 mtime 小于该值视为录制中，绝不删除
}

VIDEO_EXTS = ('.ts', '.mp4')
FFMPEG_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
             'Chrome/130.0.0.0 Safari/537.36')

# 登录态失效签名（已实测）
AUTH_CODES = (-100, -101)
AUTH_KEYWORDS = ('登录已过期', '无登录信息', '登录信息为空', '未登录', '请登录')
# 风控/反爬签名
RISK_KEYWORDS = ('471', 'function getdss', '__playground__', '安全验证', '验证码')

# ---------- 监控目标链接解析 ----------
_URL_RE = re.compile(r'https?://[^\s，。、“”"\'「」()（）]+')
_ROOM_RE = re.compile(r'(?:/live/|[?&]room_id=|"room_id_str":?)(\d{10,})')
_HOST_RE = re.compile(r'(?:[?&]host_id=|/user/profile/)([0-9a-f]{16,})')
_SHORT_HOSTS = ('xhslink.com',)
_link_cache = {}


def resolve_target_link(text):
    """把直播间链接/分享文本解析为 {'room_id':..,'host_id':..}（可能只有其一）。

    支持：
      - 纯数字房间号（10 位以上）
      - 纯 hex host_id（16 位以上）
      - https://www.xiaohongshu.com/live/<room_id>
      - 链接中带 host_id= / /user/profile/<host_id>
      - xhslink.com 短链（跟随跳转，无需登录）
    """
    text = (text or '').strip()
    if not text:
        return {}
    if text in _link_cache:
        return _link_cache[text]
    if re.fullmatch(r'\d{10,}', text):
        out = {'room_id': text}
    elif re.fullmatch(r'[0-9a-f]{16,}', text):
        out = {'host_id': text}
    else:
        m = _URL_RE.search(text)
        if not m:
            _link_cache[text] = {}
            return {}
        final = m.group(0).rstrip(')）.,，。!?！？')
        if any(h in final for h in _SHORT_HOSTS):
            try:
                req = urlreq.Request(final, headers={'User-Agent': FFMPEG_UA})
                with urlreq.urlopen(req, timeout=10) as r:
                    final = r.geturl()
            except Exception as e:
                log('短链解析失败 %s: %s' % (final[:60], str(e)[:80]))
                _link_cache[text] = {}
                return {}
        out = {}
        m = _ROOM_RE.search(final)
        if m:
            out['room_id'] = m.group(1)
        m = _HOST_RE.search(final)
        if m:
            out['host_id'] = m.group(1)
    _link_cache[text] = out
    return out


def effective_target(target):
    """合并 target 中的 url/链接文本，返回补齐后的 dict。"""
    t = dict(target or {})
    for key in ('url', 'room_id', 'host_id'):
        val = (t.get(key) or '').strip()
        if val and not val.startswith(('http', '🔴')) and ('://' not in val):
            continue
        if val.startswith('http') or 'xhslink.com' in val or _URL_RE.search(val):
            parsed = resolve_target_link(val)
            if parsed.get('room_id') and not (t.get('room_id') or '').strip():
                t['room_id'] = parsed['room_id']
            if parsed.get('host_id') and not (t.get('host_id') or '').strip():
                t['host_id'] = parsed['host_id']
    return t


def log(msg):
    print('[%s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg), flush=True)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        os.chmod(CONFIG_PATH, 0o600)
        log('已生成默认配置 %s' % CONFIG_PATH)
        return dict(DEFAULT_CONFIG)
    cfg = json.load(open(CONFIG_PATH, encoding='utf-8'))
    # 补齐新增字段
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


# ---------------------------------------------------------------------------
# 飞书告警（去重 + 恢复通知 + 持续故障限时重发）
# ---------------------------------------------------------------------------
class Alerter:
    def __init__(self, webhook):
        self.webhook = webhook
        self._lock = threading.Lock()
        self._state = {}   # key -> (level, last_sent_ts, last_detail)

    def _push(self, title, template, lines):
        payload = {'msg_type': 'interactive',
                   'card': {'config': {'wide_screen_mode': True},
                            'header': {'template': template,
                                       'title': {'tag': 'plain_text', 'content': title}},
                            'elements': [{'tag': 'div', 'text': {'tag': 'lark_md',
                                          'content': s}} for s in lines]}}
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urlreq.Request(self.webhook, data=data,
                             headers={'Content-Type': 'application/json'})
        try:
            with urlreq.urlopen(req, timeout=15) as r:
                r.read()
            return True
        except Exception as e:
            log('告警推送失败: %s' % e)
            return False

    def raise_(self, key, level, title, lines, repeat_sec=3600):
        """level: red/amber。同 key 同级别按 repeat_sec 去重；升级立即发。"""
        now = time.time()
        with self._lock:
            prev = self._state.get(key)
            if prev and prev[0] == level and now - prev[1] < repeat_sec:
                return False
            self._state[key] = (level, now, title)
        template = 'red' if level == 'red' else 'orange'
        return self._push(title, template, lines)

    def recover(self, key, title, lines):
        with self._lock:
            prev = self._state.pop(key, None)
        if prev:
            self._push(title, 'green', lines)
            return True
        return False

    def info(self, title, lines):
        return self._push(title, 'blue', lines)


# ---------------------------------------------------------------------------
# 进程工具
# ---------------------------------------------------------------------------
def pgrep(pattern):
    """返回匹配命令行的 PID 列表（排除自己/grep）。"""
    try:
        out = subprocess.run(['pgrep', '-f', pattern], capture_output=True,
                             text=True, timeout=10).stdout
        return [int(x) for x in out.split()]
    except Exception:
        return []


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_open(port, host='127.0.0.1'):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# cookie 读写（服务端滑动会话续期 + 原子回写）
# ---------------------------------------------------------------------------
def read_cookie_header():
    if not os.path.exists(COOKIE_PATH):
        return ''
    with open(COOKIE_PATH, encoding='utf-8') as f:
        return f.read().strip()


def _cookie_to_header(cookie_obj):
    """统一 cookie 输入为 'k=v; k=v' 字符串。
    支持 str / dict / curl_cffi 的 Cookies（MutableMapping）。
    排除 acw_tc：阿里 WAF 边缘 cookie，host-only，不属于共享登录态。"""
    if cookie_obj is None:
        return ''
    if isinstance(cookie_obj, str):
        header = cookie_obj.strip()
    elif hasattr(cookie_obj, 'items'):
        header = '; '.join('%s=%s' % (k, v) for k, v in cookie_obj.items()
                           if k and k != 'acw_tc')
    elif hasattr(cookie_obj, 'jar'):
        header = '; '.join('%s=%s' % (c.name, c.value) for c in cookie_obj.jar
                           if c.name and c.name != 'acw_tc')
    else:
        header = str(cookie_obj)
    # 即使字符串里带了 acw_tc 也剔掉
    return '; '.join(p.strip() for p in header.split(';')
                     if p.strip() and not p.strip().startswith('acw_tc='))


def persist_cookie_if_changed(header, logf=log):
    """把内存中的最新 cookie 头回写文件；内容未变不动，变化时保留一份 .bak。

    服务端常规响应只下发 acw_tc（host-only，已被排除在 header 之外）；
    一旦 web_session/id_token 等被轮换，这里保证重启后仍使用新值。"""
    header = _cookie_to_header(header)
    if not header:
        return False
    old = read_cookie_header()
    if _cookie_kv(old) == _cookie_kv(header):
        return False
    try:
        if old and os.path.exists(COOKIE_PATH):
            shutil.copyfile(COOKIE_PATH, COOKIE_BAK_PATH)
        tmp = COOKIE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(header.strip() + '\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, COOKIE_PATH)
        logf('cookie 已续期回写（%d 项）' % len(_cookie_kv(header)))
        return True
    except Exception as e:
        logf('cookie 回写失败: %s' % e)
        return False


def _cookie_kv(header):
    out = {}
    for item in (header or '').split(';'):
        item = item.strip()
        if '=' in item:
            k, v = item.split('=', 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# 登录态 + 接口探测（P1-1 / P1-4）
# ---------------------------------------------------------------------------
class HealthProbe:
    def __init__(self, cfg, alerter):
        self.cfg = cfg
        self.alerter = alerter
        self.net_fail = 0          # 连续网络异常
        self.struct_fail = 0       # 连续结构/风控异常
        self.healthy = None

    def _probe_once(self):
        """返回 (status, detail)
        status: ok / auth_dead / risk / changed / net_error"""
        sys.path.insert(0, SPIDER_DIR)
        from xhs_utils.xhs_auth import XHSUnifiedAuth
        from apis.xhs_live import XHSLiveAPI
        cookie = read_cookie_header()
        if not cookie or 'web_session=' not in cookie:
            return 'auth_dead', 'cookie 文件缺失或不含 web_session'
        try:
            auth = XHSUnifiedAuth.from_cookie(cookie)
        except Exception as e:
            msg = str(e)
            if any(k in msg for k in AUTH_KEYWORDS):
                return 'auth_dead', '登录态引导失败：%s' % msg[:120]
            return 'net_error', '鉴权初始化异常：%s' % msg[:120]
        live = XHSLiveAPI(auth.pc)
        status = 'ok'
        detail = ''
        try:
            # 包一层拿原始响应（不走 _request 的 json()，好区分风控 JS）
            room = self._raw_signed_get(live, self.cfg['target'].get('room_id') or '570514733809625021')
            if room is not None:
                status, detail = room
            if status == 'ok':
                # 广场接口验证另一条接口链路
                sq = self._raw_square(live)
                if sq is not None:
                    status, detail = sq
            # 探测本身即保活；回写任何服务端续发 cookie
            persist_cookie_if_changed(auth.pc.cookies)
        finally:
            try:
                auth.close()
            except Exception:
                pass
        return status, detail

    def _raw_signed_get(self, live, room_id):
        """复用 live._request 的签名逻辑，但直接检查 HTTP 响应。"""
        from apis.xhs_live import generate_request_params, build_pc_live_headers
        api = ('/api/sns/red/live/web/v1/room/current_room_info?room_id=%s'
               '&request_user_id=%s&source=web_live&client_type=1' %
               (room_id, live.auth.user_id))
        target = live.base_url + api
        context = live.auth.next_sign_context(api)
        b1 = live.auth.current_b1(context['now'])
        headers, cookies, body = generate_request_params(
            live.auth.cookies, api, '', 'GET', user_id=live.auth.user_id,
            b1=b1, dsl_pair=live.auth.dsl_pair, doc_cookie=live.auth.sign_cookie,
            tier=context['tier'], sign_context=context,
            include_client_hints=False, include_trace_headers=False)
        wire_cookies = live.auth.cookies_for_url(target, cookies)
        headers = build_pc_live_headers(headers, wire_cookies, method='GET')
        return self._classify(live.http.get(target, headers=headers,
                                            cookies=wire_cookies, timeout=15),
                              expect=('room_info',))

    def _raw_square(self, live):
        from apis.xhs_live import generate_request_params, build_pc_live_headers
        api = ('/api/sns/red/live/web/feed/v1/squarefeed?source=13&category='
               '&pre_source=&extra_info=%7B%7D&size=5')
        target = live.base_url + api
        context = live.auth.next_sign_context(api)
        b1 = live.auth.current_b1(context['now'])
        headers, cookies, body = generate_request_params(
            live.auth.cookies, api, '', 'GET', user_id=live.auth.user_id,
            b1=b1, dsl_pair=live.auth.dsl_pair, doc_cookie=live.auth.sign_cookie,
            tier=context['tier'], sign_context=context,
            include_client_hints=False, include_trace_headers=False)
        wire_cookies = live.auth.cookies_for_url(target, cookies)
        headers = build_pc_live_headers(headers, wire_cookies, method='GET')
        return self._classify(live.http.get(target, headers=headers,
                                            cookies=wire_cookies, timeout=15),
                              expect=('feeds',))

    def _classify(self, resp, expect):
        """检查一个 HTTP 响应，区分 风控 / 登录失效 / 结构漂移 / 网络。"""
        try:
            ct = resp.headers.get('content-type') or ''
            text = resp.text
        except Exception as e:
            return 'net_error', '读取响应失败 %s' % str(e)[:80]
        if resp.status_code == 471 or 'function getdss' in text or ('application/javascript' in ct and 'getdss' in text):
            return 'risk', 'HTTP %s 触发风控 JS 挑战' % resp.status_code
        if resp.status_code >= 400:
            return 'risk', 'HTTP %s（疑似风控/接口变更）' % resp.status_code
        try:
            rj = json.loads(text)
        except Exception:
            head = text[:80].replace('\n', ' ')
            return 'changed', '非 JSON 响应（ct=%s）：%s' % (ct, head)
        code = rj.get('code')
        msg = rj.get('msg') or ''
        if code in AUTH_CODES or any(k in msg for k in AUTH_KEYWORDS):
            return 'auth_dead', 'code=%s %s' % (code, msg)
        if code not in (0, None):
            return 'changed', '接口返回异常 code=%s msg=%s' % (code, msg[:80])
        data = rj.get('data')
        if not isinstance(data, dict) or not all(k in data for k in expect):
            return 'changed', '返回结构缺字段（期望 %s），实际键：%s' % (
                list(expect), list(data.keys()) if isinstance(data, dict) else type(data).__name__)
        return 'ok', ''

    def run(self):
        try:
            status, detail = self._probe_once()
        except Exception as e:
            status = 'net_error'
            detail = '探测异常 %s: %s' % (type(e).__name__, str(e)[:100])
        log('健康探测: %s %s' % (status, detail))

        if status == 'ok':
            self.net_fail = 0
            self.struct_fail = 0
            if self.healthy is False:
                self.alerter.recover('auth', '✅ 小红书登录态已恢复',
                                     ['登录态/接口恢复正常，监控链路自动继续。'])
                self.alerter.recover('api', '✅ 小红书接口已恢复',
                                     ['接口返回结构恢复正常。'])
            self.healthy = True
            return
        if status == 'net_error':
            self.net_fail += 1
            # 网络问题连续 5 次才出声，避免抖动误报
            if self.net_fail >= 5 and self.net_fail % 5 == 0:
                self.alerter.raise_('net', 'amber', '🟡 小红书接口连续网络异常',
                                    ['连续 %d 次探测无法到达接口：%s' % (self.net_fail, detail),
                                     '录制与本地状态不受影响，将继续重试。'], repeat_sec=1800)
            return
        # 确定性故障（auth/risk/changed），连续 2 次确认
        self.struct_fail += 1
        if self.struct_fail < 2:
            return
        if status == 'auth_dead':
            self.healthy = False
            self.alerter.raise_(
                'auth', 'red', '🔴 小红书登录态失效 · 需重新扫码',
                ['**连续探测确认登录已失效：** %s' % detail,
                 '弹幕/商品/人数信号会中断；游客拉流录像不受影响。',
                 '请在 Spider_XHS 目录运行 `./.venv/bin/python do_login.py` 重新扫码（可能含风控码两步），',
                 '替换 `login_cookies.txt` 后无需重启，守护会自动恢复并在群里发绿色通知。'],
                repeat_sec=1800)
        else:
            level = 'red' if status == 'risk' else 'amber'
            title = '🔴 小红书触发风控/安全验证' if status == 'risk' else '🟡 小红书接口疑似变更'
            self.alerter.raise_(
                'api', level, title,
                ['**%s**' % detail,
                 '若持续出现，说明非官方接口已变更，需要跟进签名/字段；录像链路独立，不受影响。'],
                repeat_sec=1800)


# ---------------------------------------------------------------------------
# 会话守护（relay + ffmpeg + pipeline，崩溃拉起/自动发现开播）
# ---------------------------------------------------------------------------
class SessionSupervisor:
    def __init__(self, cfg, alerter):
        self.cfg = cfg
        self.alerter = alerter
        self.ffmpeg_bin = cfg['ffmpeg_bin'] if os.path.exists(cfg['ffmpeg_bin']) else 'ffmpeg'
        self.procs = {}          # room_id -> {'ffmpeg': Popen, 'pipeline': Popen, 'outdir': str, 'name': str}
        self.crash_count = {}
        self.last_start = {}     # room_id -> 上次启动时间（重启节流）
        self.last_relay_try = 0  # relay 拉起节流

    # ---- relay ----
    def ensure_relay(self):
        port = self.cfg['relay_port']
        if port_open(port):
            return True
        if pgrep(r'xhs_relay\.py'):
            return True
        if time.time() - self.last_relay_try < 60:
            return False
        self.last_relay_try = time.time()
        relay_dir = self.cfg['relay_dir']
        py = os.path.join(relay_dir, '.venv', 'bin', 'python')
        if not os.path.exists(py):
            # relay 只依赖 requests，复用 monitor 的虚拟环境
            py = os.path.join(SPIDER_DIR, '.venv', 'bin', 'python')
        if not os.path.exists(py):
            py = 'python3'
        logdir = os.path.join(relay_dir, 'logs')
        os.makedirs(logdir, exist_ok=True)
        try:
            lf = open(os.path.join(logdir, 'xhs_relay.watchdog.log'), 'ab')
            subprocess.Popen([py, '-u', 'xhs_relay.py', str(port)], cwd=relay_dir,
                             stdout=lf, stderr=subprocess.STDOUT,
                             start_new_session=True)
            time.sleep(3)
            ok = port_open(port)
            log('relay 拉起 %s' % ('成功' if ok else '失败'))
            if not ok:
                self.alerter.raise_('relay', 'red', '🔴 小红书转封装 relay 启动失败',
                                    ['端口 %s 未监听，录制链路不可用，请检查 %s' % (port, relay_dir)],
                                    repeat_sec=900)
            else:
                self.alerter.recover('relay', '✅ relay 已恢复', ['转封装代理恢复正常。'])
            return ok
        except Exception as e:
            log('relay 拉起异常: %s' % e)
            return False

    # ---- 目标发现 ----
    def find_live(self):
        """返回 (room_id, name) 或 None。"""
        target = effective_target(self.cfg['target'])
        room_id = (target.get('room_id') or '').strip()
        name = target.get('name') or '小红书直播'
        if not room_id and not (target.get('host_id') or '').strip():
            raw = (self.cfg.get('target', {}).get('url') or '').strip()
            if raw:
                log('监控目标链接无法解析，请检查 url 字段：%s' % raw[:100])
            return None
        sys.path.insert(0, SPIDER_DIR)
        from xhs_utils.xhs_auth import XHSUnifiedAuth
        from apis.xhs_live import XHSLiveAPI
        cookie = read_cookie_header()
        if not cookie:
            return None
        try:
            auth = XHSUnifiedAuth.from_cookie(cookie)
            live = XHSLiveAPI(auth.pc)
            if room_id:
                r = live.current_room_info(room_id)
                info = (r.get('data') or {}).get('room_info') or {}
                status = str(info.get('status'))
                persist_cookie_if_changed(auth.pc.cookies)
                if status == '2':
                    return room_id, name
                return None
            # 广场按 host_id 发现
            r = live.square_feed()
            persist_cookie_if_changed(auth.pc.cookies)
            host_id = (target.get('host_id') or '').strip()
            for item in (r.get('data') or {}).get('feeds') or []:
                lv = item.get('live') or {}
                room = lv.get('t_room_info') or {}
                host = lv.get('t_live_host_info') or {}
                rid = room.get('room_id_str') or str(room.get('room_id') or '')
                if host.get('user_id') == host_id and rid:
                    return rid, host.get('nickname') or name
            return None
        except Exception as e:
            log('开播发现失败: %s' % str(e)[:120])
            return None
        finally:
            try:
                auth.close()
            except Exception:
                pass

    # ---- 进程拉起 ----
    def _outdir(self, name):
        day = time.strftime('%Y%m%d')
        return os.path.join(self.cfg['delivery_root'], '%s_%s' % (name, day))

    def _start_ffmpeg(self, room_id, name, outdir):
        os.makedirs(outdir, exist_ok=True)
        existing = [f for f in os.listdir(outdir) if f.endswith('.ts')]
        prefix = name
        logf = open(os.path.join(outdir, 'ffmpeg.log'), 'ab')
        cmd = [self.ffmpeg_bin, '-loglevel', 'warning', '-rw_timeout', '30000000',
               '-user_agent', FFMPEG_UA,
               '-i', 'http://127.0.0.1:%d/live/%s' % (self.cfg['relay_port'], room_id),
               '-c', 'copy', '-f', 'segment', '-segment_time', '1800',
               '-segment_start_number', str(len(existing)),
               '-reset_timestamps', '1',
               '%s_%%03d.ts' % prefix]
        p = subprocess.Popen(cmd, cwd=outdir, stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True)
        log('ffmpeg 已启动 PID=%s outdir=%s（已有%d段，续写）' % (p.pid, outdir, len(existing)))
        return p

    def _start_pipeline(self, room_id, name, outdir):
        py = os.path.join(SPIDER_DIR, '.venv', 'bin', 'python')
        cmd = [py, '-u', 'xhs_live_pipeline.py', room_id,
               self.cfg['target'].get('host_id', ''), name, outdir,
               self.cfg['webhook'], self.cfg.get('chat_id', ''),
               self.cfg.get('lark_cli', 'lark-cli')]
        logf = open(os.path.join(outdir, 'pipeline.log'), 'ab')
        p = subprocess.Popen(cmd, cwd=SPIDER_DIR, stdout=logf, stderr=subprocess.STDOUT,
                             start_new_session=True)
        log('pipeline 已启动 PID=%s room=%s' % (p.pid, room_id))
        return p

    def _adopt(self, room_id, name):
        """接管人工/旧 watchdog 已启动的进程。"""
        pipes = pgrep(r'xhs_live_pipeline\.py %s' % room_id)
        ff = pgrep(r'18080/live/%s' % room_id)
        if pipes:
            outdir = self._outdir(name)
            # 从进程命令行解析真实输出目录：
            # python xhs_live_pipeline.py ROOM HOST NAME OUTDIR WEBHOOK ...
            try:
                argv = subprocess.run(['ps', '-p', str(pipes[0]), '-o', 'command='],
                                     capture_output=True, text=True, timeout=10).stdout.split()
                # 找到以脚本名开头的参数序列，其后第 4 个参数为 OUTDIR
                for idx, tok in enumerate(argv):
                    if tok.endswith('xhs_live_pipeline.py') and len(argv) >= idx + 7:
                        if os.path.isdir(argv[idx + 4]):
                            outdir = argv[idx + 4]
                        break
            except Exception:
                pass
            self.procs[room_id] = {
                'pipeline_pid': pipes[0], 'ffmpeg_pid': ff[0] if ff else None,
                'outdir': outdir, 'name': name, 'adopted': True}
            log('已接管在跑的会话 room=%s pipeline PID=%s outdir=%s' %
                (room_id, pipes[0], outdir))
            return True
        return False

    def _start_session(self, room_id, name):
        outdir = self._outdir(name)
        os.makedirs(outdir, exist_ok=True)
        self.ensure_relay()
        time.sleep(1)
        ff = self._start_ffmpeg(room_id, name, outdir)
        time.sleep(2)
        pp = self._start_pipeline(room_id, name, outdir)
        self.procs[room_id] = {'pipeline_pid': pp.pid, 'ffmpeg_pid': ff.pid,
                               'outdir': outdir, 'name': name, 'adopted': False}
        self.alerter.info('🟢 守护自动开播监控：%s' % name,
                          ['room_id `%s`\n输出目录：%s' % (room_id, outdir),
                           'ffmpeg 录制 + 弹幕/人数/商品采集已启动，下播后自动交付。'])

    def _stop_ffmpeg(self, room_id):
        rec = self.procs.get(room_id)
        pid = rec and rec.get('ffmpeg_pid')
        if pid and pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                log('已停止 ffmpeg PID=%s' % pid)
            except OSError:
                pass

    def tick(self):
        # 1) relay 常驻
        self.ensure_relay()
        # 2) 发现开播
        found = self.find_live()
        if not found:
            # 无直播：清理已死会话残留的 ffmpeg，避免空录
            for rid in list(self.procs.keys()):
                rec = self.procs[rid]
                if not pid_alive(rec['pipeline_pid']):
                    self._stop_ffmpeg(rid)
                    self.procs.pop(rid, None)
                    log('会话已结束（下播），清理 room=%s 录制进程' % rid)
            return
        room_id, name = found
        if room_id in self.procs:
            rec = self.procs[room_id]
            if pid_alive(rec['pipeline_pid']):
                # ffmpeg 崩了则补拉（pipeline 会从 signal_state 恢复）
                if not rec.get('ffmpeg_pid') or not pid_alive(rec['ffmpeg_pid']):
                    if not pgrep(r'18080/live/%s' % room_id):
                        try:
                            ff = self._start_ffmpeg(room_id, name, rec['outdir'])
                            rec['ffmpeg_pid'] = ff.pid
                        except Exception as e:
                            log('ffmpeg 补拉失败: %s' % e)
                return
            # pipeline 已退出但仍在播 = 崩溃，重启
            self._stop_ffmpeg(room_id)
            self.procs.pop(room_id, None)
            log('pipeline 已退出但房间仍在播，判定崩溃 room=%s' % room_id)
        # 人工启动的同房间进程先接管
        if self._adopt(room_id, name):
            return
        # 崩溃重启节流：同房间至少间隔 60s，连续失败前 3 次上报
        if time.time() - self.last_start.get(room_id, 0) < 60:
            return
        self.crash_count.setdefault(room_id, 0)
        try:
            self._start_session(room_id, name)
            self.last_start[room_id] = time.time()
            self.crash_count[room_id] = 0
        except Exception as e:
            self.last_start[room_id] = time.time()
            self.crash_count[room_id] += 1
            log('会话启动失败: %s' % e)
            if self.crash_count[room_id] <= 3:
                self.alerter.raise_('session_%s' % room_id, 'red',
                                    '🔴 小红书监控会话启动失败',
                                    ['room=%s 错误：%s' % (room_id, e)], repeat_sec=900)

    def is_pipeline_running(self):
        return any(pid_alive(r['pipeline_pid']) for r in self.procs.values())

    def active_dirs(self):
        """正在录制/监控的输出目录（磁盘清理保护）。"""
        dirs = set()
        for rec in self.procs.values():
            if pid_alive(rec['pipeline_pid']):
                dirs.add(os.path.realpath(rec['outdir']))
        # 保护 ffmpeg 正在写的目录：以命令行输出模板路径为准（相对 cwd 解析）。
        # 不能直接把 cwd 整个加入保护——录制器 cwd 是根目录，会误保其他旧场次。
        for pid in pgrep(r'ffmpeg.*18080/live/'):
            cwd = None
            try:
                out = subprocess.run(['lsof', '-p', str(pid), '-a', '-d', 'cwd', '-Fn'],
                                     capture_output=True, text=True, timeout=10).stdout
                for line in out.splitlines():
                    if line.startswith('n/'):
                        cwd = os.path.realpath(line[1:])
            except Exception:
                pass
            try:
                cmdline = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                                         capture_output=True, text=True, timeout=10).stdout
                found_template = False
                for token in cmdline.split():
                    if token.lower().endswith(('.ts', '.mp4')) or '%03d' in token:
                        out_path = token if token.startswith('/') else \
                            os.path.join(cwd or '', token)
                        dirs.add(os.path.realpath(os.path.dirname(out_path)))
                        found_template = True
                # 命令行未暴露输出路径时退回保护 cwd
                if not found_template and cwd:
                    dirs.add(cwd)
            except Exception:
                pass
        return dirs

    @staticmethod
    def is_protected(file_path, protected_dirs):
        """文件位于受保护目录之内（含子目录）即不可删。"""
        parent = os.path.realpath(os.path.dirname(file_path))
        for d in protected_dirs:
            d = os.path.realpath(d)
            if parent == d or parent.startswith(d + os.sep):
                return True
        return False


# ---------------------------------------------------------------------------
# 磁盘/资源监控 + 录像 24h 清理（P1-3）
# ---------------------------------------------------------------------------
class DiskGuard:
    def __init__(self, cfg, alerter, supervisor):
        self.cfg = cfg
        self.alerter = alerter
        self.supervisor = supervisor

    def _video_dirs(self):
        roots = [self.cfg['delivery_root']] + [d for d in self.cfg.get('extra_video_dirs', [])
                                               if os.path.isdir(d)]
        return [r for r in roots if os.path.isdir(r)]

    def _iter_videos(self):
        for root in self._video_dirs():
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if f.lower().endswith(VIDEO_EXTS):
                        fp = os.path.join(dirpath, f)
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        yield fp, st.st_mtime, st.st_size

    def cleanup_expired(self, now=None):
        """删除 mtime 超过保留期的视频；跳过活动目录与最近写入（录制中）的文件。"""
        now = now or time.time()
        keep_sec = self.cfg['video_retention_hours'] * 3600
        grace = self.cfg['active_grace_sec']
        active = self.supervisor.active_dirs()
        deleted, freed = 0, 0
        for fp, mt, size in self._iter_videos():
            if self.supervisor.is_protected(fp, active):
                continue
            age = now - mt
            if age < grace:
                continue
            if age >= keep_sec:
                try:
                    os.remove(fp)
                    deleted += 1
                    freed += size
                    log('清理过期视频 %s（%.0f小时，%.0fMB）' %
                        (os.path.basename(fp), age / 3600, size / 1048576))
                except OSError as e:
                    log('删除失败 %s: %s' % (fp, e))
        if deleted:
            self.alerter.info('🧹 录像自动清理（%d小时保留）' % self.cfg['video_retention_hours'],
                              ['已删除 %d 个视频文件，释放 %.1f GB（CSV/报告/逐字稿/音频保留）。'
                               % (deleted, freed / 1073741824)])
        return deleted, freed

    def emergency_cleanup(self):
        """磁盘告急：删除最旧的非活动视频，直到降到目标水位。"""
        active = self.supervisor.active_dirs()
        videos = [(fp, mt, size) for fp, mt, size in self._iter_videos()
                  if not self.supervisor.is_protected(fp, active)
                  and time.time() - mt > self.cfg['active_grace_sec']]
        videos.sort(key=lambda x: x[1])
        freed = 0
        deleted = 0
        root = self.cfg['delivery_root']
        for fp, mt, size in videos:
            usage = shutil.disk_usage(root)
            if usage.used / usage.total * 100 < self.cfg['disk_emergency_target_pct']:
                break
            try:
                os.remove(fp)
                deleted += 1
                freed += size
                log('应急删除 %s' % os.path.basename(fp))
            except OSError:
                pass
        return deleted, freed

    def check(self):
        # 先执行到期清理（视频保留 video_retention_hours 小时）
        try:
            self.cleanup_expired()
        except Exception as e:
            log('到期清理异常: %s' % e)
        root = self.cfg['delivery_root']
        usage = shutil.disk_usage(root)
        pct = usage.used / usage.total * 100
        # 视频占用统计
        video_bytes = 0
        for _, _, size in self._iter_videos():
            video_bytes += size
        log('磁盘 %.1f%% 已用，视频合计 %.1f GB' % (pct, video_bytes / 1073741824))
        if pct >= self.cfg['disk_emergency_pct']:
            deleted, freed = self.emergency_cleanup()
            self.alerter.raise_(
                'disk', 'red', '🔴 磁盘告急 %.0f%% · 已应急清理' % pct,
                ['磁盘占用已达 %.1f%%，删除了 %d 个最旧非录制视频（释放 %.1f GB）。'
                 % (pct, deleted, freed / 1073741824),
                 '仍在持续增长，请人工确认是否需要更大磁盘。'], repeat_sec=1800)
        elif pct >= self.cfg['disk_crit_pct']:
            self.alerter.raise_(
                'disk', 'red', '🔴 磁盘占用 %.0f%%' % pct,
                ['视频目录占用 %.1f GB。%d 小时前的录像会自动删除；'
                 '若仍不够将在 %.0f%% 时应急清理最旧录像。'
                 % (video_bytes / 1073741824, self.cfg['video_retention_hours'],
                    self.cfg['disk_emergency_pct'])], repeat_sec=3600)
        elif pct >= self.cfg['disk_warn_pct']:
            self.alerter.raise_(
                'disk', 'amber', '🟡 磁盘占用 %.0f%%' % pct,
                ['视频目录占用 %.1f GB，超过 %.0f%% 请留意。'
                 % (video_bytes / 1073741824, self.cfg['disk_warn_pct'])], repeat_sec=7200)
        else:
            self.alerter.recover('disk', '✅ 磁盘水位恢复', ['磁盘占用已回落至 %.0f%%。' % pct])


# ---------------------------------------------------------------------------
# 运行中 pipeline 的 WS 静默检查（接口失效监控的一部分）
# ---------------------------------------------------------------------------
def check_pipeline_health(cfg, alerter, supervisor):
    """读各会话 status.json，WS 事件长时间不增长 → 接口/链路异常告警。"""
    for rid, rec in list(supervisor.procs.items()):
        if not pid_alive(rec['pipeline_pid']):
            continue
        status_file = os.path.join(rec['outdir'], 'status.json')
        if not os.path.exists(status_file):
            continue
        try:
            st = json.load(open(status_file, encoding='utf-8'))
        except Exception:
            continue
        updated = st.get('更新时间')
        if not updated:
            continue
        try:
            ts = time.mktime(time.strptime(updated, '%Y-%m-%d %H:%M:%S'))
        except Exception:
            continue
        if time.time() - ts > 300:
            alerter.raise_('stale_%s' % rid, 'amber', '🟡 监控状态已 5 分钟未更新',
                           ['room=%s（%s），采集循环可能卡住，将检查日志/考虑重启。'
                            % (rid, rec['name'])], repeat_sec=1800)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
class Watchdog:
    def __init__(self, cfg):
        self.cfg = cfg
        self.alerter = Alerter(cfg['webhook'])
        self.supervisor = SessionSupervisor(cfg, self.alerter)
        self.probe = HealthProbe(cfg, self.alerter)
        self.disk = DiskGuard(cfg, self.alerter, self.supervisor)
        self.stop = False

    def _loop(self, interval, fn, name):
        """每 interval 秒执行一次 fn 的线程（启动先跑一次）。"""
        def worker():
            while not self.stop:
                t0 = time.time()
                try:
                    fn()
                except Exception as e:
                    log('%s 循环异常: %s: %s' % (name, type(e).__name__, str(e)[:150]))
                dt = max(5, interval - (time.time() - t0))
                slept = 0
                while slept < dt and not self.stop:
                    time.sleep(min(2, dt - slept))
                    slept += 2
        th = threading.Thread(target=worker, name=name, daemon=True)
        th.start()
        return th

    def run(self):
        log('小红书无人值守守护启动（配置 %s）' % CONFIG_PATH)
        self.alerter.info('🛡️ 小红书无人值守守护已启动',
                         ['已接管：relay 守护、开播自动监控、崩溃拉起、登录态/接口/磁盘监控。',
                          '录像保留 %d 小时后自动删除；目标：%s' %
                          (self.cfg['video_retention_hours'],
                           self.cfg['target'].get('name', '未配置'))])
        # 启动时先跑一轮，确认 cookie 健康
        self.probe.run()
        self.disk.check()
        self._loop(self.cfg['interval_sec'], self.supervisor.tick, 'session')
        self._loop(self.cfg['auth_probe_sec'], self.probe.run, 'health')
        self._loop(self.cfg['disk_check_sec'], self.disk.check, 'disk')
        self._loop(180, lambda: check_pipeline_health(self.cfg, self.alerter, self.supervisor),
                   'pipeline-health')
        while not self.stop:
            time.sleep(5)


PIDFILE = os.path.join(LOG_DIR, 'watchdog.pid')


def acquire_pidfile():
    """单实例保护。已存在存活实例时退出（0）；否则原子写入自身 PID。"""
    if os.path.exists(PIDFILE):
        try:
            old = int(open(PIDFILE).read().strip())
            if pid_alive(old):
                log('守护已在运行（PID=%s），本次启动退出' % old)
                return False
        except (ValueError, OSError):
            pass
    tmp = PIDFILE + '.tmp'
    with open(tmp, 'w') as f:
        f.write(str(os.getpid()))
    os.replace(tmp, PIDFILE)
    return True


def release_pidfile():
    try:
        if os.path.exists(PIDFILE) and open(PIDFILE).read().strip() == str(os.getpid()):
            os.remove(PIDFILE)
    except OSError:
        pass


def main():
    cfg = load_config()
    if '--resolve-link' in sys.argv:
        i = sys.argv.index('--resolve-link')
        raw = sys.argv[i + 1] if i + 1 < len(sys.argv) else (cfg.get('target', {}).get('url') or '')
        if not raw.strip():
            print('用法：xhs_watchdog.py --resolve-link "<直播间链接或分享文本>"')
            print('或在 watchdog_config.json 的 target.url 填好后直接执行 --resolve-link')
            return
        parsed = resolve_target_link(raw)
        print('输入：', raw)
        print('解析结果：', json.dumps(parsed, ensure_ascii=False) or '（无法识别）')
        if parsed:
            t = effective_target(dict(cfg.get('target', {}), url=raw))
            print('生效目标：room_id=%s host_id=%s' % (t.get('room_id') or '', t.get('host_id') or ''))
        return
    if '--cleanup' in sys.argv:
        wd = Watchdog(cfg)
        deleted, freed = wd.disk.cleanup_expired()
        print('清理完成：%d 个，%.2f GB' % (deleted, freed / 1073741824))
        return
    if '--once' in sys.argv:
        wd = Watchdog(cfg)
        wd.supervisor.ensure_relay()
        wd.probe.run()
        wd.disk.check()
        found = wd.supervisor.find_live()
        print('开播发现:', found)
        wd.supervisor._adopt(found[0], found[1]) if found else None
        print('在跑会话:', list(wd.supervisor.procs.keys()))
        print('活动目录保护:', wd.supervisor.active_dirs())
        return
    if '--discover' in sys.argv:
        wd = Watchdog(cfg)
        print('当前开播目标:', wd.supervisor.find_live())
        return
    # 守护常驻模式：单实例保护 + 信号退出时清理 pidfile
    if not acquire_pidfile():
        return

    def _shutdown(signum, frame):
        log('收到信号 %s，退出守护' % signum)
        release_pidfile()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        Watchdog(cfg).run()
    finally:
        release_pidfile()


if __name__ == '__main__':
    main()
