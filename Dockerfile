FROM python:3.12-alpine

# BotBrother 镜像：非 root 运行
RUN addgroup -S botbrother && adduser -S botbrother -G botbrother
WORKDIR /app

COPY monitor.py probe.py statemachine.py channels.py runtime.py webui.py webui.html auth.py /app/

# 认证数据目录（密码哈希）需可写；compose 会把宿主机 ./data 挂到这里以在重建/重启后保留
RUN mkdir -p /app/data && chown -R botbrother:botbrother /app/data

# HEALTHCHECK 用 --once：0=在线 1=异常（配置经挂载/env 提供）
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 monitor.py --config /app/data/config.json --once || exit 1

USER botbrother
# config.json 与认证数据都放在可写 data 目录（compose 把宿主机 ./data 挂到 /app/data），
# 以便 WebUI 保存配置时能原子写；无 config 时启动会提示后退出
CMD ["python3", "monitor.py", "--config", "/app/data/config.json"]
