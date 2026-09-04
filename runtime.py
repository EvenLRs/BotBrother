"""MonitorRuntime：监视循环的共享运行时（WebUI 与轮询线程的唯一数据源）。

程序结构（本模块是“状态中枢”）：
    MonitorRuntime 持有一切可变状态：
      - 配置 cfg（可被 WebUI 热更新）+ 落盘路径
      - 当前探测状态 / 历史环形缓冲 / 通知记录
      - 状态机实例 + 渠道实例列表
    两条消费线共用一个实例、一把 RLock：
      - poll_loop()  后台线程：每 interval 秒 probe → 状态机 → 发通知
      - WebUI 线程池：读 snapshot()/masked_config()，写 update_config()/send_test_alert()
    读多写少、块都极小，锁粒度够用；绝不存密钥明文进快照。

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
    out['debounce'] = _int('debounce（去抖轮数）', incoming.get('debounce'), 1, 10)

    p = incoming.get('probe')
    if not isinstance(p, dict):
        raise ValueError('probe 必须是对象')
    base = p.get('base')
    if not isinstance(base, str) or not (base.startswith('http://')
                                         or base.startswith('https://')):
        raise ValueError('probe.base 必须是 http:// 或 https:// 开头的地址')
    token = p.get('token', '')
    if token is None:
        token = ''
    if not isinstance(token, str):
        raise ValueError('probe.token 必须是字符串')
    out['probe'] = {'base': base, 'token': token,
                    'timeout': _int('probe.timeout（超时）', p.get('timeout', 5), 1, 60)}

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
    out['webui'] = {
        'port': _int('webui.port（端口）', w.get('port', 8080), 1, 65535),
        'bind': (w.get('bind') or '127.0.0.1'),
        'token': (w.get('token') or ''),
    }
    if not isinstance(out['webui']['bind'], str) or not out['webui']['bind'].strip():
        raise ValueError('webui.bind 必须是非空字符串')
    if not isinstance(out['webui']['token'], str):
        raise ValueError('webui.token 必须是字符串')
    return out


class MonitorRuntime:
    """一次构建，长期运行；WebUI 与轮询线程共享（线程安全靠 self.lock）。

    实例状态分三组：
      配置组   cfg / config_path / auth_token —— update_config 热更新
      运行组   state / state_detail / state_since / history —— poll_once 写
      业务组   sm（状态机）/ channels（渠道列表）—— 热更新时整体重建
    """

    def __init__(self, cfg, log_fn=None, config_path=None):
        self.cfg = cfg
        self.config_path = config_path
        self.log = log_fn if log_fn is not None else _default_log
        self.lock = threading.RLock()

        # 兼容两种 cfg 形态：monitor.load_config 的扁平键（base/token/timeout）
        # 与磁盘上的嵌套 probe 对象。统一展开成运行时扁平视图，后续只读扁平键。
        probe_cfg = cfg.get('probe') or {}
        cfg.setdefault('base', probe_cfg.get('base', 'http://127.0.0.1:3000'))
        cfg.setdefault('token', probe_cfg.get('token', ''))
        cfg.setdefault('timeout', probe_cfg.get('timeout', 5))
        cfg.setdefault('interval', 30)
        cfg.setdefault('debounce', 3)
        cfg.setdefault('channels', [{'type': 'log'}])

        # webui 配置补默认值（缺省只听本机、不鉴权——安全默认）
        w = dict(cfg.get('webui') or {})
        w.setdefault('port', 8080)
        w.setdefault('bind', '127.0.0.1')
        w.setdefault('token', '')
        cfg['webui'] = w
        self.auth_token = w['token']

        self.sm = statemachine.MonitorStateMachine(debounce=cfg['debounce'])
        self.channels = channels_mod.build_channels(cfg['channels'], log=self.log)

        # 运行状态初值（None=尚未探测过；首帧 WebUI 显示“未知”）
        self.state = None
        self.state_detail = ''
        self.state_since = None
        self.last_probe_at = None
        self.probe_count = 0
        self.started_at = time.time()
        self.history = collections.deque(maxlen=MAX_HISTORY)
        self.notifications = collections.deque(maxlen=MAX_NOTIFICATIONS)

    # ==================== 轮询（监视主循环） ====================

    def poll_once(self):
        """跑一轮：探测 → 更新运行状态 → 状态机 → 发通知。返回探测状态。

        顺序刻意为之：先落历史记录，再发通知——通知失败不影响状态记录。
        锁只罩“读配置”和“写状态”两个极小段，探测本身（可能秒级）不持锁，
        避免 WebUI 请求被探测阻塞。
        """
        with self.lock:
            base, token, timeout = self.cfg['base'], self.cfg['token'], self.cfg['timeout']
        state, detail = probe.probe(base, token or None, timeout)
        now = time.time()
        with self.lock:
            if state != self.state:
                self.state_since = now      # 状态翻转时刻，UI 据此算“持续时长”
            self.state = state
            self.state_detail = detail
            self.last_probe_at = now
            self.probe_count += 1
            self.history.append((now, state))
        for title, body in self.sm.feed(state, detail):
            self._notify(title, body)
        return state

    def poll_loop(self):
        """常驻轮询（WebUI 模式跑在后台线程里）。

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
            base = self.cfg['base']
        return self._notify('QQ 监视测试通知',
                            '如果你收到这条，说明 %s 的消息渠道配置是通的。' % base)

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

        # 掩码还原：提交值是回显掩码 → 保留原密钥（见 _unmask 文档）
        token = _unmask(validated['probe']['token'], cur['token'])

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

        # 应用：一次持锁写完全部字段；状态机与渠道列表整体重建（不可变换新，
        # 避免半新半旧的中间态）。debounce 只换数字，状态机实例不重建。
        with self.lock:
            cur['interval'] = validated['interval']
            cur['debounce'] = validated['debounce']
            cur['base'] = validated['probe']['base']
            cur['token'] = token
            cur['timeout'] = validated['probe']['timeout']
            cur['channels'] = new_channels
            cur['webui'] = {'port': w['port'], 'bind': w['bind'], 'token': wtoken}
            self.sm.debounce = validated['debounce']
            self.channels = channels_mod.build_channels(new_channels, log=self.log)
            self.auth_token = wtoken

        self._persist(cur)
        self.log('配置已通过 WebUI 更新（interval=%s，去抖=%s，渠道 %d 个）'
                 % (validated['interval'], validated['debounce'], len(new_channels)))
        return {'restart_required': restart_required}

    def _persist(self, cfg):
        """把运行时配置写回磁盘：先写 .tmp，再原子替换；旧文件备份 .bak。

        崩溃安全：os.replace 原子生效，任何时刻磁盘上都有一个完整 config。
        """
        if not self.config_path:
            return
        # 运行时是扁平键，落盘还原成用户熟悉的嵌套结构
        data = {
            'interval': cfg['interval'],
            'probe': {'base': cfg['base'], 'token': cfg['token'],
                      'timeout': cfg['timeout']},
            'debounce': cfg['debounce'],
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

        history 截最近 120 帧（UI 色条一格一轮正好画满），
        notifications 截最新 20 条（表格一页的量）。
        """
        with self.lock:
            return {
                'state': self.state,
                'state_detail': self.state_detail,
                'state_since': self.state_since,
                'last_probe_at': self.last_probe_at,
                'uptime': time.time() - self.started_at,
                'probe_count': self.probe_count,
                'interval': self.cfg['interval'],
                'debounce': self.cfg['debounce'],
                'base': self.cfg['base'],
                'channels_active': [c.name for c in self.channels],
                'webui': {'port': self.cfg['webui']['port'],
                          'bind': self.cfg['webui']['bind'],
                          'auth_required': bool(self.auth_token)},
                'history': list(self.history)[-120:],
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
            return {
                'interval': self.cfg['interval'],
                'debounce': self.cfg['debounce'],
                'probe': {'base': self.cfg['base'],
                          'token': mask_secret(self.cfg['token']),
                          'timeout': self.cfg['timeout']},
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
