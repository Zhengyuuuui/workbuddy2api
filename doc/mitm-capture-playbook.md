# mitmproxy 抓包实战手册（macOS / Electron 应用）

> 本文档记录 2026-09-21 对 CodeBuddy CN（腾讯 AI IDE）真实流量的完整抓包过程，
> 包含踩过的所有坑与最终可行方案。可直接复用于其他 Electron/VSCode-fork/Node 客户端的协议逆向。

---

## 0. 适用场景

| 场景 | 是否适用本方案 |
|---|---|
| Electron 应用（跟随系统代理） | ✅ 直接用 `--proxy-server` 启动参数 |
| Electron 主进程硬编码直连（不吃代理参数） | ✅ hosts + 反向代理（本文核心方案） |
| VSCode fork 的 extension host（Node 网络栈） | ✅ 同上，Node 不读系统代理 |
| 有证书固定（SSL Pinning）的应用 | ⚠️ 需先脱壳分析 pinning 逻辑，或用 Frida 绕过 |
| Native 应用（非 Electron） | ⚠️ 参考 skills/software-research/playbooks/native-binary-analysis.md |

## 1. 环境准备

```bash
# 安装 mitmproxy
brew install mitmproxy

# 证书位置（首次运行任意 mitm 命令后自动生成）
~/.mitmproxy/mitmproxy-ca-cert.pem
```

确认应用数据目录（后续提取 device-token 用）：

| 应用 | 用户数据目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/app/` |
| CodeBuddy CN (IDE) | `~/Library/Application Support/CodeBuddy CN/` |
| CodeBuddy CN (CLI 侧) | `~/.codebuddy/` |

## 2. 方案选型决策树

```
应用流量是否已被抓到？
├── 是 → 直接分析 flows
└── 否 → 应用是否吃 --proxy-server 启动参数？
    ├── 是 → 方案 A（最简单）
    └── 否 → Node/Electron 主进程直连？
        ├── 是 → 方案 C：hosts + 反向代理（本文推荐，最稳）
        └── 优先级低于系统路由 → 方案 B：透明代理（坑多，不推荐）
```

### 方案 A：启动参数代理（能吃参数就用这个）

```bash
http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080 \
HTTP_PROXY=http://127.0.0.1:8080 HTTPS_PROXY=http://127.0.0.1:8080 \
NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem \
open -a "目标App" --args --proxy-server="http://127.0.0.1:8080"
```

注意：`NODE_EXTRA_CA_CERTS` 只对 Node 子进程生效；Electron 主进程信任
钥匙串中的 mitm CA（需手动导入并信任）。

### 方案 B：透明代理（macOS 上坑极多，仅记录教训）

失败路径复盘：
1. `pf rdr on lo0` → 本机出站包不经过 lo0 的 rdr（rdr 只处理流入包）❌
2. `pf rdr on en0` → 同理，本机出站不走 en0 输入路径 ❌
3. `route add -host X 127.0.0.1` + `rdr on lo0` → 包能进 mitm，但
   mitmproxy 透明模式需要 root 读 pfctl 还原原始目标地址，否则报
   `Could not resolve original destination` ❌
4. sudo 跑透明模式 + 静态路由同时存在 → **路由回环**：mitm 连上游的包
   也被 lo0 路由打回自己，TLS 握手永远挂起 ❌

教训：macOS 上透明代理 = sudo mitmdump + 路由 + rdr 三件套缺一不可，
且必须先删路由再连上游。不如直接用方案 C。

### 方案 C：hosts + 反向代理（本次实战最终方案）✅

原理：

```
应用请求域名X → /etc/hosts 把域名X指向 127.0.0.1
            → mitmdump --mode reverse:https://真实上游/ 监听 443
            → 记录后转发到真实上游（上游IP≠被劫持域名解析结果，不回环）
```

#### 步骤 1：找到应用的真实出口

```bash
# 全进程扫描外连（应用发请求时反复执行）
for pid in $(pgrep -f "应用进程名关键词"); do
  lsof -nP -iTCP -a -p $pid 2>/dev/null | grep ESTABLISHED | grep -v 127.0.0.1
