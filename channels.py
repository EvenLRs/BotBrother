"""推送渠道框架：每渠道一个类，统一 send(title, body) 接口。

程序结构：
    Channel 基类定了三段式模板：
      1. _request(title, body)  → 子类实现，返回 (url, method, headers, data)
      2. transport(url, method, headers, data) → 发送（可注入，测试用）
      3. _verify(status, resp)  → 子类可覆盖，校验应答体（errcode 等）
    监视层只认 send()，不关心渠道差异；单渠道失败抛 ChannelError，
    由调用方（monitor/runtime）记日志继续——绝不因一个渠道挂全盘。

传输层设计（transport 可替换是刻意为之）：
    - 默认 UrllibTransport：urllib.request 实现
    - 特例：HTTP 头值含非 ASCII（ntfy 中文标题）时 urllib 会拒绝
      （头值限 latin-1），自动降级到 raw-socket 直发 UTF-8 字节
      （实测 ntfy 服务端按 UTF-8 解头，2026-09-03 验证）
    - 测试注入 RecordingTransport：记录每次请求的 url/method/headers/data，
      断言与各渠道 API 规范一字不差
"""

import http.client
import json
import math
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def _header_value(value):
    """HTTP 头值兼容性预检：能否安全交给 urllib（latin-1 可编码）。

    返回 (原值, 是否可用urllib)；不能则由 UrllibTransport 切 raw-socket 路径。
    仅在需要预检的场景使用（ntfy 中文 Title）。
    """
    try:
        value.encode("latin-1")
        return value, True  # (值, 可用urllib)
    except UnicodeEncodeError:
        return value, False  # 需要 raw-socket 直发字节


class ChannelError(Exception):
    """渠道发送失败（网络错、鉴权错、应答不认）。抛给调用方记日志，不算致命。"""


