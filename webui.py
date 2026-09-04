"""WebUI HTTP 服务（纯标准库，无框架）。

程序结构（本模块只做“HTTP 壳”，业务都在 runtime）：
    WebUIServer            起一个 ThreadingHTTPServer（每请求一线程）
    _make_handler(rt, …)   闭包工厂：把 runtime 和 server 织进请求处理类
    Handler 路由表：
      GET  /                 静态页 webui.html（单文件前端）
      GET  /api/state        实时状态快照 → rt.snapshot()
      GET  /api/config       打码配置 → rt.masked_config()
      POST /api/config       热更新 → rt.update_config()（校验/解掩码/落盘）
      POST /api/test-alert   测试通知 → rt.send_test_alert()
      GET  /api/history.csv  探测历史 CSV 下载
      POST /api/shutdown     远程退出（须设 token，无 token 时 403 禁用）

鉴权模型：
    不设 webui.token → 全开放（仅本机回环时是安全默认）
    设了 token       → 页面照常打开；API 必须 Authorization: Bearer <token>
    /api/shutdown    → 更严：无 token 直接 403（防止误触/匿名关停）
"""

import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import runtime as runtime_mod

# 单文件前端的位置（与 webui.py 同目录）
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webui.html')

_MAX_BODY = 64 * 1024  # POST 体积上限：配置 JSON 再大也到不了 64KB，防滥用