done
```

对 443 目标 IP 反查域名：`host <IP>` 或 `dscacheutil -q host -a name <域名>`

> 实战记录：CodeBuddy IDE 的 chat 请求（PID 为 Helper (Plugin) 进程）
> 直连 `211.91.8.69`（copilot.tencent.com 的 IP），完全绕过
> `--proxy-server` 参数和系统代理。这是 Node `undici`/`fetch` 不读系统代理导致。

#### 步骤 2：hosts 劫持（先备份！）

```bash
sudo cp /etc/hosts /tmp/hosts.backup.$(date +%s)
echo "127.0.0.1 目标域名" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache
```

#### 步骤 3：反向代理（上游写 IP 或"不被劫持的域名"避免回环）

```bash
# 上游用 IP（推荐，绕过 hosts）：
sudo mitmdump --mode reverse:https://<真实IP>/ \
  --listen-port 443 \
  --set keep_host_header=true \
  --set ssl_insecure=true \
  --set save_stream_file=/tmp/capture.mitm

# 上游用域名时，确认该域名解析结果 ≠ hosts 里被劫持的 IP，否则回环！
```

关键参数：
- `keep_host_header=true`：转发时保留原始 Host 头（很多后端校验）
- `ssl_insecure=true`：上游证书是 CDN 的，SNI/域名校验可能不匹配
- 必须 `sudo`：监听 443 特权端口

#### 步骤 4：验证劫持链

```bash
curl -sS --max-time 8 -o /dev/null -w "HTTP %{http_code}\n" \
  https://被劫持的域名/任意路径
# 预期：返回真实上游的状态码（405/404/200 都算通）
# 若超时：检查路由回环 / 检查 mitmdump 是否真的监听 443
```

#### 步骤 5：触发目标请求并解析

让目标应用执行要抓的操作（登录/发消息/调模型），然后：

```bash
# 列出所有流量
mitmdump -nr /tmp/capture.mitm -s /dev/stdin <<'EOF'
def response(flow):
    print(flow.request.method, flow.request.pretty_host, flow.request.path[:60],
          '→', flow.response.status_code if flow.response else '?')
EOF

# 提取目标请求的完整 headers + body
mitmdump -nr /tmp/capture.mitm -s /dev/stdin <<'EOF' > result.txt
import gzip, json
def response(flow):
    if flow.request.method == 'POST' and '目标路径特征' in flow.request.path:
        print("=== HEADERS ===")
        for k, v in flow.request.headers.items():
            if k.lower() in ('authorization', 'cookie'):
                print(f"{k}: {v[:20]}... [REDACTED]")
            else:
                print(f"{k}: {v}")
        raw = flow.request.raw_content or b''
        if flow.request.headers.get('content-encoding') == 'gzip':
            raw = gzip.decompress(raw)
        print("=== BODY ===")
        print(raw.decode('utf-8', errors='replace')[:5000])
EOF
```

#### 步骤 6：清理（必须完整，否则污染本机网络）

```bash
sudo pkill -f mitmdump
sudo sed -i '' '/被劫持域名/d' /etc/hosts
sudo route delete <劫持过的IP> 2>/dev/null   # 每条路由都要删！
sudo pfctl -F all
sudo dscacheutil -flushcache
```

验证：`curl https://被劫持域名/` 应返回直连结果且 `%{remote_ip}` 不是 127.0.0.1。

## 3. 实战成果：CodeBuddy CN 官方 IDE 请求指纹

### 3.1 请求链路真相

| 层 | 域名 | 用途 |
|---|---|---|
| IDE UI/账户 | `copilot.tencent.com` | 登录、会话列表、签到、报表 |
| IDE AI chat | 同上（`/v2/chat/completions`，直连 IP 绕代理） | **模型对话主通道** |
| 模型网关（代码中存在） | `api.lkeap.cloud.tencent.com/plan/v3/chat/completions`、`tokenhub.tencentmaas.com` | 计费网关（`crb-` 前缀 request ID） |
| 消费记录标记 | — | IDE 走插件协议通道记账为 "-"，App 走自有通道记为 "WorkBuddy" |

### 3.2 官方 IDE chat 请求完整 headers（抓包原文）

```http
user-agent: CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0
authorization: Bearer <JWT 1334字符>
content-type: application/json
content-encoding: gzip                      ← body 是 gzip 压缩的！

x-device-token: v3:AAAA...（465字符设备指纹，最强风控标识；完整值已脱敏，仅存于本地）
x-agent-intent: craft
x-ide-name: CodeBuddyIDE
x-ide-type: CodeBuddyIDE
x-ide-version: 4.12.0
x-model-id: deepseek-v4.1-flash
x-product: SaaS
x-product-code: codebuddy
x-product-version: 4.12.0
x-env-id: production
x-domain: www.codebuddy.cn
x-conversation-id: <32hex，每会话一个>
x-conversation-message-id: <32hex，每消息一个>
x-conversation-request-id: <32hex，每请求一个，= x-request-id>
x-request-trace-id: <标准UUID>
x-b3-traceid: <32hex> / x-b3-spanid: <16hex> / x-b3-sampled: 1 / b3: ...
monitor_httpsendtime: <13位毫秒时间戳>
x-requested-with: XMLHttpRequest
accept: */*   accept-language: *   sec-fetch-mode: cors
```