class RecordingTransport:
    """测试专用 transport：按序记录每次请求，永不失败（断言素材）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, url, method, headers, data):
        self.calls.append(
            {"url": url, "method": method, "headers": dict(headers), "data": data}
        )
        return 200, b'{"ok":true}'


class UrllibTransport:
    """默认 transport：urllib.request 实现，返回 (status, body_bytes)。

    __call__ 是入口：检测头值是否含非 ASCII，无则走常规 urllib；
    有则转 _httpclient 用 socket 直发（保留 UTF-8 原始字节）。
    """

    def __call__(self, url, method, headers, data):
        utf8_headers = {k: v for k, v in headers.items() if self._needs_raw(v)}
        if not utf8_headers:
            # 常规路径：全部头值 latin-1 安全，urllib 足矣
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in headers.items():
                req.add_header(k, v)
            return self._urlopen(req, url)
        # 特殊路径：有非 ASCII 头值（如中文标题）→ raw-socket 直发
        return self._httpclient(url, method, headers, data)

    @staticmethod
    def _needs_raw(value):
        """头值需要 raw-socket 吗：非字符串不算；latin-1 编不进的要。"""
        if not isinstance(value, str):
            return False
        try:
            value.encode("latin-1")
            return False
        except UnicodeEncodeError:
            return True

    @staticmethod
    def _urlopen(req, url):
        """urllib 常规收发。HTTPError 也读出应答体（不少 API 把错误写在 4xx 体里）。"""
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            raise ChannelError("%s 请求失败: %s" % (url, e))

    @staticmethod
    def _httpclient(url, method, headers, data):
        """raw-socket 直发路径：手写 HTTP/1.1 请求行+头+体。

        为什么不用 http.client：它的 putheader 同样强制 latin-1（实测
        Python 3.9 抛 UnicodeEncodeError）。socket 层拼字节是标准库内
        唯一能发 UTF-8 头值的方式；服务端（ntfy）按 UTF-8 解析，实测通过。
        应答只解析状态行和整体——本程序只关心状态码，不做复杂分块处理。
        """
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            sock = socket.create_connection((host, port), timeout=10)
            if parts.scheme == "https":
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            # 逐行拼请求；头值原样（含 UTF-8 中文）整体 encode 成字节
            lines = [
                "%s %s HTTP/1.1" % (method, path),
                "Host: %s" % parts.netloc,
                "Connection: close",
            ]
            for k, v in headers.items():
                lines.append("%s: %s" % (k, v))
            if data is not None:
                body = data if isinstance(data, bytes) else data.encode("utf-8")
                lines.append("Content-Length: %d" % len(body))
            else:
                body = b""
            payload = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body
            sock.sendall(payload)
            # 读完整应答（Connection: close 保证服务器会主动断，循环自然结束）
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
            sock.close()
            status = int(resp.split(b" ", 2)[1])  # "HTTP/1.1 200 ..." → 200
            _, _, respbody = resp.partition(b"\r\n\r\n")  # 头体分隔
            return status, respbody
        except ChannelError:
            raise
        except Exception as e:
            raise ChannelError("%s 请求失败: %s" % (url, e))


class Channel:
    """渠道基类：模板方法模式。

    子类职责：
      - _request()：必实现。返回 (url, method, headers, data)
      - _verify()：可选。解析渠道特有的应答体（errcode/code/ok 字段），
        失败抛 ChannelError；不覆盖则 2xx 即成功
    """

    name = "channel"

    def __init__(self, transport=None):
        self.transport = transport if transport is not None else UrllibTransport()

    def send(self, title, body):
        """发送主流程：拼请求 → 传输 → 校验状态码 → 校验应答体。"""
        url, method, headers, data = self._request(title, body)
        status, resp = self.transport(url, method, headers, data)
        if status >= 300:
            raise ChannelError("%s 应答 HTTP %d: %s" % (self.name, status, resp[:200]))
        self._verify(status, resp)

    def _request(self, title, body):
        raise NotImplementedError

    def _verify(self, status, resp):
        """默认不校验应答体（2xx 已由 send 兜住）。"""


# ======================= 七个真实渠道 + 一个本地渠道 =======================
# 每个渠道一个类；构造参数与 config.json channels[] 里的字段一一对应。


class BarkChannel(Channel):
    """Bark（iOS 推送）：GET {server}/{key}/{标题}/{内容}。

    标题和内容做路径段 URL 编码（斜杠/空格/中文都不能裸进 URL）。
    """

    name = "bark"

    def __init__(self, key, server="https://api.day.app", transport=None):
        super().__init__(transport)
        self.key = key
        self.server = server.rstrip("/")

    def _request(self, title, body):
        q = urllib.parse.quote
        url = "%s/%s/%s/%s" % (
            self.server,
            q(self.key, safe=""),
            q(title, safe=""),
            q(body, safe=""),
        )
        return url, "GET", {}, None


class WecomWebhookChannel(Channel):
    """企业微信群机器人 webhook：POST JSON，msgtype=text。

    应答校验：errcode 字段非 0 视为失败（假 key 实测返回 93000）。
    """

    name = "wecom"

    def __init__(self, key, transport=None):
        super().__init__(transport)
        self.key = key

    def _request(self, title, body):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=%s" % self.key
        payload = json.dumps(
            {"msgtype": "text", "text": {"content": title + "\n" + body}}
        ).encode("utf-8")
        return url, "POST", {"Content-Type": "application/json"}, payload

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode("utf-8"))
        except Exception:
            return  # 非 JSON 应答，2xx 已由基类兜住
        if data.get("errcode") not in (0, None):
            raise ChannelError(
                "wecom errcode=%s: %s" % (data.get("errcode"), data.get("errmsg"))
            )


class DingTalkChannel(Channel):
    """钉钉自定义机器人（关键词方式）：POST JSON，结构与企微相同。

    仅支持关键词机器人；加签机器人未实现（记 BLOCKED.md 建议，
    需要 HMAC-SHA256 时间戳签名拼 URL）。
    """

    name = "dingtalk"

    def __init__(self, access_token, transport=None):
        super().__init__(transport)
        self.access_token = access_token

    def _request(self, title, body):
        url = "https://oapi.dingtalk.com/robot/send?access_token=%s" % self.access_token
        payload = json.dumps(
            {"msgtype": "text", "text": {"content": title + "\n" + body}}
        ).encode("utf-8")
        return url, "POST", {"Content-Type": "application/json"}, payload

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode("utf-8"))
        except Exception:
            return
        if data.get("errcode") not in (0, None):
            raise ChannelError(
                "dingtalk errcode=%s: %s" % (data.get("errcode"), data.get("errmsg"))
            )


# --------------------------------------------------------------------------- #
# 飞书应用机器人（自建应用，tenant_access_token + im/v1/messages）
# 官方依据：
#   - 获取 tenant_access_token：POST /open-apis/auth/v3/tenant_access_token/internal
#     （body: app_id/app_secret；应答 code/msg/tenant_access_token/expire 秒）
#   - 发送消息：POST /open-apis/im/v1/messages?receive_id_type=chat_id
#     （Authorization: Bearer <tenant_access_token>；body: receive_id=配置的 chat_id/msg_type/content/uuid）
#   - 配置固定为 app_id + app_secret + chat_id（本工具只支持群会话 chat_id）
#   - token 失效错误码（服务端通用错误码）：99991661 / 99991663 / 99991665
#   - uuid 官方支持 1 小时内请求去重（用于“刷新后重试一次”不产生重复消息）
# --------------------------------------------------------------------------- #
FEISHU_BASE = "https://open.feishu.cn"
FEISHU_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_SEND_PATH = "/open-apis/im/v1/messages"
FEISHU_RECEIVE_ID_TYPE = "chat_id"  # 本工具固定使用群会话 ID
FEISHU_TOKEN_INVALID_CODES = frozenset({99991661, 99991663, 99991665})
FEISHU_TOKEN_REFRESH_BUFFER_SECONDS = 60
# 官方：tenant_access_token 最长有效期 2 小时；超出按上限安全收敛
FEISHU_TOKEN_MAX_EXPIRE_SECONDS = 7200

# 仅按已知错误码给出安全建议，不原样拼接平台 msg（可能含敏感回显）
_FEISHU_CODE_HINTS = {
    10014: "应用状态不可用（是否已停用？）",
    10015: "App Secret 错误，请核对开发者后台",
    20005: "access_token 无效",
    20013: "tenant_access_token 无效",
    230002: "机器人不在目标群，请先把应用机器人拉进群",
    230006: "应用未启用机器人能力",
    230013: "目标用户/群不在应用可用范围内",
    230027: "缺少发送消息权限，请在开发者后台申请并发布",
    230034: "receive_id 非法或与 receive_id_type 不匹配",
    230035: "无发送权限（群禁言/用户拒收/租户管控等）",
    99991661: "缺少或无效的 access token",
    99991663: "tenant_access_token 已失效",
    99991665: "tenant_access_token 非法",
}


def describe_feishu_code(code) -> str:
    """把业务码转成安全摘要（绝不回显平台 msg，避免敏感内容外泄）。"""
    hint = _FEISHU_CODE_HINTS.get(code)
    if hint:
        return "feishu 业务失败 code=%s（%s）" % (code, hint)
    return "feishu 业务失败 code=%s（详情见开发者后台/运行日志）" % code


def _coerce_expire(value):
    """expire 必须是非 bool 的正有限数；超出官方上限时收敛到上限。非法返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(min(number, FEISHU_TOKEN_MAX_EXPIRE_SECONDS))


