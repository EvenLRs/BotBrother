"""MonitorRuntime：监视循环的共享运行时（WebUI 与轮询线程的唯一数据源）。

多端点支持（2026-09-04 迭代）：
    配置里的 endpoints[] 列表定义多个被监视的 OneBot HTTP 服务端，
    每个端点有独立的名字（label，用于报警文案与页面展示）、探测参数
    和去抖轮数；运行时给每个端点维护独立的状态机、历史、计数。
    旧单端点写法（probe{base,token,timeout}）自动转换成长度为 1 的
    endpoints 列表——旧配置文件不改也能跑（向后兼容）。

    监视循环 poll_loop 依次轮询所有端点（串行，每轮间隔 interval 秒）；
    端点级超时/拒连各自独立计时，不会互相拖慢（串行轮询只叠加
    各端点的探测耗时，总耗时 = Σtimeout 最坏情况）。

程序结构（本模块是“状态中枢”）：
    MonitorRuntime 持有一切可变状态：
      - 配置 cfg（可被 WebUI 热更新）+ 落盘路径
      - 每端点的当前状态 / 历史环形缓冲 / 计数，聚合通知记录
      - 每端点一个状态机实例 + 共享的渠道实例列表
    两条消费线共用一个实例、一把 RLock：
      - poll_loop()  后台线程：每 interval 秒轮询全部端点
      - WebUI 线程池：读 snapshot()/masked_config()，写 update_config()等

安全设计（WebUI 密钥保护的三件套）：
      1. mask_secret()   API 回显一律 '****'+末4位
      2. _unmask()       提交回来的是掩码 → 沿用原值（不覆盖）
      3. SECRET_FIELDS   哪些字段算密钥的唯一登记处
"""

import collections
import json
import os
import threading
import time

import channels as channels_mod
import probe
import statemachine

MAX_HISTORY = 720        # 历史环形缓冲容量：30s 一轮 ≈ 6 小时
MAX_NOTIFICATIONS = 100  # 通知记录容量（WebUI 只显示前 20 条）

# 各渠道的密钥字段：API 输出时打码；提交回掩码值视为「不修改」。
# 新增渠道若含密钥，必须在这里登记，否则会明文外泄到浏览器。
SECRET_FIELDS = {
    'bark': ('key',),
    'wecom': ('key',),
    'dingtalk': ('access_token',),
    'feishu': ('hook_id',),
    'ntfy': ('topic',),
    'telegram': ('bot_token', 'chat_id'),
    'serverchan': ('send_key',),
}

# 每渠道必填字段（log 无）。WebUI 提交配置时的完整性校验依据。
REQUIRED_FIELDS = {
    'bark': ('key',),
    'wecom': ('key',),
    'dingtalk': ('access_token',),
    'feishu': ('hook_id',),
    'ntfy': ('topic',),
    'telegram': ('bot_token', 'chat_id'),
    'serverchan': ('send_key',),
    'log': (),
}


def mask_secret(value):
    """密钥打码：'****'+末4位（≤8 位只留 ****）；空串原样（表示未设置）。

    保留末 4 位是为了让用户能在页面上辨认“这是不是我刚才存的那个 key”。
    """
    if not value:
        return value if value == '' else '****'
    if not isinstance(value, str):
        return '****'
    if len(value) <= 8:
        return '****'
    return '****' + value[-4:]


def _unmask(incoming, current):
    """解掩码：提交值恰好等于当前值的掩码 → 保持原值；否则按新值处理。

    这是“掩码回显不改原值”的判定核心——用户没动过输入框时，
    提交上来的就是 mask_secret(原值)，还原成原值避免把 **** 写进配置。
    """
    if incoming and current and incoming == mask_secret(current):
        return current
    return incoming


