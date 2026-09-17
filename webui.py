"""WebUI HTTP 服务（纯标准库，无框架）。

程序结构（本模块只做“HTTP 壳”，业务都在 runtime）：
    WebUIServer            起一个 ThreadingHTTPServer（每请求一线程）+ 认证管理器
    _make_handler(rt, …)   闭包工厂：把 runtime / server / auth 织进请求处理类

路由表：
    GET  /                    单文件前端 webui.html（本身无敏感信息）
    GET  /api/auth/status     认证状态：{needs_setup, locked, authenticated}
    POST /api/auth/setup      首次设置密码（仅 unset 时可用；成功即登录）
    POST /api/auth/login      密码登录
    POST /api/auth/logout     退出（失效当前会话）
    GET  /api/state           实时状态快照（需登录）
    GET  /api/config          打码配置（需登录）
    POST /api/config          热更新（需登录 + CSRF）
    POST /api/test-alert      测试通知（需登录 + CSRF）
    GET  /api/history.csv     探测历史 CSV（需登录）
    POST /api/shutdown        远程退出（需登录 + CSRF + 本次密码复核）

认证模型（取代旧的 webui.token/Bearer）：
    - 首次访问（认证文件不存在）→ 前端展示“设置密码”，设置成功后自动登录；
    - 之后必须密码登录，会话通过 HttpOnly + SameSite=Strict 的 Cookie 维持；
    - 未登录仅放行页面与 /api/auth/*，其余 API 一律 401；
    - 所有 POST 额外要求同源(Origin/Referer) 与自定义头 X-BB-CSRF: 1（兼容局域网 HTTP）；
    - 登录失败按来源 IP 限速；
    - 旧配置里的 webui.token 兼容读取但被忽略，不构成任何登录通道。
"""

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import auth as auth_mod

# 单文件前端的位置（与 webui.py 同目录）
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui.html")

_MAX_BODY = 64 * 1024  # POST 体积上限：配置 JSON 再大也到不了 64KB，防滥用
SESSION_COOKIE = "bb_session"
_CSRF_HEADER = "X-BB-CSRF"
_CSRF_VALUE = "1"