class FeishuAppChannel(Channel):
    """飞书应用机器人渠道（废弃原 webhook 自定义机器人实现）。

    凭据（App ID / App Secret）只能获取 tenant_access_token，**不能定位接收者**；
    必须另行配置群会话 chat_id。token 带过期缓冲缓存，刷新有并发保护；
    仅当业务返回明确的“token 失效”错误码时刷新并重试一次（同 uuid 幂等）。
    """

    name = "feishu"

    def __init__(
        self,
        app_id,
        app_secret,
        chat_id="",
        transport=None,
        base=FEISHU_BASE,
    ):
        super().__init__(transport)
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self.receive_id_type = FEISHU_RECEIVE_ID_TYPE  # 固定 chat_id
        self.base = base.rstrip("/")
        self._token = None
        self._token_expire_at = 0.0
        self._lock = threading.RLock()

    # ---- token 获取/缓存 ----
    def _fetch_token(self):
        url = self.base + FEISHU_TENANT_TOKEN_PATH
        body = json.dumps(
            {"app_id": self.app_id, "app_secret": self.app_secret}
        ).encode("utf-8")
        status, resp = self.transport(
            url, "POST", {"Content-Type": "application/json; charset=utf-8"}, body
        )
        data = self._decode_json(resp)
        code = data.get("code") if isinstance(data, dict) else None
        # 严格：业务码必须是 int 且恰为 0；缺码/非 int/bool 一律按形态错误处理
        if type(code) is not int:
            if status >= 300:
                raise ChannelError(
                    "feishu 获取 tenant_access_token 失败：HTTP %d（应答缺少合法 code）"
                    % status
                )
            raise ChannelError("feishu tenant_access_token 应答缺少合法 code")
        if code != 0:
            raise ChannelError(describe_feishu_code(code))
        token = data.get("tenant_access_token")
        if not isinstance(token, str) or not token.strip():
            raise ChannelError("feishu tenant_access_token 应答缺少合法 token 字段")
        expire = _coerce_expire(data.get("expire"))
        if expire is None:
            raise ChannelError("feishu tenant_access_token 应答 expire 非法")
        with self._lock:
            self._token = token
            self._token_expire_at = time.time() + expire
        return token

    def _get_token(self, force=False, stale=None):
        """取 token。force=True 时：若当前缓存已不是本次使用的 stale token，
        说明其它请求已刷新，直接复用，避免并发重复刷新。
        """
        now = time.time()
        if (
            not force
            and self._token
            and now < self._token_expire_at - FEISHU_TOKEN_REFRESH_BUFFER_SECONDS
        ):
            return self._token
        with self._lock:  # 并发保护：同一时刻只允许一个刷新
            if force and stale is not None and self._token and self._token != stale:
                return self._token  # 已被其它请求刷新
            now = time.time()
            if (
                not force
                and self._token
                and now < self._token_expire_at - FEISHU_TOKEN_REFRESH_BUFFER_SECONDS
            ):
                return self._token
            return self._fetch_token()

    @staticmethod
    def _decode_json(resp):
        try:
            return json.loads(resp.decode("utf-8"))
        except Exception:
            return None

    # ---- 发送 ----
    def _send_once(self, text, token, msg_uuid):
        """发一次；返回业务 code（0 表示成功）。形态/HTTP 错误抛 ChannelError。"""
        query = urllib.parse.urlencode({"receive_id_type": FEISHU_RECEIVE_ID_TYPE})
        url = "%s%s?%s" % (self.base, FEISHU_SEND_PATH, query)
        payload = {
            "receive_id": self.chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
            "uuid": msg_uuid,  # 官方 1 小时去重：重试复用同一 uuid
        }
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        }
        status, resp = self.transport(
            url,
            "POST",
            headers,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        data = self._decode_json(resp)
        code = data.get("code") if isinstance(data, dict) else None
        # 先按业务码判定（官方错误常为 HTTP 非 2xx + code），避免 HTTP 提前退出
        # 掩盖可恢复的 token 失效。
        if type(code) is not int:
            if status >= 300:
                raise ChannelError(
                    "feishu 发送失败：HTTP %d（应答缺少合法 code）" % status
                )
            raise ChannelError("feishu 发送应答缺少合法 code")
        if code != 0:
            return code
        payload_data = data.get("data")
        message_id = (
            payload_data.get("message_id") if isinstance(payload_data, dict) else None
        )
        if not isinstance(message_id, str) or not message_id.strip():
            raise ChannelError("feishu 发送应答缺少合法 message_id")
        return 0

    def send(self, title, body):
        if not self.chat_id:
            raise ChannelError(
                "feishu 未配置群会话 chat_id（App 凭据无法定位接收者，请填写 chat_id）。"
            )
        text = title + "\n" + body
        msg_uuid = uuid.uuid4().hex  # 同一次发送（含唯一一次重试）共用，供官方去重
        used_token = self._get_token()
        code = self._send_once(text, used_token, msg_uuid)
        if code in FEISHU_TOKEN_INVALID_CODES:
            # 仅明确的 token 失效才刷新；同一请求只重试一次（同 uuid）。
            # 若并发中已被其它请求换新，则复用其新 token，不再重复刷新。
            used_token = self._get_token(force=True, stale=used_token)
            code = self._send_once(text, used_token, msg_uuid)
        if code != 0:
            raise ChannelError(describe_feishu_code(code))


class LegacyFeishuWebhookChannel:
    """旧版飞书 webhook（hook_id）占位：保留配置与掩码，发送时提示迁移。

    目的：旧配置不被静默丢弃，也不会让单个旧渠道阻断监视或其它渠道。
    """

    name = "feishu"

    def __init__(self, hook_id=""):
        self.hook_id = hook_id

    def send(self, title, body):
        raise ChannelError(
            "飞书 Webhook 渠道已废弃：请在 WebUI 将“飞书”渠道改为“飞书应用机器人”"
            "并填写 App ID、App Secret、接收者类型与 ID 后保存。"
        )


class LegacyFeishuMisconfiguredChannel:
    """旧配置使用了非 chat_id 的接收者类型：保留 App 凭据，但明确要求重填 chat_id。

    不得把用户 ID（open_id/user_id/union_id/email）静默当作 chat_id 投递。
    """

    name = "feishu"

    def __init__(self, app_id="", app_secret="", receive_id_type=""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.receive_id_type = receive_id_type

    def send(self, title, body):
        raise ChannelError(
            "飞书旧配置的接收者类型为 %s，本工具仅支持群会话 chat_id："
            "请在 WebUI 重新填写 chat_id 后保存（不会把用户 ID 当作 chat_id 投递）。"
            % (self.receive_id_type or "未知")
        )


class NtfyChannel(Channel):
    """ntfy：POST {server}/{topic}，裸正文做 body，标题走 Title 头。

    免鉴权、跨平台；中文标题会触发 UrllibTransport 的 raw-socket 路径
    （见模块头部传输层设计说明）。server 参数可指向自建实例。
    """

    name = "ntfy"

    def __init__(self, topic, server="https://ntfy.sh", transport=None):
        super().__init__(transport)
        self.topic = topic
        self.server = server.rstrip("/")

    def _request(self, title, body):
        url = "%s/%s" % (self.server, urllib.parse.quote(self.topic, safe=""))
        # Title 头原样传中文；非 latin-1 头值由 UrllibTransport 自动切 raw-socket
        return url, "POST", {"Title": title}, body.encode("utf-8")


class TelegramChannel(Channel):
    """Telegram Bot：POST sendMessage，表单字段 chat_id + text。

    应答校验：ok 字段为 false 视为失败（假 token 实测 401 Unauthorized）。
    """

    name = "telegram"

    def __init__(self, bot_token, chat_id, transport=None):
        super().__init__(transport)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _request(self, title, body):
        url = "https://api.telegram.org/bot%s/sendMessage" % self.bot_token
        form = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": title + "\n" + body}
        )
        return (
            url,
            "POST",
            {"Content-Type": "application/x-www-form-urlencoded"},
            form.encode("utf-8"),
        )

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode("utf-8"))
        except Exception:
            return
        if data.get("ok") is False:
            raise ChannelError("telegram: %s" % data.get("description"))


