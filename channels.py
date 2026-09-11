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
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request


def _header_value(value):
    """HTTP 头值兼容性预检：能否安全交给 urllib（latin-1 可编码）。

    返回 (原值, 是否可用urllib)；不能则由 UrllibTransport 切 raw-socket 路径。
    仅在需要预检的场景使用（ntfy 中文 Title）。
    """
    try:
        value.encode('latin-1')
        return value, True   # (值, 可用urllib)
    except UnicodeEncodeError:
        return value, False  # 需要 raw-socket 直发字节


class ChannelError(Exception):
    """渠道发送失败（网络错、鉴权错、应答不认）。抛给调用方记日志，不算致命。"""


class RecordingTransport:
    """测试专用 transport：按序记录每次请求，永不失败（断言素材）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, url, method, headers, data):
        self.calls.append({'url': url, 'method': method,
                           'headers': dict(headers), 'data': data})
        return 200, b'{"ok":true}'


class UrllibTransport:
    """默认 transport：urllib.request 实现，返回 (status, body_bytes)。

    __call__ 是入口：检测头值是否含非 ASCII，无则走常规 urllib；
    有则转 _httpclient 用 socket 直发（保留 UTF-8 原始字节）。
    """

    def __call__(self, url, method, headers, data):
        utf8_headers = {k: v for k, v in headers.items()
                        if self._needs_raw(v)}
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
            value.encode('latin-1')
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
            raise ChannelError('%s 请求失败: %s' % (url, e))

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
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        try:
            sock = socket.create_connection((host, port), timeout=10)
            if parts.scheme == 'https':
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            # 逐行拼请求；头值原样（含 UTF-8 中文）整体 encode 成字节
            lines = ['%s %s HTTP/1.1' % (method, path),
                     'Host: %s' % parts.netloc,
                     'Connection: close']
            for k, v in headers.items():
                lines.append('%s: %s' % (k, v))
            if data is not None:
                body = data if isinstance(data, bytes) else data.encode('utf-8')
                lines.append('Content-Length: %d' % len(body))
            else:
                body = b''
            payload = '\r\n'.join(lines).encode('utf-8') + b'\r\n\r\n' + body
            sock.sendall(payload)
            # 读完整应答（Connection: close 保证服务器会主动断，循环自然结束）
            resp = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
            sock.close()
            status = int(resp.split(b' ', 2)[1])          # "HTTP/1.1 200 ..." → 200
            _, _, respbody = resp.partition(b'\r\n\r\n')   # 头体分隔
            return status, respbody
        except ChannelError:
            raise
        except Exception as e:
            raise ChannelError('%s 请求失败: %s' % (url, e))


class Channel:
    """渠道基类：模板方法模式。

    子类职责：
      - _request()：必实现。返回 (url, method, headers, data)
      - _verify()：可选。解析渠道特有的应答体（errcode/code/ok 字段），
        失败抛 ChannelError；不覆盖则 2xx 即成功
    """

    name = 'channel'

    def __init__(self, transport=None):
        self.transport = transport if transport is not None else UrllibTransport()

    def send(self, title, body):
        """发送主流程：拼请求 → 传输 → 校验状态码 → 校验应答体。"""
        url, method, headers, data = self._request(title, body)
        status, resp = self.transport(url, method, headers, data)
        if status >= 300:
            raise ChannelError('%s 应答 HTTP %d: %s' % (self.name, status, resp[:200]))
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

    name = 'bark'

    def __init__(self, key, server='https://api.day.app', transport=None):
        super().__init__(transport)
        self.key = key
        self.server = server.rstrip('/')

    def _request(self, title, body):
        q = urllib.parse.quote
        url = '%s/%s/%s/%s' % (self.server, q(self.key, safe=''),
                               q(title, safe=''), q(body, safe=''))
        return url, 'GET', {}, None


class WecomWebhookChannel(Channel):
    """企业微信群机器人 webhook：POST JSON，msgtype=text。

    应答校验：errcode 字段非 0 视为失败（假 key 实测返回 93000）。
    """

    name = 'wecom'

    def __init__(self, key, transport=None):
        super().__init__(transport)
        self.key = key

    def _request(self, title, body):
        url = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=%s' % self.key
        payload = json.dumps({'msgtype': 'text',
                              'text': {'content': title + '\n' + body}}).encode('utf-8')
        return url, 'POST', {'Content-Type': 'application/json'}, payload

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode('utf-8'))
        except Exception:
            return  # 非 JSON 应答，2xx 已由基类兜住
        if data.get('errcode') not in (0, None):
            raise ChannelError('wecom errcode=%s: %s' % (data.get('errcode'), data.get('errmsg')))


class DingTalkChannel(Channel):
    """钉钉自定义机器人（关键词方式）：POST JSON，结构与企微相同。

    仅支持关键词机器人；加签机器人未实现（记 BLOCKED.md 建议，
    需要 HMAC-SHA256 时间戳签名拼 URL）。
    """

    name = 'dingtalk'

    def __init__(self, access_token, transport=None):
        super().__init__(transport)
        self.access_token = access_token

    def _request(self, title, body):
        url = 'https://oapi.dingtalk.com/robot/send?access_token=%s' % self.access_token
        payload = json.dumps({'msgtype': 'text',
                              'text': {'content': title + '\n' + body}}).encode('utf-8')
        return url, 'POST', {'Content-Type': 'application/json'}, payload

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode('utf-8'))
        except Exception:
            return
        if data.get('errcode') not in (0, None):
            raise ChannelError('dingtalk errcode=%s: %s' % (data.get('errcode'), data.get('errmsg')))


class FeishuChannel(Channel):
    """飞书自定义机器人 webhook：POST JSON，msg_type=text + content.text 结构。"""

    name = 'feishu'

    def __init__(self, hook_id, transport=None):
        super().__init__(transport)
        self.hook_id = hook_id

    def _request(self, title, body):
        url = 'https://open.feishu.cn/open-apis/bot/v2/hook/%s' % self.hook_id
        payload = json.dumps({'msg_type': 'text',
                              'content': {'text': title + '\n' + body}}).encode('utf-8')
        return url, 'POST', {'Content-Type': 'application/json'}, payload

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode('utf-8'))
        except Exception:
            return
        if data.get('code') not in (0, None):
            raise ChannelError('feishu code=%s: %s' % (data.get('code'), data.get('msg')))


class NtfyChannel(Channel):
    """ntfy：POST {server}/{topic}，裸正文做 body，标题走 Title 头。

    免鉴权、跨平台；中文标题会触发 UrllibTransport 的 raw-socket 路径
    （见模块头部传输层设计说明）。server 参数可指向自建实例。
    """

    name = 'ntfy'

    def __init__(self, topic, server='https://ntfy.sh', transport=None):
        super().__init__(transport)
        self.topic = topic
        self.server = server.rstrip('/')

    def _request(self, title, body):
        url = '%s/%s' % (self.server, urllib.parse.quote(self.topic, safe=''))
        # Title 头原样传中文；非 latin-1 头值由 UrllibTransport 自动切 raw-socket
        return url, 'POST', {'Title': title}, body.encode('utf-8')


class TelegramChannel(Channel):
    """Telegram Bot：POST sendMessage，表单字段 chat_id + text。

    应答校验：ok 字段为 false 视为失败（假 token 实测 401 Unauthorized）。
    """

    name = 'telegram'

    def __init__(self, bot_token, chat_id, transport=None):
        super().__init__(transport)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _request(self, title, body):
        url = 'https://api.telegram.org/bot%s/sendMessage' % self.bot_token
        form = urllib.parse.urlencode({'chat_id': self.chat_id,
                                       'text': title + '\n' + body})
        return url, 'POST', {'Content-Type': 'application/x-www-form-urlencoded'}, form.encode('utf-8')

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode('utf-8'))
        except Exception:
            return
        if data.get('ok') is False:
            raise ChannelError('telegram: %s' % data.get('description'))


class ServerChanChannel(Channel):
    """Server 酱 Turbo（微信推送）：POST {send_key}.send，表单 title + desp。"""

    name = 'serverchan'

    def __init__(self, send_key, transport=None):
        super().__init__(transport)
        self.send_key = send_key

    def _request(self, title, body):
        url = 'https://sctapi.ftqq.com/%s.send' % self.send_key
        form = urllib.parse.urlencode({'title': title, 'desp': body})
        return url, 'POST', {'Content-Type': 'application/x-www-form-urlencoded'}, form.encode('utf-8')

    def _verify(self, status, resp):
        try:
            data = json.loads(resp.decode('utf-8'))
        except Exception:
            return
        if data.get('code') not in (0, None):
            raise ChannelError('serverchan code=%s: %s' % (data.get('code'), data.get('message')))


class LogChannel:
    """本地渠道：不发送，只把通知写成结构化一行进日志。

    用途：①渠道自测（看清将要发什么）②服务器上没配推送时的兜底，
    保证通知记录不为空。不是 Channel 子类——没有网络请求，不需要 transport。
    """

    name = 'log'

    def __init__(self, log=None):
        self.log = log if log is not None else print

    def send(self, title, body):
        # 换行折成 ' / '，保证单行日志可 grep
        self.log('[log-channel] %s | %s' % (title, body.replace('\n', ' / ')))


# 渠道类型注册表：config.json channels[].type 的字符串 → 类。
# 新增渠道 = 写类 + 在这里登记，其余零改动。
CHANNEL_TYPES = {
    'bark': BarkChannel,
    'wecom': WecomWebhookChannel,
    'dingtalk': DingTalkChannel,
    'feishu': FeishuChannel,
    'ntfy': NtfyChannel,
    'telegram': TelegramChannel,
    'serverchan': ServerChanChannel,
    'log': LogChannel,
}


def build_channels(config_list, transport=None, log=None):
    """按配置数组建渠道实例列表。

    config_list 每项形如 {'type': 'bark', 'key': '...'}——type 之外的
    字段原样传给渠道类构造器（多传/少传由构造器自己报 TypeError）。
    log 参数专门喂给 LogChannel。
    """
    channels = []
    for item in config_list or []:
        ctype = item.get('type')
        cls = CHANNEL_TYPES.get(ctype)
        if cls is None:
            raise ValueError('未知渠道类型: %r' % ctype)
        params = {k: v for k, v in item.items() if k != 'type'}
        if ctype == 'log':
            channels.append(LogChannel(log=log))
        else:
            channels.append(cls(transport=transport, **params))
    return channels
