"""BotBrother 主程序（原 qq-online-monitor）。

程序结构（入口与三种运行模式的分派）：
    main()
      → load_config() 读配置 + QQMON_ 环境变量覆盖
      → 按命令行分派三种模式：
          --test-alert  向所有渠道发一条测试通知后退出（验证配置）
          --once        单轮探测后退出，退出码 0=在线 / 1=异常（容器健康检查用）
          --webui       WebUI 模式：后台线程监视 + 浏览器控制台（见 run_webui）
          （无参）      纯命令行常驻轮询
      纯 CLI 模式用轻量三元组（probe + statemachine + channels 直接串联，
      不经 runtime），WebUI 模式用 MonitorRuntime（有共享状态和热更新）。

配置：config.json（见 config.example.json）；QQMON_ 前缀环境变量可覆盖：
  QQMON_INTERVAL / QQMON_PROBE_BASE / QQMON_PROBE_TOKEN / QQMON_PROBE_TIMEOUT / QQMON_DEBOUNCE
"""

import argparse
import json
import os
import sys
import time

import channels
import probe
import statemachine

# 配置默认值（config.json 缺字段时兜底；环境变量优先级高于文件）
DEFAULTS = {
    'interval': 30,
    'base': 'http://127.0.0.1:3000',
    'token': '',
    'timeout': 5,
    'debounce': 3,
}


def log(msg):
    """带时间戳写 stdout 的日志（stdout 被重定向时即为运行日志）。"""
    sys.stdout.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stdout.flush()


def load_config(path):
    """读配置文件 → 扁平化运行时字典。

    文件结构是嵌套的（probe.channels.webui 节），运行时全用扁平键
    （base/token/timeout…）——runtime.py 侧再反向兼容一次，两个形态互认。
    环境变量覆盖放在最后，容器里改配置不用动挂载文件。
    """
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    cfg = dict(DEFAULTS)
    cfg['interval'] = raw.get('interval', cfg['interval'])
    cfg['debounce'] = raw.get('debounce', cfg['debounce'])
    pc = raw.get('probe', {})
    cfg['base'] = pc.get('base', cfg['base'])
    cfg['token'] = pc.get('token', cfg['token'])
    cfg['timeout'] = pc.get('timeout', cfg['timeout'])
    # 环境变量覆盖（QQMON_ 前缀，容器友好）
    env = os.environ
    if 'QQMON_INTERVAL' in env:
        cfg['interval'] = int(env['QQMON_INTERVAL'])
    if 'QQMON_PROBE_BASE' in env:
        cfg['base'] = env['QQMON_PROBE_BASE']
    if 'QQMON_PROBE_TOKEN' in env:
        cfg['token'] = env['QQMON_PROBE_TOKEN']
    if 'QQMON_PROBE_TIMEOUT' in env:
        cfg['timeout'] = int(env['QQMON_PROBE_TIMEOUT'])
    if 'QQMON_DEBOUNCE' in env:
        cfg['debounce'] = int(env['QQMON_DEBOUNCE'])
    cfg['channels'] = raw.get('channels', [{'type': 'log'}])
    cfg['webui'] = raw.get('webui') or {'port': 8080, 'bind': '127.0.0.1', 'token': ''}
    return cfg


def send_all(chs, title, body):
    """CLI 模式的广播：单渠道失败仅记日志继续（不许崩整个监视进程）。"""
    for ch in chs:
        try:
            ch.send(title, body)
            log('通知已发往渠道 %s：%s' % (ch.name, title))
        except channels.ChannelError as e:
            log('渠道 %s 发送失败（已跳过，不影响其他渠道）：%s' % (ch.name, e))


def run_once(cfg, chs, sm):
    """CLI 单轮流水线：探测 → 状态机 → 发通知。返回探测状态（供 --once 定退出码）。"""
    state, detail = probe.probe(cfg['base'], cfg['token'] or None,
                                cfg['timeout'])
    log('探测结果：%s（%s）' % (state, detail))
    for title, body in sm.feed(state, detail):
        send_all(chs, title, body)
    return state


def main(argv=None):
    """命令行入口：解析参数 → 读配置 → 分派运行模式（见模块 docstring）。"""
    parser = argparse.ArgumentParser(
        description='BotBrother —— QQ 机器人在线状态监视（OneBot v11）')
    parser.add_argument('--config', default='config.json',
                        help='配置文件路径（默认 ./config.json）')
    parser.add_argument('--once', action='store_true',
                        help='只跑一轮探测后退出（HEALTHCHECK 用）')
    parser.add_argument('--test-alert', action='store_true',
                        help='向所有已配置渠道发一条测试通知后退出')
    parser.add_argument('--webui', action='store_true',
                        help='启动 WebUI（浏览器配置/看状态），同时后台持续监视')
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        log('配置文件不存在：%s（从 config.example.json 复制一份再改）' % args.config)
        return 2
    except (ValueError, KeyError) as e:
        log('配置文件解析失败：%s' % e)
        return 2

    chs = channels.build_channels(cfg['channels'], log=log)

    # 模式一：测试通知（渠道验证），发完即退
    if args.test_alert:
        send_all(chs, 'QQ 监视测试通知',
                 '如果你收到这条，说明 %s 的消息渠道配置是通的。' % cfg['base'])
        return 0

    sm = statemachine.MonitorStateMachine(debounce=cfg['debounce'])
    # 模式二：单轮探测，退出码即健康状态（0=在线 1=异常）
    if args.once:
        state = run_once(cfg, chs, sm)
        return 0 if state == probe.ONLINE else 1

    # 模式三：WebUI（后台监视 + 浏览器控制台）
    if args.webui:
        return run_webui(cfg, args.config)

    # 模式四：纯 CLI 常驻轮询（最简形态，无共享状态需求）
    log('启动：监视 %s，间隔 %ds，去抖 %d 次，渠道 %d 个'
        % (cfg['base'], cfg['interval'], cfg['debounce'], len(chs)))
    while True:
        run_once(cfg, chs, sm)
        time.sleep(cfg['interval'])


def run_webui(cfg, config_path):
    """WebUI 模式：一条线程轮询监视，主线程挂住 WebUI 服务。

    为什么要两条线：WebUI 要展示连续历史和实时状态，轮询不能停；
    而 HTTP 服务要常驻主线程。二者共享同一个 MonitorRuntime 实例
    （所有可变状态和锁都在那里面，见 runtime.py 模块说明）。
    """
    import threading

    import runtime as runtime_mod
    import webui

    rt = runtime_mod.MonitorRuntime(cfg, log_fn=log, config_path=config_path)
    poll = threading.Thread(target=rt.poll_loop, daemon=True)
    poll.start()
    log('启动（WebUI 模式）：监视 %s，间隔 %ds，去抖 %d 次，渠道 %d 个'
        % (cfg['base'], cfg['interval'], cfg['debounce'], len(rt.channels)))
    srv = webui.start_webui(rt)
    try:
        while True:
            time.sleep(3600)   # 主线程只负责不退出（sleep 被中断就结束）
    except KeyboardInterrupt:
        log('收到 Ctrl+C，退出')
    finally:
        srv.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
