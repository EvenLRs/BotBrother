#!/bin/bash
# BotBrother 一键安装脚本（Linux/macOS，root 部署到系统；Docker 见 README）
# 用法：sudo bash install.sh [安装目录]   默认 /opt/botbrother
set -euo pipefail

DEST="${1:-/opt/botbrother}"
SRC="$(cd "$(dirname "$0")" && pwd)"

[ -f "$SRC/monitor.py" ] || { echo "请在项目根目录运行本脚本"; exit 1; }

echo "安装到 $DEST ..."
mkdir -p "$DEST"
cp monitor.py probe.py statemachine.py channels.py "$DEST/"
if [ ! -f "$DEST/config.json" ]; then
    cp config.example.json "$DEST/config.json"
    echo "已生成 $DEST/config.json —— 记得改 probe.base/channels 再启动"
fi

# systemd（Linux）
if command -v systemctl >/dev/null 2>&1; then
    sed "s#/opt/botbrother#$DEST#g" systemd/botbrother.service \
        > /etc/systemd/system/botbrother.service
    systemctl daemon-reload
    echo "已装 systemd 单元。启动： systemctl enable --now botbrother"
    echo "看日志： journalctl -u botbrother -f"
# launchd（macOS）
elif [ -d /Library/LaunchDaemons ]; then
    sed "s#/usr/local/opt/botbrother#$DEST#g" \
        macos/com.botbrother.monitor.plist \
        > /Library/LaunchDaemons/com.botbrother.monitor.plist
    echo "已装 launchd 配置。启动： sudo launchctl load -w /Library/LaunchDaemons/com.botbrother.monitor.plist"
    echo "看日志： tail -f /tmp/botbrother.log"
else
    echo "未识别 systemd/launchd，手动启动： python3 $DEST/monitor.py --config $DEST/config.json"
fi
echo "完成。"