### 3.3 body 关键参数（与 OpenAI 格式的差异）

```json
{
  "model": "deepseek-v4.1-flash",
  "max_tokens": 393216,
  "temperature": 1,
  "reasoning_summary": "auto",
  "reasoning_effort": "high",
  "previous_response_id": "<32hex，会话连续性标识>",
  "messages": [...]
}
```

- body 整体 gzip 压缩（`Content-Encoding: gzip`）
- `previous_response_id`：由上一轮响应返回，链式传递
- system prompt 中包含官方 content_policy 与安全声明

### 3.4 x-device-token 说明

- 465 字符，`v3:` 前缀，设备级加密指纹
- 存放位置：IDE 用户数据目录（leveldb / local_storage）
- **一设备一值**：同 token 多账号 = 设备关联，连锁封号
- 提取方式：参考 `skills/software-research/playbooks/configuration-analysis.md`

## 4. 风控结论与反检测清单

### 4.1 为什么 bridge 的请求被封（11140 request illegal）

风控视角的请求画像对比：

| 维度 | 官方 IDE | 旧 bridge | 风控判定 |
|---|---|---|---|
| 设备指纹 | x-device-token | 无 | 未知设备 |
| 会话链路 | conversation-id 链式 | 每次孤立 | 机器行为 |
| UA | CodeBuddyIDE/4.12.0 | Python httpx | 机器行为 |
| trace 链 | 完整 b3 | 无 | 机器行为 |
| 频率 | 人类节奏 | Agent 连发+热重试 | 滥用 |
| 单请求体量 | 常规 IDE 上下文 | 2552/987 条消息 | 滥用 |

信号叠加 → 先 429(6004 频率限制) 警告 → 11140 账号级拦截。

### 4.2 反检测改造清单

**P0（必做，无风险）：**
- [ ] UA 与 x-ide-* 三件套伪装为官方 IDE 版本
- [ ] 补齐 x-product / x-env-id / x-domain / x-requested-with
- [ ] max_tokens 拉到上游同款量级（393216）
- [ ] body 是否 gzip 由内容大小决定（对齐官方行为）

**P1（核心对抗）：**
- [ ] x-device-token：从真实 IDE 数据目录提取，一设备一账号
- [ ] 会话链路：proxy 为每个客户端会话生成并维护
      x-conversation-id（会话级）/ x-conversation-message-id（消息级）/
      x-conversation-request-id（请求级）
- [ ] previous_response_id：从上游响应中提取并随会话复用
- [ ] x-b3-traceid/spanid/b3：每请求生成规范值

**P2（行为层）：**
- [ ] 429 熔断：收到 6004 立即冷却 ≥5min，指数退避，禁止热重试
- [ ] 请求限速 + 随机抖动（3~10s）
- [ ] --optimize-context 常开，单请求消息数封顶（≤100）
- [ ] 额度监控：/billing/meter/get-user-resource 轮询 + 异常速率熔断
      （可暴露为 GET /v1/credits）
- [ ] 保持账号在官方 IDE 中有真实使用记录（账单来源混合）

## 5. 复用本方法时的检查清单

- [ ] 找到应用全部出口域名/IP（lsof 扫描，别只看官方文档）
- [ ] 确认哪个域名承载核心业务请求（不是所有流量都值得抓）
- [ ] hosts 备份 → 劫持 → 反代（上游 IP 优先）→ 验证 → 触发 → 解析 → **完整清理**
- [ ] 抓到请求后，对比自家代理请求的差异（headers/body/压缩/链路）逐项补齐
- [ ] 敏感值（token/设备指纹）永远不进 git，不写入日志
- [ ] 对抗升级预期：风控会持续演进，抓包结论有时效性（本文基于 2026-09 版本 4.12.0）

## 6. 原始数据留存

| 文件 | 内容 |
|---|---|
| `.ydevsphere/official-ide-headers.txt` | 官方 IDE chat 请求 headers 快照（已脱敏 authorization） |
| `/tmp/wb-capture/flows-copilot.mitm` | 完整原始流量（重启后丢失，重要请自行备份） |
| `.ydevsphere/` | gitignore 内，不会提交 |