class WebUIServer:
    """HTTP 服务容器：构造即监听，start() 后台化，shutdown() 关停。"""

    def __init__(self, rt, host, port):
        self.rt = rt
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
        handler = _make_handler(rt, self)
        # ThreadingHTTPServer：每个请求独立线程，慢客户端不拖累别人
        self.server = ThreadingHTTPServer((host, port), handler)

    def start(self):
        """后台线程跑事件循环（不阻塞主线程）。"""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.rt.log('WebUI 已启动: http://%s:%d/（鉴权%s）'
                    % (self.host, self.port, '开' if self.rt.auth_token else '关'))

    def shutdown(self):
        """线程外触发关停（serve_forever 的 shutdown 不能在自己线程里调）。"""
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def _make_handler(rt, server):
    """闭包工厂：Handler 类捕住 rt（数据源）和 server（关停目标）。

    BaseHTTPRequestHandler 是每连接实例化的，不能用构造器传参，
    所以用闭包把依赖“焊”进类里——标准库 HTTP 服务的惯用做法。
    """

    def authed(handler_self):
        """鉴权闸（每次请求现读，热更新后立即生效）：

        规则：
          - 页面（/, /index.html）永远放行（无敏感信息，鉴权在 JS 层对 API 做）
          - bind 非回环（0.0.0.0 / 内网IP / 公网IP 等任何对外地址）且未设 token
            → 一切 API 拒绝（403）。监听对外却零鉴权等于把配置/密钥/关停
            裸奔给局域网，强制拦截。
          - 设了 token → API 必须 Authorization: Bearer <token>（401）
          - 仅回环监听且未设 token → 放行（本机信任，安全默认）
        """
        if handler_self.path in ('/', '/index.html', ''):
            return True
        if rt.auth_token:
            h = handler_self.headers.get('Authorization', '')
            return h == 'Bearer ' + rt.auth_token
        # 未设 token：仅回环监听才放行 API；非回环（任何对外地址）一律拒绝
        with rt.lock:
            bind = rt.cfg['webui'].get('bind', '127.0.0.1')
        if not bind.startswith('127.'):  # 非回环即强制（0.0.0.0 / 内网IP / 公网IP 全拦）
            return False
        return True

    class Handler(BaseHTTPRequestHandler):
        server_version = 'BotBrother-webui/1.0'

        # ---- 基础工具：统一收发 ----

        def log_message(self, fmt, *args):
            pass  # 静默逐请求访问日志（业务日志由 runtime 记，避免刷屏）

        def _send(self, code, body, ctype='application/json; charset=utf-8', headers=None):
            """统一响应出口：定长、禁缓存、写体。body 可 str 可 bytes。"""
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            """JSON 响应（ensure_ascii=False：中文原样输出，人读友好）。"""
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _err(self, code, msg):
            """错误响应统一格式 {'error': msg}，前端直接展示。"""
            self._json(code, {'error': msg})

        # ---- GET 路由 ----

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            # 静态页：无需鉴权（鉴权在 JS 层对 API 做，页面本身无敏感信息）
            if path in ('/', '/index.html'):
                try:
                    with open(HTML_FILE, 'rb') as f:
                        self._send(200, f.read(), 'text/html; charset=utf-8')
                except OSError:
                    self._err(500, 'webui.html 缺失')
                return
            if not authed(self):
                self._auth_err()
                return
            if path == '/api/state':
                self._json(200, rt.snapshot())
            elif path == '/api/config':
                self._json(200, rt.masked_config())
            elif path == '/api/history.csv':
                self._history_csv()
            else:
                self._err(404, 'not found')

        # ---- POST 路由 ----

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if not authed(self):
                self._auth_err()
                return
            # 读体 + 解析 JSON；超长直接 413（配置不可能这么大）
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if length > _MAX_BODY:
                    self._err(413, '请求体过大')
                    return
                raw = self.rfile.read(length) if length else b''
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                if not isinstance(payload, dict):
                    raise ValueError('请求体必须是 JSON 对象')
            except (ValueError, UnicodeDecodeError) as e:
                self._err(400, 'JSON 解析失败: %s' % e)
                return

            if path == '/api/config':
                # 热更新：校验失败 ValueError → 400 带人话错误
                try:
                    result = rt.update_config(payload)
                    self._json(200, {'ok': True, **result})
                except ValueError as e:
                    self._json(400, {'ok': False, 'error': str(e)})
            elif path == '/api/test-alert':
                results = rt.send_test_alert()
                ok_any = any(r.get('ok') for r in results) or not results
                self._json(200, {'ok': ok_any, 'results': results})
            elif path == '/api/shutdown':
                # 远程关停必须有 token（无 token 403）——匿名能把守护进程关掉是事故
                if not rt.auth_token:
                    self._err(403, '未设置 webui.token，远程关停已禁用')
                    return
                self._json(200, {'ok': True, 'message': '正在退出……'})
                rt.log('收到 WebUI 关停请求，程序退出')
                server.shutdown()
                threading.Thread(target=_delayed_exit, daemon=True).start()
            else:
                self._err(404, 'not found')

        def _auth_err(self):
            """鉴权失败的分流错误：设了 token=401（可带 Bearer 重试）；
            0.0.0.0 未设 token=403（配置错误，提示去设 token，重试无意义）。"""
            if rt.auth_token:
                self._err(401, '未授权：请带 Authorization: Bearer <token>')
            else:
                self._err(403, 'WebUI 监听地址非回环（%s）且未设置 webui.token，'
                               'API 已禁用：请在 config.json 的 webui.token 设置管理令牌后重启'
                               % _current_bind(rt))

        # ---- CSV 导出 ----

        def _history_csv(self):
            """探测历史导出：label,timestamp,state 三列（多端点各占一行），
            Excel 可直接开、可按端点筛选。"""
            with rt.lock:
                rows = []
                for ep in rt.endpoints:
                    for ts, state in ep.history:
                        rows.append((ep.label, ts, state))
            lines = ['label,timestamp,state']
            for label, ts, state in rows:
                # CSV 转义：label 含逗号时包引号
                safe_label = '"%s"' % label.replace('"', '""') if (',' in label or '"' in label) else label
                lines.append('%s,%s,%s' % (safe_label, ts, state))
            body = '\n'.join(lines) + '\n'
            self._send(200, body, 'text/csv; charset=utf-8',
                       {'Content-Disposition': 'attachment; filename="history.csv"'})

    return Handler


def _current_bind(rt):
    """读当前监听地址（供 403 错误信息展示，让用户知道拖住了哪个地址）。"""
    with rt.lock:
        return rt.cfg['webui'].get('bind', '127.0.0.1')


def _delayed_exit():
    """延迟 300ms 退出：先把 HTTP 响应发完再 os._exit（不留给半途连接）。"""
    import time
    time.sleep(0.3)
    import os
    import sys
    sys.stdout.flush()
    os._exit(0)


def start_webui(rt):
    """按 runtime 配置（webui.bind / webui.port）起服务。返回 server 实例。"""
    host = rt.cfg['webui']['bind']
    port = rt.cfg['webui']['port']
    srv = WebUIServer(rt, host, port)
    srv.start()
    return srv