class ServerChanChannel(Channel):
    """Server 酱 Turbo（微信推送）：POST {send_key}.send，表单 title + desp。"""

    name = "serverchan"

    def __init__(self, send_key, transport=None):
        super().__init__(transport)
        self.send_key = send_key

    def _request(self, title, body):
        url = "https://sctapi.ftqq.com/%s.send" % self.send_key
        form = urllib.parse.urlencode({"title": title, "desp": body})
        return (
            url,
            "POST",
            {"Content-Type": "application/x-www-form-urlencoded"},
            form.encode("utf-8"),
        )

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode("utf-8"))
        except Exception:
            return
        if data.get("code") not in (0, None):
            raise ChannelError(
                "serverchan code=%s: %s" % (data.get("code"), data.get("message"))
            )


class LogChannel:
    """本地渠道：不发送，只把通知写成结构化一行进日志。

    用途：①渠道自测（看清将要发什么）②服务器上没配推送时的兜底，
    保证通知记录不为空。不是 Channel 子类——没有网络请求，不需要 transport。
    """

    name = "log"

    def __init__(self, log=None):
        self.log = log if log is not None else print

    def send(self, title, body):
        # 换行折成 ' / '，保证单行日志可 grep
        self.log("[log-channel] %s | %s" % (title, body.replace("\n", " / ")))