def _validate_config(incoming):
    """校验 WebUI 提交的配置。不合法抛 ValueError（消息直接展示给用户）。

    校验范围：interval/debounce/timeout 数值范围、base 必须是 http(s)://、
    channels 数组内每项 type 合法且必填字段非空、webui 各字段类型。
    通过则返回规范化后的 dict（结构与磁盘 config.json 一致）。
    """
    if not isinstance(incoming, dict):
        raise ValueError('请求体必须是 JSON 对象')

    def _int(name, value, lo, hi):
        # bool 是 int 子类，先排除；再卡范围
        if isinstance(value, bool) or not isinstance(value, int) \
                or not lo <= value <= hi:
            raise ValueError('%s 必须是 %d-%d 之间的整数' % (name, lo, hi))
        return value

    out = {}
    out['interval'] = _int('interval（轮询间隔）', incoming.get('interval'), 5, 86400)

    # 多端点：endpoints 数组，每项 base 必填、token/timeout/debounce/label 可选
    eps = incoming.get('endpoints')
    if not isinstance(eps, list) or not eps:
        raise ValueError('endpoints 必须是非空数组（至少一个被监视端点）')
    out_eps = []
    for i, item in enumerate(eps):
        if not isinstance(item, dict):
            raise ValueError('endpoints[%d] 必须是对象' % i)
        base = item.get('base')
        if not isinstance(base, str) or not (base.startswith('http://')
                                             or base.startswith('https://')):
            raise ValueError('endpoints[%d].base 必须是 http:// 或 https:// 开头的地址' % i)
        token = item.get('token', '')
        if token is None:
            token = ''
        if not isinstance(token, str):
            raise ValueError('endpoints[%d].token 必须是字符串' % i)
        label = item.get('label') or base
        if not isinstance(label, str) or not label.strip():
            raise ValueError('endpoints[%d].label 必须是非空字符串' % i)
        out_eps.append({
            'label': label,
            'base': base,
            'token': token,
            'timeout': _int('endpoints[%d].timeout（超时）' % i,
                            item.get('timeout', 5), 1, 60),
            'debounce': _int('endpoints[%d].debounce（去抖）' % i,
                             item.get('debounce', 3), 1, 10),
        })
    # 去重检查：同 base 同 token 只允许一次
    keys = [(e['base'], e['token']) for e in out_eps]
    if len(keys) != len(set(keys)):
        raise ValueError('endpoints 存在重复项（同地址同令牌只能配一次）')
    out['endpoints'] = out_eps

    chs = incoming.get('channels')
    if not isinstance(chs, list):
        raise ValueError('channels 必须是数组')
    for i, item in enumerate(chs):
        if not isinstance(item, dict) or 'type' not in item:
            raise ValueError('channels[%d] 缺少 type' % i)
        t = item['type']
        if t not in channels_mod.CHANNEL_TYPES:
            raise ValueError('channels[%d] 未知渠道类型 %r' % (i, t))
        for f in REQUIRED_FIELDS[t]:
            v = item.get(f)
            if not isinstance(v, str) or not v.strip():
                raise ValueError('channels[%d]（%s）缺少必填字段 %s' % (i, t, f))
    out['channels'] = chs

    w = incoming.get('webui') or {}
    if not isinstance(w, dict):
        raise ValueError('webui 必须是对象')
    # port=0 是「系统分配」哨兵（测试/临时场景），放行；真实端口才卡 1-65535
    port = w.get('port', 8080)
    if port != 0:
        port = _int('webui.port（端口）', port, 1, 65535)
    out['webui'] = {
        'port': port,
        'bind': (w.get('bind') or '127.0.0.1'),
        'token': (w.get('token') or ''),
    }
    if not isinstance(out['webui']['bind'], str) or not out['webui']['bind'].strip():
        raise ValueError('webui.bind 必须是非空字符串')
    if not isinstance(out['webui']['token'], str):
        raise ValueError('webui.token 必须是字符串')
    return out


class _EndpointState:
    """单个端点的运行时状态（内部类，不外泄）。

    每端点独立持有：状态机 / 当前状态 / 历史 / 计数——
    这样端点 A 的去抖计数永远不会被端点 B 的轮询结果污染。
    """

    def __init__(self, label, debounce):
        self.label = label            # 展示名（报警文案、WebUI）
        self.sm = statemachine.MonitorStateMachine(debounce=debounce)
        self.state = None            # online/offline/unreachable，None=尚未探测
        self.state_detail = ''
        self.state_since = None
        self.last_probe_at = None
        self.probe_count = 0
        self.history = collections.deque(maxlen=MAX_HISTORY)


def normalize_endpoints(cfg):
    """把任意历史形态的端点配置统一成 endpoints 列表形态。

    支持的输入（优先级从高到低）：
      1. endpoints 数组（新多端点写法）：每项 {label?, base, token?, timeout?, debounce?}
      2. 旧单端点写法：probe{base,token,timeout} + 顶层 debounce
    返回统一列表，每项含全部字段（缺省值已填）；label 缺省用 base 本身。
    """
    if cfg.get('endpoints'):
        eps = []
        for i, item in enumerate(cfg['endpoints']):
            if not isinstance(item, dict) or not item.get('base'):
                raise ValueError('endpoints[%d] 缺少 base' % i)
            eps.append({
                'label': item.get('label') or item['base'],
                'base': item['base'],
                'token': item.get('token', ''),
                'timeout': item.get('timeout', 5),
                'debounce': item.get('debounce', cfg.get('debounce', 3)),
            })
        # 去重：同 base 同 token 只保留第一个
        seen, deduped = set(), []
        for e in eps:
            key = (e['base'], e['token'])
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        return deduped
    # 旧单端点写法自动转换
    p = cfg.get('probe') or {}
    base = cfg.get('base') or p.get('base') or 'http://127.0.0.1:3000'
    return [{
        'label': p.get('label') or base,
        'base': base,
        'token': cfg.get('token', p.get('token', '')),
        'timeout': cfg.get('timeout', p.get('timeout', 5)),
        'debounce': cfg.get('debounce', 3),
    }]


