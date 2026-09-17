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

# ---- 三种通知文案（单条完整文案，含 [BotBrother] 前缀与 self_id 占位）----
# 产品拍板：告警只发这一句话；self_id 为机器人 QQ 号，探测失败拿不到时留空。

OFFLINE_ALERT = (
    "[BotBrother] 警告：{self_id}账号离线。如本次离线为您主动触发，请忽略本信息。"
)
SERVICE_ALERT = "[BotBrother] 警告：{self_id}所在客户端连接失败，请检查客户端是否离线。"
RECOVERY_NOTICE = "[BotBrother] {self_id}已恢复在线。"


class MonitorStateMachine:
    """feed(state, detail, self_id) -> list[str]：本轮结束后应发送的通知（0 或 1 条）。

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

    def feed(self, state, detail="", self_id=""):
        """处理一轮探测结果，返回该轮应发通知列表（[str]）。

        分支顺序就是业务优先级：online 收尾一切；offline/unreachable
        各自走「计数 → 达阈值且未报 → 报警」的相同逻辑。
        self_id：机器人 QQ 号，拼进文案；拿不到时留空。
        """
        if state == probe.ONLINE:
            # 在线：清两个方向的计数；若之前报过故障，补发恢复通知并复位标记
            self._offline_count = 0
            self._unreachable_count = 0
            if self._ever_alerted:
                self._ever_alerted = False
                self._offline_alerted = False
                self._unreachable_alerted = False
                return [RECOVERY_NOTICE.format(self_id=self_id)]
            return []
        if state == probe.OFFLINE:
            # 账号下线：清对向计数（unreachable），自己 +1
            self._unreachable_count = 0
            self._offline_count += 1
            if self._offline_count >= self.debounce and not self._offline_alerted:
                self._offline_alerted = True
                self._ever_alerted = True
                return [OFFLINE_ALERT.format(self_id=self_id)]
            return []
        if state == probe.UNREACHABLE:
            # 服务不可达：镜像 offline 的逻辑
            self._offline_count = 0
            self._unreachable_count += 1
            if (
                self._unreachable_count >= self.debounce
                and not self._unreachable_alerted
            ):
                self._unreachable_alerted = True
                self._ever_alerted = True
                return [SERVICE_ALERT.format(self_id=self_id)]
            return []
        # 防御：probe 之外的来源喂进来的未知状态，宁可炸出来也不静默吞
        raise ValueError("未知状态: %r" % state)