class WebUIServer:
    """HTTP 服务容器：构造即监听，start() 后台化，shutdown() 关停。"""

    def __init__(self, rt, host, port):
        self.rt = rt
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
        data_dir = auth_mod.resolve_data_dir(
            getattr(rt, "config_path", None),
            (rt.cfg.get("webui") or {}).get("data_dir"),
        )
        self.auth = auth_mod.AuthManager(data_dir, log_fn=rt.log)
        handler = _make_handler(rt, self, self.auth)
        # ThreadingHTTPServer：每个请求独立线程，慢客户端不拖累别人
        self.server = ThreadingHTTPServer((host, port), handler)

    def start(self):
        """后台线程跑事件循环（不阻塞主线程）。"""
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        state = (
            "需首次设置密码"
            if self.auth.needs_setup()
            else ("认证数据损坏已锁定" if self.auth.is_locked() else "密码登录已启用")
        )
        self.rt.log(
            "WebUI 已启动: http://%s:%d/（%s；认证数据: %s）"
            % (self.host, self.port, state, self.auth.path)
        )

    def shutdown(self):
        """线程外触发关停（serve_forever 的 shutdown 不能在自己线程里调）。"""
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def _make_handler(rt, server, auth):
    """闭包工厂：Handler 类捕住 rt / server / auth。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "BotBrother-webui/2.0"

        # ---- 基础工具：统一收发 ----

        def log_message(self, fmt, *args):
            pass  # 静默逐请求访问日志（业务日志由 runtime 记，避免刷屏）

        def _send(
            self, code, body, ctype="application/json; charset=utf-8", headers=None
        ):
            """统一响应出口：定长、禁缓存、写体。body 可 str 可 bytes。"""
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            """JSON 响应（ensure_ascii=False：中文原样输出，人读友好）。"""
            self._send(code, json.dumps(obj, ensure_ascii=False))

        def _err(self, code, msg, err_code=None):
            """错误响应统一格式 {'error': msg[, 'code']}，前端直接展示。"""
            payload = {"error": msg}
            if err_code:
                payload["code"] = err_code
            self._json(code, payload)

        # ---- 会话 / CSRF ----

        def _cookie_token(self):
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                if name.strip() == SESSION_COOKIE:
                    return value.strip()
            return None

        def _authed(self):
            return auth.validate_session(self._cookie_token())

        def _client_ip(self):
            return self.client_address[0] if self.client_address else "?"

        def _set_session_cookie(self, token, max_age):
            self.send_header(
                "Set-Cookie",
                "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                % (SESSION_COOKIE, token, max_age),
            )

        def _same_origin(self):
            """严格同源：必须有 Origin（优先）或 Referer，且 host[:port] 与 Host 一致。

            不放行任何 CORS；缺失来源头视为不可信（返回 False）。局域网 HTTP 下
            浏览器会带 Origin，非浏览器客户端需显式提供。
            """
            host = self.headers.get("Host", "")
            source = self.headers.get("Origin") or self.headers.get("Referer")
            if not host or not source:
                return False
            try:
                netloc = urllib.parse.urlsplit(source).netloc
            except ValueError:
                return False
            return netloc == host

        def _csrf_ok(self):
            """POST 保护：要求同源 + 自定义头（跨站表单无法带自定义头）。"""
            return self.headers.get(_CSRF_HEADER) == _CSRF_VALUE and self._same_origin()

        def _require_auth(self):
            """返回 True 表示已登录；否则已发 401。"""
            if self._authed():
                return True
            self._err(401, "需要登录。", "unauthenticated")
            return False

        def _read_json_body(self):
            """读取并解析 JSON 体；失败时已回 413/400 并返回 None。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._err(400, "Content-Length 非法")
                return None
            if length > _MAX_BODY:
                self._err(413, "请求体过大")
                return None
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as e:
                self._err(400, "JSON 解析失败: %s" % e)
                return None
            if not isinstance(payload, dict):
                self._err(400, "请求体必须是 JSON 对象")
                return None
            return payload

        # ---- GET 路由 ----

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            # 静态页：无需鉴权（页面本身无敏感信息，鉴权在后端 API 层）
            if path in ("/", "/index.html"):
                try:
                    with open(HTML_FILE, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    self._err(500, "webui.html 缺失")
                return
            if path == "/api/auth/status":
                self._json(
                    200,
                    {
                        "needs_setup": auth.needs_setup(),
                        "locked": auth.is_locked(),
                        "authenticated": auth.validate_session(self._cookie_token()),
                    },
                )
                return
            if not self._require_auth():
                return
            if path == "/api/state":
                self._json(200, rt.snapshot())
            elif path == "/api/config":
                self._json(200, rt.masked_config())
            elif path == "/api/history.csv":
                self._history_csv()
            else:
                self._err(404, "not found")

        # ---- POST 路由 ----

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            # 所有 POST 先过 CSRF（含未登录的 auth 端点）
            if not self._csrf_ok():
                self._err(403, "CSRF 校验失败：请从 WebUI 页面操作。", "csrf")
                return

            if path == "/api/auth/setup":
                self._handle_setup()
                return
            if path == "/api/auth/login":
                self._handle_login()
                return
            if path == "/api/auth/logout":
                self._handle_logout()
                return

            if not self._require_auth():
                return
            payload = self._read_json_body()
            if payload is None:
                return

            if path == "/api/config":
                # 热更新：校验失败 ValueError → 400 带人话错误
                try:
                    result = rt.update_config(payload)
                    self._json(200, {"ok": True, **result})
                except ValueError as e:
                    self._json(400, {"ok": False, "error": str(e)})
            elif path == "/api/test-alert":
                results = rt.send_test_alert()
                ok_any = any(r.get("ok") for r in results) or not results
                self._json(200, {"ok": ok_any, "results": results})
            elif path == "/api/shutdown":
                self._handle_shutdown(payload)
            else:
                self._err(404, "not found")

        def _handle_shutdown(self, payload):
            """远程关闭：在三重前置（已登录会话 + CSRF + 同源）之外，再复核一次本次输入的当前登录密码。

            安全要点：
              * 缺失/空/超长/错误密码一律拒绝，且绝不触发关闭回调；
              * 复用登录同一套限速（先判限速，失败记一次），不新开绕过通道；
              * 密码只用于 AuthManager.verify，绝不写入日志/响应，也不回显；
              * 不提供任何远程重置入口（重置仍只在本机 CLI）。
            """
            ip = self._client_ip()
            if auth.is_rate_limited(ip):
                self._err(429, "尝试过于频繁，请稍后再试。", "rate_limited")
                return
            password = payload.get("password")
            if not isinstance(password, str) or not password:
                auth.record_failure(ip)
                self._err(400, "请输入当前登录密码。", "password_required")
                return
            if len(password) > auth_mod.MAX_PASSWORD_LEN:
                auth.record_failure(ip)
                self._err(400, "密码过长。", "password_too_long")
                return
            if not auth.verify(password):
                auth.record_failure(ip)
                # 会话本身有效，仅本次复核密码错误 → 403（避免前端把 401 当作会话过期而登出）
                self._err(403, "密码错误。", "bad_credentials")
                return
            auth.record_success(ip)
            self._json(200, {"ok": True, "message": "正在退出……"})
            rt.log("收到 WebUI 关停请求，程序退出")
            server.shutdown()
            threading.Thread(target=_delayed_exit, daemon=True).start()

        # ---- 认证端点 ----

        def _handle_setup(self):
            payload = self._read_json_body()
            if payload is None:
                return
            password = payload.get("password")
            confirm = payload.get("confirm")
            if not isinstance(password, str) or not isinstance(confirm, str):
                self._err(400, "密码格式不正确。")
                return
            if password != confirm:
                self._err(400, "两次输入的密码不一致。")
                return
            ok, message = auth.setup(password)
            if not ok:
                # 已设置/锁定/写盘失败：区分状态码
                code = 423 if auth.is_locked() else (409 if auth.is_ready() else 400)
                self._err(code, message)
                return
            token = auth.create_session()
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._set_session_cookie(token, auth.session_ttl_seconds)
            self.end_headers()
            self.wfile.write(body)

        def _handle_login(self):
            ip = self._client_ip()
            if auth.is_rate_limited(ip):
                self._err(429, "登录尝试过于频繁，请稍后再试。", "rate_limited")
                return
            payload = self._read_json_body()
            if payload is None:
                return
            password = payload.get("password")
            if not auth.verify(password):
                auth.record_failure(ip)
                self._err(401, "密码错误。", "bad_credentials")
                return
            auth.record_success(ip)
            token = auth.create_session()
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._set_session_cookie(token, auth.session_ttl_seconds)
            self.end_headers()
            self.wfile.write(body)

        def _handle_logout(self):
            token = self._cookie_token()
            auth.revoke_session(token)
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Set-Cookie",
                "%s=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % SESSION_COOKIE,
            )
            self.end_headers()
            self.wfile.write(body)

        # ---- CSV 导出 ----

        def _history_csv(self):
            """探测历史导出：label,timestamp,state 三列（多端点各占一行），
            Excel 可直接开、可按端点筛选。"""
            with rt.lock:
                rows = []
                for ep in rt.endpoints:
                    for ts, state in ep.history:
                        rows.append((ep.label, ts, state))
            lines = ["label,timestamp,state"]
            for label, ts, state in rows:
                # CSV 转义：label 含逗号时包引号
                safe_label = (
                    '"%s"' % label.replace('"', '""')
                    if ("," in label or '"' in label)
                    else label
                )
                lines.append("%s,%s,%s" % (safe_label, ts, state))
            body = "\n".join(lines) + "\n"
            self._send(
                200,
                body,
                "text/csv; charset=utf-8",
                {"Content-Disposition": 'attachment; filename="history.csv"'},
            )

    return Handler


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
    host = rt.cfg["webui"]["bind"]
    port = rt.cfg["webui"]["port"]
    srv = WebUIServer(rt, host, port)
    srv.start()
    return srv
