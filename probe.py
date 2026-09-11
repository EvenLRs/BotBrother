"""探测层：调 OneBot v11 HTTP API 判断 QQ 账号在线状态。

程序结构（本模块是数据流的起点）：
    probe.probe() 被监视循环每轮调用一次
      → 按“风格”拼请求（path / envelope 两种）
      → 拿到 OneBot JSON 应答 → 解读出三种状态之一
      → 返回 (state, detail) 给状态机（statemachine.py）消费

三种状态（对外语义，其余模块都以这套常量为准）：
    online       API 可达，且应答 data.online == True（QQ 在线）
    offline      API 可达，但明确应答 online == False（账号掉线，程序还活着）
    unreachable  API 不可达/超时/403/应答不是 OneBot JSON（程序多半挂了）

风格自动识别（缓存按 base 地址记忆，多端点各自独立、互不污染）：
    path      NapCat / LLOneBot 风格：POST {base}/get_status，body {}
    envelope  SnowLuma 风格：POST {base}/，body {"action":"get_status","params":{}}
  探测顺序：先 path，遇 404/应答不认识再试 envelope，两都不行判 unreachable。
  同一 base 识别成功后记住，后续轮询直接用，省一次 404 试探。"""

import json
import urllib.error
import urllib.parse
import urllib.request

# ---- 三种状态常量（供其他模块 import，避免魔法字符串散落各处）----
ONLINE = 'online'
OFFLINE = 'offline'
UNREACHABLE = 'unreachable'

# 风格缓存：base → 'path' / 'envelope'。按端点地址分别记忆——
# 多端点监视时（比如同机 NapCat + 异机 SnowLuma），两家的调用风格不同，
# 全局单值会互相污染，第二个端点永远拿错风格多试一次 404。
_style_cache = {}


def _url(base, style):
    """按风格拼请求 URL：path 式打 /get_status，envelope 式打根路径。"""
    base = base.rstrip('/')
    if style == 'path':
        return base + '/get_status'
    return base + '/'


def _body(style):
    """按风格拼请求体：path 式空 JSON，envelope 式带 action 信封。"""
    if style == 'path':
        return json.dumps({}).encode('utf-8')
    return json.dumps({'action': 'get_status', 'params': {}}).encode('utf-8')


def _request(base, token, timeout, style):
    """按指定风格发一次 POST，返回 (http_status, 解析后的JSON或None)。

    token 同时走两条路（两种 OneBot 实现都至少支持其一）：
      - URL query  ?access_token=...
      - 请求头     Authorization: Bearer ...
    连接层失败（拒连/超时/DNS）抛 _Unreachable，由上层归类为 unreachable。
    """
    url = _url(base, style)
    if token:
        parts = urllib.parse.urlsplit(url)
        query = parts.query
        if query:
            query += '&access_token=' + urllib.parse.quote(token, safe='')
        else:
            query = 'access_token=' + urllib.parse.quote(token, safe='')
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    req = urllib.request.Request(
        url, data=_body(style), method='POST',
        headers={'Content-Type': 'application/json'})
    result_self_id = ['']
    if token:
        req.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            status = resp.status
    except urllib.error.HTTPError as e:
        # HTTP 错误码不是连接失败：读出应答体交给上层按状态码分类
        try:
            raw = e.read().decode('utf-8', 'replace')
        except Exception:
            raw = ''
        status = e.code
    except Exception as e:
        raise _Unreachable(str(e))
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except ValueError:
        parsed = None
    # get_status 应答的 data.user_id 即机器人自身 QQ 号（self_id），
    # 供告警文案使用；取不到就留空，文案退化为不带 self_id。
    if isinstance(parsed, dict):
        data = parsed.get('data')
        if isinstance(data, dict) and data.get('user_id'):
            result_self_id[0] = str(data['user_id'])
    return status, parsed, result_self_id[0]


class _Unreachable(Exception):
    """内部信号：TCP/超时层面的连接失败（区别于 HTTP 4xx/5xx）。"""


def _interpret(parsed):
    """把 OneBot 应答 JSON 解读为状态。返回 None 表示“这不像 OneBot 应答”。

    判定标准（宽松优先，兼容各家实现细节）：
      - 结构上：顶层 dict 且 data.online 存在
      - 语义上：status=ok 且 retcode=0 时按 online 定；即便 retcode 非 0，
        只要 data.online 是布尔值也按它定（有些实现掉线时 retcode 仍为 0）
    """
    if not isinstance(parsed, dict):
        return None
    data = parsed.get('data')
    if not isinstance(data, dict) or 'online' not in data:
        return None
    if parsed.get('status') == 'ok' and parsed.get('retcode') == 0:
        return ONLINE if data.get('online') else OFFLINE
    if isinstance(data.get('online'), bool):
        return ONLINE if data['online'] else OFFLINE
    return None


def probe(base, token=None, timeout=5):
    """探测一个 OneBot v11 HTTP 端点，返回 (state, detail, self_id)。

    self_id 取自 get_status 应答的 data.user_id；连接层失败时为空串。
    （告警文案需要机器人 QQ 号；探测失败拿不到时由上层自行兜底。）

    主流程（见模块 docstring 的风格识别说明）：
      1. 已知风格 → 直接用；否则先试 path 再试 envelope
      2. 拒连要“双风格确认”才判 unreachable（防止把 SnowLuma 的 404 误判）
      3. 404 → 换下一种风格；403 → token 错，直接 unreachable
      4. 应答能解读 → 记住风格、返回状态
    detail 是给日志和报警文案用的人话描述，不含内部术语（如风格名）。
    """
    global _style_cache
    cached = _style_cache.get(base)
    styles = [cached] if cached else ['path', 'envelope']
    last_detail = ''
    for style in styles:
        try:
            status, parsed, result_self_id = _request(base, token, timeout, style)
        except _Unreachable as e:
            last_detail = '连接失败: %s' % e
            # 拒连对两种风格无区别；但 path 拒连时先补验 envelope，
            # 避免把“路径不存在”误判成拒连（SnowLuma 只应答根路径）
            if style == 'path' and cached is None:
                try:
                    _request(base, token, timeout, 'envelope')
                except _Unreachable:
                    return UNREACHABLE, last_detail, ''
                continue
            if cached:
                return UNREACHABLE, last_detail, ''
            continue
        if status == 404:
            last_detail = 'HTTP 404（路径不存在）'
            continue
        if status >= 400:
            last_detail = 'HTTP %d' % status
            if status == 403:
                return UNREACHABLE, 'HTTP 403（token 鉴权失败或未授权）', ''
            continue
        state = _interpret(parsed)
        if state is not None:
            _style_cache[base] = style
            # 风格信息只进内部缓存，不进对外 detail（UI/报警不出现“path 风格”字样）
            detail = 'API 应答 online=%s' % (
                'true' if state == ONLINE else 'false')
            return state, detail, result_self_id
        last_detail = '应答不是合法 OneBot JSON'
        continue
    return UNREACHABLE, last_detail or '无法识别端点', ''
