FROM python:3.12-alpine

# BotBrother 镜像：非 root 运行
RUN addgroup -S botbrother && adduser -S botbrother -G botbrother
WORKDIR /app

COPY monitor.py probe.py statemachine.py channels.py runtime.py webui.py webui.html /app/

# HEALTHCHECK 用 --once：0=在线 1=异常（配置经挂载/env 提供）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 monitor.py --config /app/config.json --once || exit 1

USER botbrother
# config.json 通过卷挂载或 QQMON_ 环境变量提供；无则启动时提示后退出
CMD ["python3", "monitor.py", "--config", "/app/config.json"]