class MonitorRuntime:
    """一次构建，长期运行；WebUI 与轮询线程共享（线程安全靠 self.lock）。

    多端点：cfg['endpoints'] 定义端点列表；每端点一个 _EndpointState；
    poll_once 依次探测全部端点，聚合进同一份通知记录与渠道列表。
    """

    def __init__(self, cfg, log_fn=None, config_path=None):
        self.cfg = cfg
        self.config_path = config_path
        self.log = log_fn if log_fn is not None else _default_log
        self.lock = threading.RLock()

        # 端点归一化：新写法 endpoints[] 直接用；旧写法 probe{} 转 1 元素列表。
        # 归一化结果写回 cfg，让 masked_config/validate/update 走同一条路。
        cfg['endpoints'] = normalize_endpoints(cfg)
        cfg.setdefault('interval', 30)
        cfg.setdefault('channels', [{'type': 'log'}])

        # webui 配置补默认值（缺省只听本机、不鉴权——安全默认）
        w = dict(cfg.get('webui') or {})
        w.setdefault('port', 8080)
        w.setdefault('bind', '127.0.0.1')
        w.setdefault('token', '')
        cfg['webui'] = w
        self.auth_token = w['token']

        self.channels = channels_mod.build_channels(cfg['channels'], log=self.log)

        # 每端点独立运行状态
        self.endpoints = []           # [ _EndpointState ]，与 cfg['endpoints'] 同序
        self._sync_endpoint_states()
        self.started_at = time.time()
        # 聚合通知记录（所有端点共用，渠道是全局的）
        self.notifications = collections.deque(maxlen=MAX_NOTIFICATIONS)

    def _sync_endpoint_states(self):
        """按当前 cfg['endpoints'] 重建/保留端点状态。

        热更新后：已有的端点（同 base 同 token）保留历史与计数，
        新增的建新状态，删掉的丢弃——改地址/令牌视为新端点。
        """
        with self.lock:
            old = {}
            for e in self.endpoints:
                old[(getattr(e, 'base', None), getattr(e, '_token', None))] = e
            new_list = []
            for item in self.cfg['endpoints']:
                key = (item['base'], item['token'])
                e = old.get(key)
                if e is None:
                    e = _EndpointState(item['label'], item['debounce'])
                    e.base = item['base']
                    e._token = item['token']
                    e.timeout = item['timeout']
                else:
                    e.label = item['label']
                    e.sm.debounce = item['debounce']
                    e.timeout = item['timeout']
                new_list.append(e)
            self.endpoints = new_list

    # ==================== 轮询（监视主循环） ====================

    def poll_once(self):
        """跑一轮：逐端点探测 → 各自更新状态 → 各自状态机 → 发通知。

        返回「最差端点状态」——全部在线才 online；任一不可达优先报 unreachable
        （服务退出比账号下线更需要人立即处理）。CLI --once 沿用此语义。
        顺序刻意为之：先落历史，再发通知——通知失败不影响状态记录。
        锁只罩读写状态的小段，探测本身（可能秒级）不持锁。
        """
        worst = probe.ONLINE
        rank = {probe.ONLINE: 0, probe.OFFLINE: 1, probe.UNREACHABLE: 2}
        for ep in list(self.endpoints):        # 快照一份，热更新不冲击本轮
            with self.lock:
                base, token, timeout = ep.base, ep._token, getattr(ep, 'timeout', 5)
                label = ep.label
            state, detail = probe.probe(base, token or None, timeout)
            now = time.time()
            with self.lock:
                if state != ep.state:
                    ep.state_since = now
                ep.state = state
                ep.state_detail = detail
                ep.last_probe_at = now
                ep.probe_count += 1
                ep.history.append((now, state))
            # 端点自己的状态机，报警文案带上端点名
            for title, body in ep.sm.feed(state, detail, label):
                self._notify(title, body)
            if rank.get(state, 0) > rank.get(worst, 0):
                worst = state
        return worst

    def poll_loop(self):
        """常驻轮询（WebUI 模式的后台线程）：每轮串行探测全部端点。

        单轮异常吃掉记日志继续——监视进程自己绝不能挂（否则谁来看门）。
        interval 每轮从配置现读，热更新改间隔下一轮就生效。
        """
        while True:
            try:
                self.poll_once()
            except Exception as e:
                self.log('轮询异常（继续运行）：%s' % e)
            with self.lock:
                interval = self.cfg['interval']
            time.sleep(interval)

    # ==================== 通知 ====================

    def send_all(self, title, body):
        """向所有渠道广播；单渠道失败记日志继续。返回逐渠道结果列表。

        结果形如 [{'channel':'ntfy','ok':True}, {'channel':'wecom','ok':False,'error':...}]，
        WebUI 的“发测试通知”直接展示这份结果。
        """
        results = []
        for ch in self.channels:
            try:
                ch.send(title, body)
                self.log('通知已发往渠道 %s：%s' % (ch.name, title))
                results.append({'channel': ch.name, 'ok': True})
            except channels_mod.ChannelError as e:
                self.log('渠道 %s 发送失败（已跳过，不影响其他渠道）：%s' % (ch.name, e))
                results.append({'channel': ch.name, 'ok': False, 'error': str(e)})
        return results

    def _notify(self, title, body):
        """发送 + 记进通知环形缓冲（最新的在前，appendleft）。"""
        results = self.send_all(title, body)
        with self.lock:
            self.notifications.appendleft(
                {'ts': time.time(), 'title': title, 'body': body, 'results': results})
        return results

    def send_test_alert(self):
        """发一条测试通知到所有渠道（结果同步进通知记录，页面上立刻可见）。"""
        with self.lock:
            eps = [e.label for e in self.endpoints]
        target = '、'.join(eps) if eps else '（未配置端点）'
        return self._notify('QQ 监视测试通知',
                            '如果你收到这条，说明监视 %s 的消息渠道配置是通的。' % target)

    # ==================== 配置热更新 ====================

    def update_config(self, incoming):
        """WebUI 提交配置的完整流水线：校验 → 解掩码 → 应用 → 落盘。

        返回 {'restart_required': bool}——端口/监听地址变化时进程内
        无法原地换 socket，提示用户重启；其余全部即时生效。
        """
        validated = _validate_config(incoming)
        with self.lock:
            cur = self.cfg
            cur_channels = cur['channels']

        # 端点 token 掩码还原：逐端点对位（同 base 同 token 视为未变）
        cur_eps = {(e['base'], e.get('token', '')): e for e in cur.get('endpoints', [])}
        new_endpoints = []
        for item in validated['endpoints']:
            item = dict(item)
            old_ep = cur_eps.get((item['base'], item.get('token', '')))
            if item.get('token') and old_ep is None:
                # 新地址或改了 token：掩码可能对不上旧端点，尝试按 base 对位
                by_base = {e['base']: e for e in cur.get('endpoints', [])}
                old_ep = by_base.get(item['base'])
            if item.get('token'):
                old_token = old_ep.get('token', '') if old_ep else ''
                item['token'] = _unmask(item['token'], old_token)
            new_endpoints.append(item)

        # 渠道密钥逐项还原。对位规则：同类型渠道按出现序号一一对应
        # （配置里两个 bark，第 1 个对旧的第 1 个）——够用且无需额外 ID。
        cur_by_type = {}
        for it in cur_channels:
            cur_by_type.setdefault(it.get('type'), []).append(it)
        seen = {}
        new_channels = []
        for item in validated['channels']:
            item = dict(item)
            t = item.get('type')
            idx = seen.get(t, 0)
            seen[t] = idx + 1
            lst = cur_by_type.get(t, [])
            cur_item = lst[idx] if idx < len(lst) else None
            for f in SECRET_FIELDS.get(t, ()):
                if f in item and cur_item is not None:
                    item[f] = _unmask(item[f], cur_item.get(f, ''))
            new_channels.append(item)

        w = validated['webui']
        wtoken = _unmask(w['token'], cur['webui'].get('token', ''))
        # 端口 0 是测试用的“系统分配”哨兵，不参与变化比较；
        # 真实的 bind/port 变化才要求重启（socket 已被监听，改不了）
        cur_port = cur['webui'].get('port', 8080)
        restart_required = (w['port'] != cur_port and cur_port != 0) \
            or (w['bind'] != cur['webui'].get('bind'))

        # 应用：一次持锁写完全部字段；渠道列表整体重建（不可变换新，
        # 避免半新半旧的中间态）；端点状态按 base+token 对位保留历史。
        with self.lock:
            cur['interval'] = validated['interval']
            cur['endpoints'] = new_endpoints
            cur['channels'] = new_channels
            cur['webui'] = {'port': w['port'], 'bind': w['bind'], 'token': wtoken}
            self._sync_endpoint_states()
            self.channels = channels_mod.build_channels(new_channels, log=self.log)
            self.auth_token = wtoken

        self._persist(cur)
        self.log('配置已通过 WebUI 更新（interval=%s，端点 %d 个，渠道 %d 个）'
                 % (validated['interval'], len(new_endpoints), len(new_channels)))
        return {'restart_required': restart_required}

    def _persist(self, cfg):
        """把运行时配置写回磁盘：先写 .tmp，再原子替换；旧文件备份 .bak。

        崩溃安全：os.replace 原子生效，任何时刻磁盘上都有一个完整 config。
        """
        if not self.config_path:
            return
        # 落盘统一用 endpoints 新结构（旧 probe 写法已被 __init__ 归一化）
        data = {
            'interval': cfg['interval'],
            'endpoints': cfg['endpoints'],
            'channels': cfg['channels'],
            'webui': cfg['webui'],
        }
        old = None
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r', encoding='utf-8') as f:
                old = f.read()
        tmp = self.config_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        if old is not None:
            with open(self.config_path + '.bak', 'w', encoding='utf-8') as f:
                f.write(old)
        os.replace(tmp, self.config_path)

    # ==================== WebUI 读接口 ====================

    def snapshot(self):
        """实时状态快照（/api/state 的数据源）。绝不包含任何密钥明文。

        多端点：endpoints 数组逐端点给出状态/历史；顶层 state 是聚合
        最差值（保持旧字段兼容单端点页面），端点色条取自各端点 history。
        """
        with self.lock:
            rank = {probe.ONLINE: 0, probe.OFFLINE: 1, probe.UNREACHABLE: 2}
            worst, worst_ep = None, None
            eps_out = []
            for ep in self.endpoints:
                if worst is None or rank.get(ep.state, 0) > rank.get(worst, 0):
                    worst, worst_ep = ep.state, ep
                eps_out.append({
                    'label': ep.label,
                    'base': ep.base,
                    'state': ep.state,
                    'state_detail': ep.state_detail,
                    'state_since': ep.state_since,
                    'last_probe_at': ep.last_probe_at,
                    'probe_count': ep.probe_count,
                    'history': list(ep.history)[-120:],
                })
            return {
                'state': worst_ep.state if worst_ep else None,
                'state_detail': (worst_ep.state_detail if worst_ep else ''),
                'state_since': (worst_ep.state_since if worst_ep else None),
                'last_probe_at': (worst_ep.last_probe_at if worst_ep else None),
                'uptime': time.time() - self.started_at,
                'interval': self.cfg['interval'],
                'base': '、'.join(e['label'] for e in self.cfg['endpoints']),
                'endpoints': eps_out,
                'channels_active': [c.name for c in self.channels],
                'webui': {'port': self.cfg['webui']['port'],
                          'bind': self.cfg['webui']['bind'],
                          'auth_required': bool(self.auth_token)},
                'notifications': list(self.notifications)[:20],
            }

    def masked_config(self):
        """可编辑配置（/api/config 的数据源）：结构与 config.json 一致，密钥打码。

        配合 update_config 的 _unmask 闭环：打码值原样提交 = 不改。
        """
        with self.lock:
            channels_out = []
            for item in self.cfg['channels']:
                it = dict(item)
                for f in SECRET_FIELDS.get(it.get('type'), ()):
                    if it.get(f):
                        it[f] = mask_secret(it[f])
                channels_out.append(it)
            eps_out = []
            for item in self.cfg['endpoints']:
                it = dict(item)
                if it.get('token'):
                    it['token'] = mask_secret(it['token'])
                eps_out.append(it)
            return {
                'interval': self.cfg['interval'],
                'endpoints': eps_out,
                'channels': channels_out,
                'webui': {'port': self.cfg['webui']['port'],
                          'bind': self.cfg['webui']['bind'],
                          'token': mask_secret(self.cfg['webui'].get('token', ''))},
            }


def _default_log(msg):
    """默认日志：带时间戳写 stdout（被重定向到文件时即为完整运行日志）。"""
    import sys
    sys.stdout.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stdout.flush()
