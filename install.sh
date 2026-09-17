#!/bin/bash
# BotBrother 一键安装脚本（Linux/macOS，root 部署到系统；Docker 见 README）
# 用法：sudo bash install.sh [安装目录]   默认 /opt/botbrother
set -euo pipefail

DEST="${1:-/opt/botbrother}"
SRC="$(cd "$(dirname "$0")" && pwd)"

# 把路径安全嵌入 sed 替换串（转义反斜杠、& 与分隔符 #）
sed_escape() { printf '%s' "$1" | sed -e 's/[\\&#]/\\&/g'; }

[ -f "$SRC/monitor.py" ] || { echo "请在项目根目录运行本脚本"; exit 1; }

echo "安装到 $DEST ..."
mkdir -p "$DEST"
cp monitor.py probe.py statemachine.py channels.py runtime.py webui.py webui.html auth.py "$DEST/"
mkdir -p "$DEST/data"
if [ ! -f "$DEST/config.json" ]; then
    cp config.example.json "$DEST/config.json"
    echo "已生成 $DEST/config.json —— 记得改 endpoints.base/channels 再启动"
fi
echo "WebUI 首次访问将提示设置登录密码（保存在 $DEST/data/webui_auth.json）；"
echo "忘记密码可在本机运行： python3 $DEST/monitor.py --config $DEST/config.json --reset-webui-password"

# systemd（Linux）
if command -v systemctl >/dev/null 2>&1; then
    sed "s#/opt/botbrother#$(sed_escape "$DEST")#g" systemd/botbrother.service \
        > /etc/systemd/system/botbrother.service
    systemctl daemon-reload
    echo "已装 systemd 单元。启动： systemctl enable --now botbrother"
    echo "看日志： journalctl -u botbrother -f"
# launchd（macOS）：发布不含 macos/ 目录，这里用 stdlib plistlib 就地生成等价 plist，
# 由库负责 XML 编码与转义（安装路径含 & < > 等字符也安全），保持既有安装能力。
elif [ -d /Library/LaunchDaemons ]; then
    PLIST="/Library/LaunchDaemons/com.botbrother.monitor.plist"
    python3 - "$DEST" "$PLIST" <<'PY'
import plistlib, sys
dest, out = sys.argv[1], sys.argv[2]
plistlib.dump({
    "Label": "com.botbrother.monitor",
    "ProgramArguments": ["/usr/bin/python3", dest + "/monitor.py",
                         "--config", dest + "/config.json"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": "/tmp/botbrother.log",
    "StandardErrorPath": "/tmp/botbrother.err.log",
}, open(out, "wb"))
PY
    if command -v plutil >/dev/null 2>&1; then plutil -lint "$PLIST" >/dev/null; fi
    echo "已装 launchd 配置。启动： sudo launchctl load -w $PLIST"
    echo "看日志： tail -f /tmp/botbrother.log"
else
    echo "未识别 systemd/launchd，手动启动： python3 $DEST/monitor.py --config $DEST/config.json"
fi
echo "完成。"
