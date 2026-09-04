"""状态机：吃每轮 probe 结果，吐该轮应发的通知。

程序结构（本模块是核心业务规则所在）：
    监视循环每轮拿 probe 的 (state, detail) 喂给 feed()
      → 内部对 offline / unreachable 两个方向各自计数（去抖）
      → 达到阈值且未报过 → 返回一条报警通知
      → 故障转 online 且之前报过 → 返回一条恢复通知
      → 其余情况返回空列表（本轮不发）

规则（拍板值 debounce=3，可在配置里改）：
    - offline / unreachable 各自独立计数：连续 debounce 轮才触发，防网络抖动误报
    - 中途混入任何其他状态，计数清零（重新去抖）
    - 同一故障期内只报一次（不重复轰炸）
    - 文案必须区分「账号下线」与「服务退出」两种故障（这是产品的核心卖点）
"""

import probe

# ---- 三种通知文案（标题, 正文）。标题给推送渠道的短摘要用，正文带详情 ----

OFFLINE_ALERT_TITLE = 'QQ 机器人告警：QQ 账号已下线'
OFFLINE_ALERT_BODY = ('QQ 账号已下线（API 应答 online=false）。\n'
                      'NapCat/LLOneBot/SnowLuma 程序还在运行，但 QQ 登录态已掉线，'
                      '消息收发已中断，请尽快处理。')

SERVICE_ALERT_TITLE = 'QQ 机器人告警：OneBot 服务疑似意外退出'
SERVICE_ALERT_BODY = ('NapCat/LLOneBot/SnowLuma 服务疑似意外退出（API 不可达）。\n'
                      '此类程序较少手动退出，如退出多为意外退出（崩溃/被杀/服务器断电），'
                      '请尽快登录服务器查看。')

RECOVER_TITLE = 'QQ 机器人已恢复上线'
RECOVER_BODY = ('QQ 机器人已恢复上线（API 应答 online=true）。\n'
                '此前故障已自动解除，无需处理。')


class MonitorStateMachine:
    """feed(state, endpoint) -> list[(title, body)]：本轮结束后应发送的通知（0 或 1 条）。

    多端点：每个端点各自一个状态机实例（在 runtime 层维护映射），
    本类自身不感知端点身份——报警文案里的端点信息由 feed 的
    endpoint_label 参数拼进标题。

    内部状态（全部私有，调用方只管喂 state）：
      _offline_count / _unreachable_count   两个方向的去抖计数器
      _offline_alerted / _unreachable_alerted  本故障期是否已报过（防轰炸）
      _ever_alerted   报过任何故障→转 online 时补发恢复通知的依据
    """

    def __init__(self, debounce=3):
        self.debounce = debounce
        self._offline_count = 0
        self._unreachable_count = 0
        self._offline_alerted = False
        self._unreachable_alerted = False
        self._ever_alerted = False

    def feed(self, state, detail='', endpoint_label=''):
        """处理一轮探测结果，返回该轮应发通知列表（[(title, body)]）。

        分支顺序就是业务优先级：online 收尾一切；offline/unreachable
        各自走「计数 → 达阈值且未报 → 报警」的相同逻辑。
        detail 非空时附加在报警正文尾部（比如“连接失败: ...”）。
        endpoint_label：端点标识（URL 或名字），非空时拼进标题——
        多端点场景下必须知道是哪台出的事；不传则文案与单端点时代一致。
        """
        # 前缀：有标签就拼（“端点名 】 ”），没标签保持原文案
        prefix = ('【%s】 ' % endpoint_label) if endpoint_label else ''
        if state == probe.ONLINE:
            # 在线：清两个方向的计数；若之前报过故障，补发恢复通知并复位标记
            self._offline_count = 0
            self._unreachable_count = 0
            if self._ever_alerted:
                self._ever_alerted = False
                self._offline_alerted = False
                self._unreachable_alerted = False
                return [(prefix + RECOVER_TITLE, RECOVER_BODY)]
            return []
        if state == probe.OFFLINE:
            # 账号下线：清对向计数（unreachable），自己 +1
            self._unreachable_count = 0
            self._offline_count += 1
            if self._offline_count >= self.debounce and not self._offline_alerted:
                self._offline_alerted = True
                self._ever_alerted = True
                return [(prefix + OFFLINE_ALERT_TITLE,
                         OFFLINE_ALERT_BODY + ('\n详情：%s' % detail if detail else ''))]
            return []
        if state == probe.UNREACHABLE:
            # 服务不可达：镜像 offline 的逻辑
            self._offline_count = 0
            self._unreachable_count += 1
            if self._unreachable_count >= self.debounce and not self._unreachable_alerted:
                self._unreachable_alerted = True
                self._ever_alerted = True
                return [(prefix + SERVICE_ALERT_TITLE,
                         SERVICE_ALERT_BODY + ('\n详情：%s' % detail if detail else ''))]
            return []
        # 防御：probe 之外的来源喂进来的未知状态，宁可炸出来也不静默吞
        raise ValueError('未知状态: %r' % state)
