# encoding: utf-8
"""完整登录（健壮版）：登录二维码 -> (触发风控时)风控二维码 -> 保存登录 Cookie。

- 登录码过期会自动重新生成；风控码会自动刷新（始终覆盖同名 PNG）。
- 整体最多运行 10 分钟，由外部用 nohup 独立拉起，避免被工具中断连带停止。
"""
import sys, time, struct, zlib
sys.path.insert(0, '.')
import qrcode
from apis.xhs_pc_login_apis import (
    XHSLoginApi, RiskControlError, build_pc_login_headers,
)
from xhs_utils.http_util import REQUEST_TIMEOUT


def write_png(path, matrix, scale=12, border=4):
    n = len(matrix)
    size = (n + 2 * border) * scale

    def is_black(px, py):
        mx = px // scale - border
        my = py // scale - border
        if 0 <= mx < n and 0 <= my < n:
            return matrix[my][mx]
        return False

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw += b'\x00\x00\x00' if is_black(x, y) else b'\xff\xff\xff'

    def chunk(typ, data):
        c = typ + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c))

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 9)))
        f.write(chunk(b'IEND', b''))


def save_qr_png(url, path):
    qr = qrcode.QRCode(border=4)
    qr.add_data(url)
    qr.make(fit=True)
    write_png(path, qr.get_matrix())


def prepare_login_qr(login):
    """初始化设备 -> 生成登录码 -> 预检 -> 保存 PNG，返回 (cookies, qr_id, code)。"""
    cookies = login.generate_init_cookies()
    success, msg, qr_data = login.generate_qrcode(cookies)
    if not success:
        raise RuntimeError('获取登录码失败: ' + str(msg))
    cookies = qr_data['cookies']
    success, msg, cookies = login.check_qrcode_status(
        qr_data['qr_id'], qr_data['code'], cookies)
    login.ensure_webprofile(cookies)
    save_qr_png(qr_data['qr_url'], os.environ.get('XHS_LOGIN_QR', 'login_qr.png'))
    print('LOGIN_QR_SAVED @%d' % int(time.time()), flush=True)
    return cookies, qr_data['qr_id'], qr_data['code']


def risk_verify(cookies, err: RiskControlError, timeout=200):
    """471/461 风控的 App 扫码验证，风控码自动刷新。"""
    va = XHSLoginApi()
    deadline = time.time() + timeout
    payload_base = {
        "verifyType": str(err.verify_type),
        "verifyUuid": str(err.verify_uuid),
        "verifyBiz": str(err.http_status),
        "sourceSite": "/",
    }
    boot = dict(cookies)
    va._ensure_profile(boot)
    va._initialize_security(boot)
    va._activate(boot)
    va.ensure_webprofile(boot)
    boot['xsecappid'] = 'fe-login'

    def signed_post(path, payload):
        headers, body = va._signed_request_params(
            boot, path, dict(payload),
            sec_domain=True, tier='0201', mns_profile='security_initial')
        headers = build_pc_login_headers(
            headers, va._cookies_for_url(va.base_url, boot), kind='post')
        return va.http.post(va.base_url + path, headers=headers,
                            data=body.encode('utf-8'),
                            proxies=va.proxies, timeout=REQUEST_TIMEOUT)

    try:
        while time.time() < deadline:
            resp = signed_post('/api/redcaptcha/v2/qr/init', payload_base)
            rid = None
            if resp.text.strip().startswith('{'):
                rid = (resp.json().get('data') or {}).get('rid')
            if not rid:
                print('  init 失败:', resp.text[:100], flush=True)
                time.sleep(2); continue
            qr_url = ('https://www.xiaohongshu.com/web-login/qrcode-transfer'
                      f'?rid={rid}&verifyUuid={err.verify_uuid}&verifyType={err.verify_type}')
            save_qr_png(qr_url, 'risk_qr.png')
            print('RISK_QR_SAVED @%d' % int(time.time()), flush=True)

            poll = dict(payload_base, rid=rid)
            refreshed = False
            while time.time() < deadline:
                qresp = signed_post('/api/redcaptcha/v2/qr/status/query', poll)
                qres = qresp.json() if qresp.text.strip().startswith('{') else {}
                st = (qres.get('data') or {}).get('status')
                if st == 2:
                    print('  风控码已扫，等待手机确认...', flush=True)
                elif st == 4:
                    print('  风控验证通过', flush=True)
                    return True
                elif st in (3, 5, 6):
                    print('  风控码取消/过期/失败，刷新...', flush=True)
                    refreshed = True; break
                time.sleep(1.5)
            if refreshed:
                continue
        return False
    finally:
        va.close()


def main():
    login = XHSLoginApi()
    overall = time.monotonic() + 600  # 10 分钟

    cookies, qr_id, code = prepare_login_qr(login)
    last_msg = None
    done = False

    while time.monotonic() < overall:
        try:
            success, msg, cookies = login.check_qrcode_status(qr_id, code, cookies)
        except RiskControlError as err:
            print('>> 触发风控，等待扫验证码...', flush=True)
            if risk_verify(cookies, err):
                print('>> 风控通过，继续登录...', flush=True)
                continue
            # 风控整体失败：重新准备登录码
            cookies, qr_id, code = prepare_login_qr(login)
            continue

        if msg != last_msg:
            print('  状态:', msg, flush=True)
            last_msg = msg
        if success:
            done = True; break
        if msg == '二维码已过期':
            print('  登录码过期，重新生成...', flush=True)
            cookies, qr_id, code = prepare_login_qr(login)
        time.sleep(2)

    if not done:
        print('OVERALL_TIMEOUT'); sys.exit(3)

    success, user_info, cookies = login.get_user_info(cookies)
    if not success or user_info.get('guest') is not False:
        print('VERIFY_FAIL'); sys.exit(4)

    cookies_str = login.cookies_to_str(cookies)
    cookie_path = os.environ.get('XHS_COOKIE_PATH', 'login_cookies.txt')
    with open(cookie_path, 'w') as f:
        f.write(cookies_str)
    print('LOGIN_SUCCESS nickname=%s redid=%s' % (
        user_info.get('nickname'), user_info.get('red_id')), flush=True)


if __name__ == '__main__':
    main()
