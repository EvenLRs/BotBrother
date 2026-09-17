# BotBrother

v1.0.0

BotBrother is watching

QQ Bot在线状态监视，账号下线或服务意外退出时第一时间通知到你。

## 为什么需要它

QQ Bot大多跑在服务器上，账号掉线或程序崩溃时用户无法及时得知，高度依赖用户反馈或定期巡检。

BotBrother 支持同时监视多个 OneBot 服务端（如 NapCat / LLOneBot / SnowLuma）。能区分两种故障并给出不同告警提示：

| 故障 | 判定依据 | 报警文案（实际发送，`{self_id}` 为机器人 QQ 号，探测不到时留空） |
|---|---|---|
| **QQ 账号下线** | API 应答 `online=false` | `[BotBrother] 警告：{self_id}账号离线。如本次离线为您主动触发，请忽略本信息。` |
| **服务意外退出** | API 不可达（拒连/超时/403） | `[BotBrother] 警告：{self_id}所在客户端连接失败，请检查客户端是否离线。` |

恢复通知：`[BotBrother] {self_id}已恢复在线。`。

同一异常连续 N 轮（默认 3）才报；故障期内只报一次；恢复后补发一次。

## 部署
### 快速开始

```bash
git clone <repo> BotBrother && cd BotBrother
cp config.example.json config.json

# 1. 改两处：endpoints[].base 指向你的 OneBot 服务（可多个）；channels 填你已部署的推送渠道
# 2. 验证渠道通不通（会发送真实通知）
python3 monitor.py --config config.json --test-alert

# 3. 启动（推荐 WebUI 模式）
python3 monitor.py --config config.json --webui
```

> 运行环境：Python 3.9+
> macOS 自带的 `/usr/bin/python3`（命令行工具版）不含 scrypt，会导致 WebUI 设置密码失败；请改用
> Homebrew / python.org 版 Python，或直接用 Docker 镜像（`python:3.12-alpine`）。

### 直接安装-Linux

```bash
sudo bash install.sh                 # 默认装到 /opt/botbrother
sudo systemctl enable --now botbrother
journalctl -u botbrother -f
```

### 直接安装-macOS

```bash
sudo bash install.sh /usr/local/opt/botbrother
sudo launchctl load -w /Library/LaunchDaemons/com.botbrother.monitor.plist
tail -f /tmp/botbrother.log
```

> `install.sh` 会用 Python 标准库 `plistlib` 就地生成 `/Library/LaunchDaemons/com.botbrother.monitor.plist`
> （发布包不含 `macos/` 目录），并按传入的安装路径正确转义；Linux 侧用同仓库的 `systemd/botbrother.service` 生成单元。

### Docker镜像部署

```bash
cp config.example.json config.json   # 维护配置文件后，再启动程序
docker compose up -d --build
docker logs -f botbrother
```

> 注意：镜像默认 CMD 是**纯 CLI 常驻轮询**（`python3 monitor.py --config /app/data/config.json`，**不含 `--webui`**）。
> 需要在容器里用 WebUI 时，覆盖 command 追加 `--webui` 并映射端口，例如
> `docker run -d -p 8080:8080 -v "$PWD/data":/app/data botbrother:latest python3 monitor.py --config /app/data/config.json --webui`。
> 仓库内 compose 目前未开 WebUI。

## 运行模式

| 模式 | 命令 | 用途 |
|---|---|---|
| 后台常驻 | （默认） | 纯命令行轮询，日志到 stdout |
| WebUI | `--webui` | 后台监视 + 浏览器控制台（改配置/看状态/发测试/导历史） |
| 单轮 | `--once` | 退出码即健康状态，适合 cron / 容器 HEALTHCHECK / 巡检脚本 |
| 测试 | `--test-alert` | 渠道连通性验证，发完即退 |

<details>
<summary>启动命令示例</summary>
  
```bash
python3 monitor.py --config config.json              # 常驻轮询，日志写 stdout
python3 monitor.py --config config.json --webui      # 与WebUI一同启动
python3 monitor.py --config config.json --once       # 单轮探测，退出码 0=在线 1=异常（健康检查用）
python3 monitor.py --config config.json --test-alert # 向所有渠道发测试通知
```

</details>

## 配置

支持配置文件(`config.json`)和环境变量两种配置方式：

### 配置文件：
| 字段 | 默认 | 说明 |
|---|---|---|
| `interval` | 30 | 轮询间隔（秒），全部服务端共用，支持设置范围为5–86400 |
| `endpoints` | 见 example | 被监视服务端列表，每项一个 OneBot 服务，字段见下 |
| `channels` | `[{"type":"log"}]` | 推送渠道列表，见下节 |
| `webui.port` | 8080 | WebUI 端口 |
| `webui.bind` | `127.0.0.1` | 默认仅允许本机访问；设为 `0.0.0.0` 可供局域网访问（此时登录密码是唯一防线） |
| `webui.data_dir` | `data` | 认证数据目录（相对路径以 config.json 所在目录为基准）；保存 WebUI 登录密码哈希，需可写并在重建/重启后保留 |