# 渠道类型注册表：config.json channels[].type 的字符串 → 类。
# 新增渠道 = 写类 + 在这里登记，其余零改动。
CHANNEL_TYPES = {
    "bark": BarkChannel,
    "wecom": WecomWebhookChannel,
    "dingtalk": DingTalkChannel,
    "feishu": FeishuAppChannel,
    "ntfy": NtfyChannel,
    "telegram": TelegramChannel,
    "serverchan": ServerChanChannel,
    "log": LogChannel,
}


def build_channels(config_list, transport=None, log=None):
    """按配置数组建渠道实例列表。

    config_list 每项形如 {'type': 'bark', 'key': '...'}——type 之外的
    字段原样传给渠道类构造器（多传/少传由构造器自己报 TypeError）。
    log 参数专门喂给 LogChannel。
    """
    channels = []
    for item in config_list or []:
        ctype = item.get("type")
        cls = CHANNEL_TYPES.get(ctype)
        if cls is None:
            raise ValueError("未知渠道类型: %r" % ctype)
        params = {k: v for k, v in item.items() if k != "type"}
        if ctype == "log":
            channels.append(LogChannel(log=log))
        elif ctype == "feishu":
            has_app = bool(
                str(item.get("app_id", "")).strip()
                or str(item.get("app_secret", "")).strip()
            )
            chat_id = str(item.get("chat_id", "")).strip()
            legacy_receive = str(item.get("receive_id", "")).strip()
            legacy_type = str(
                item.get("receive_id_type", "chat_id") or "chat_id"
            ).strip()
            if not chat_id and legacy_receive and legacy_type == "chat_id":
                chat_id = legacy_receive  # 旧 chat_id 配置无损归一
            if has_app:
                if not chat_id and legacy_receive and legacy_type != "chat_id":
                    channels.append(
                        LegacyFeishuMisconfiguredChannel(
                            app_id=item.get("app_id", ""),
                            app_secret=item.get("app_secret", ""),
                            receive_id_type=legacy_type,
                        )
                    )
                else:
                    channels.append(
                        FeishuAppChannel(
                            app_id=item.get("app_id", ""),
                            app_secret=item.get("app_secret", ""),
                            chat_id=chat_id,
                            transport=transport,
                        )
                    )
            else:
                channels.append(
                    LegacyFeishuWebhookChannel(hook_id=item.get("hook_id", ""))
                )
        else:
            channels.append(cls(transport=transport, **params))
    return channels
