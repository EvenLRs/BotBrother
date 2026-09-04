# BotBrother

BotBrother is watching

QQ Bot在线状态监视：轮询 OneBot v11 HTTP API，账号下线或服务意外退出时第一时间通知到你。

## 为什么需要它

QQ Bot大多跑在服务器上，账号掉线或程序崩溃没人知道，一挂一整晚。

BotBrother 支持同时监视多个 OneBot 服务端（NapCat / LLOneBot / SnowLuma 混布均可），每端点独立探测、独立去抖，报警文案带端点名区分是哪台出的事。能区分两种故障并给出不同报警：

| 故障 | 判定依据 | 报警文案 |
|---|---|---|
| **QQ 账号下线** | API 应答 `online=false` | QQ 账号已下线（程序还活着，登录态掉了） |
| **服务意外退出** | API 不可达（拒连/超时/403） | OneBot 服务疑似意外退出（此类程序极少手动退出） |

同一异常连续 N 轮（默认 3）才报；故障期内只报一次；恢复后补发「已恢复上线」。

## 部署
### 快速开始

```bash
git clone <repo> BotBrother && cd BotBrother
cp config.example.json config.json

# 1. 改两处：endpoints[].base 指向你的 OneBot 服务（可多个）；channels 填一个推送渠道
# 2. 验证渠道通不通（会发送真实通知）
python3 monitor.py --config config.json --test-alert

# 3. 启动（推荐 WebUI 模式）
python3 monitor.py --config config.json --webui
```

### 直接安装-Linux (需systemd)

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

### Docker镜像部署

```bash
cp config.example.json config.json   # 改好再起
docker compose up -d --build
docker logs -f botbrother
```

## 运行模式

| 模式 | 命令 | 用途 |
|---|---|---|
| 常驻 | （默认） | 纯命令行轮询，日志到 stdout |
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
| `interval` | 30 | 轮询间隔（秒），全部端点共用，支持设置范围为5–86400 |
| `endpoints` | 见 example | 被监视端点列表，每项一个 OneBot 服务，字段见下 |
| `channels` | `[{"type":"log"}]` | 推送渠道列表，见下节 |
| `webui.port` | 8080 | WebUI 端口 |
| `webui.bind` | `127.0.0.1` | 默认仅允许本机访问，设置为`0.0.0.0`后可允许局域网访问或外网访问（此场景必须设置token） |
| `webui.token` | 空 | 管理令牌；设置后 API 需 `Authorization: Bearer <token>` |

**端点字段（`endpoints[]` 每项，可配多个）：**

| 字段 | 默认 | 说明 |
|---|---|---|
| `label` | base 地址 | 展示名：报警文案（「【服务器NapCat】QQ 账号已下线」）与页面卡片标题 |
| `base` | 必填 | OneBot HTTP 端点 |
| `token` | 空 | 该端点的 access_token（各端点可不同） |
| `timeout` | 5 | 单次探测超时（秒），1–60 |
| `debounce` | 3 | 该端点连续 N 轮异常才报警（1–10），每端点独立计数互不影响 |

> 兼容旧写法：单端点时代的 `probe{base,token,timeout}` + 顶层 `debounce` 自动转换为一个端点，旧配置不改也能跑；落盘统一写新结构。同地址同令牌重复配置会被拒绝。

### 环境变量（作用于第一个端点，容器单端点场景友好）：
| 变量名 | 默认 | 说明 |
|---|---|---|
| `QQMON_INTERVAL` | 30 | 轮询间隔（秒），全局生效 |
| `QQMON_PROBE_BASE` | `http://127.0.0.1:3000` | 第一个端点的 OneBot HTTP 端点 |
| `QQMON_PROBE_TOKEN` | 空 | 第一个端点的 access_token |
| `QQMON_PROBE_TIMEOUT` | 5 | 第一个端点的探测超时（秒） |
| `QQMON_DEBOUNCE` | 3 | 第一个端点的去抖轮数 |

<details>
<summary>如何找到你的 probe.base（LLOneBot / NapCat / SnowLuma）</summary>

#### 监视程序与 OneBot **在同一台机器**时：
- **NapCat**：WebUI → 网络配置 → 启用 HTTP 服务，看监听端口；本机默认 `http://127.0.0.1:3000`
- **LLOneBot/LuckyLiliaBot**：QQ 设置 → OneBot11 → 启用 HTTP 服务器，看监听端口；本机默认 `http://127.0.0.1:3000`
- **SnowLuma**：默认同为 3000
#### 监视程序与 OneBot **不在同一台机器**时：
- base 填 OneBot 机器的 IP，且 OneBot 的 HTTP 监听地址须为 `0.0.0.0` 或对应IP地址（默认 `127.0.0.1` 只接受本机连接）
- 监听接口查询方式同上方

</details>

## 推送渠道

| type | 必填参数 | 平台 |
|---|---|---|
| `bark` | `key`（可 `server`） | iOS Bark |
| `wecom` | `key` | 企业微信机器人 |
| `feishu` | `hook_id` | 飞书机器人 |
| `ntfy` | `topic`（可 `server`） | ntfy（iOS/Android/桌面，免注册） |
| `telegram` | `bot_token` + `chat_id` | Telegram |
| `serverchan` | `send_key` | Server酱·Turbo（微信推送） |
| `log` | — | 仅写日志不发送（兜底/自测） |

```json
"channels": [
  { "type": "bark", "key": "你的BarkKey" },
  { "type": "ntfy", "topic": "私有不易猜的topic名" },
  { "type": "log" }
]
```

- 可同时配多个通知渠道，逐渠道独立发送；单个渠道失败只记日志，不拖累其他渠道、不中断监视
- 暂不支持钉钉**加签**机器人
- Bark / ntfy 支持自建服务器：传 `server` 参数覆盖默认端点

## 安全须知

- WebUI 暴露到局域网（`bind: 0.0.0.0`）时**必须设 `webui.token`**：未设置情况下虽仍可打开页面，但改配置/发测试/关停等 API 全部无法使用
- 未设 token 时远程关停接口自动禁用（403）
- WebUI 回显密钥一律打码 `****末4位`，掩码原样提交 = 不修改（不会把 `****` 写进配置）