**服务端字段（`endpoints[]` 每项，可配多个）：**

| 字段 | 默认 | 说明 |
|---|---|---|
| `label` | base 地址 | 展示名：报警文案（「【服务器NapCat】QQ 账号已下线」）与页面卡片标题 |
| `base` | 必填 | OneBot HTTP 服务端 |
| `token` | 空 | 该服务端的 access_token（各服务端可不同） |
| `timeout` | 5 | 单次探测超时（秒），1–60 |
| `debounce` | 3 | 该服务端连续 N 轮异常才报警（1–10），每服务端独立计数互不影响 |

### 环境变量（仅作用于第一个服务端，多端探测请使用配置文件）：
| 变量名 | 默认 | 说明 |
|---|---|---|
| `QQMON_INTERVAL` | 30 | 轮询间隔（秒），全局生效 |
| `QQMON_PROBE_BASE` | `http://127.0.0.1:3000` | 第一个服务端的 OneBot HTTP 服务端 |
| `QQMON_PROBE_TOKEN` | 空 | 第一个服务端的 access_token |
| `QQMON_PROBE_TIMEOUT` | 5 | 第一个服务端的探测超时（秒） |
| `QQMON_DEBOUNCE` | 3 | 第一个服务端的去抖轮数 |

<details>
<summary>如何找到你的 probe.base（LLOneBot / NapCat / SnowLuma）</summary>

#### 监视程序与服务端**在同一台机器**时：
- **NapCat**：WebUI → 网络配置 → 启用 HTTP 服务，看监听端口；本机默认 `http://127.0.0.1:3000`
- **LLOneBot/LuckyLiliaBot**：QQ 设置 → OneBot11 → 启用 HTTP 服务器，看监听端口；本机默认 `http://127.0.0.1:3000`
- **SnowLuma**：默认同为 3000
#### 监视程序与服务端**不在同一台机器**时：
- base 填 OneBot 机器的 IP，且 OneBot 的 HTTP 监听地址须为 `0.0.0.0` 或对应IP地址（默认 `127.0.0.1` 只接受本机连接）
- 监听接口查询方式同上

</details>

## 推送渠道

| type | 必填参数 | 平台 |
|---|---|---|
| `bark` | `key` | Bark |
| `wecom` | `key` | 企业微信机器人 |
| `feishu` | `app_id` + `app_secret` + `chat_id` | 飞书应用机器人（自建应用） |
| `ntfy` | `topic` | ntfy |
| `telegram` | `bot_token` + `chat_id` | Telegram |
| `serverchan` | `send_key` | Server 酱 Turbo（微信推送） |
| `log` | — | 仅写日志不发送 |

```json
"channels": [
  { "type": "bark", "key": "你的BarkKey" },
  { "type": "ntfy", "topic": "私有不易猜的topic名" },
  { "type": "log" }
]
```

- 可同时配多个通知渠道，逐渠道独立发送。渠道推送失败会写进日志
- Bark / ntfy 支持自建服务器：使用 `server` 参数覆盖默认服务端

## WebUI 登录与密码

WebUI 使用**登录密码**保护

- **首次访问**：自动进入“设置登录密码”页面（设置密码 + 确认密码），设置成功后立即登录；密码仅以 `scrypt` 哈希保存，不存明文、不回显。
- **后续登录**：已登陆会话通过 Cookie 维持，Cookie 过期或退出WebUi后需重新登录。
- **登录失败限速**：按来源 IP 计数，连续失败后短暂锁定。

### 升级与忘记密码

- **从旧版本升级**：`webui.token` 会被兼容读取但**被忽略、不再作为登录通道**；升级后首次访问需重新设置登录密码。
- **忘记密码（本机重置）**：先在运行程序的机器上**停止服务**，再执行
  `python3 monitor.py --config config.json --reset-webui-password`
  或直接删除认证文件（默认 `<config.json 同目录>/data/webui_auth.json`；Docker 默认 `/app/data/data/webui_auth.json`），然后重启；下次访问会重新提示设置密码。
- **认证数据损坏**：若认证文件损坏，WebUI 会进入“已锁定”状态，**同时拒绝登录与重新设置**（防止被接管），需按上面的本机方式重置。

## 安全须知

- WebUI 暴露到局域网（`bind: 0.0.0.0`）时，登录密码是唯一访问防线：请设置强密码，勿在不可信网络暴露。
- WebUI 回显渠道密钥一律打码 `****末4位`，掩码原样提交 = 不修改（不会把 `****` 写进配置）；NapCat 探测 token 与各通知渠道密钥行为不变。
