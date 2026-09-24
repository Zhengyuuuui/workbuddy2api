# 海外 WorkBuddy（8788）x-device-token 获取指引

> 适用：`www.workbuddy.ai` 海外体系（turing-shield **channelId=400101**），
> 官方客户端 WorkBuddy.app（bundle id `com.tencent.workbuddy.mac`）。
> 国内 CodeBuddy 流程见 [mitm-capture-playbook.md](./mitm-capture-playbook.md) §7，
> 本文只讲**海外差异**与实测引导步骤。

## 0. 前置与安全

- 已安装 mitmproxy（`mitmdump`）、已用官方 WorkBuddy.app 登录目标账号
- **一设备一账号一 token**：token 与「设备 + 账号」绑定，多账号共用会设备关联连坐封号
- 海外 token（400101）**不要与国内 token（109137）混用**，各自存独立文件：
  - 海外：`~/.workbuddy-device-token.json`
  - 国内：`~/.codebuddy-device-token.json`
- 全程命令需 `sudo`（监听 443）；token 只写 0600 文件，勿打印全量、勿分享、勿提交（.gitignore 已排除 `*device-token*.json`）

## 1. 第一步：实测确认目标域名与 IP（不要照抄结论）

海外官方客户端的业务域名/直连 IP 需**现场实测**。已知线索（仅供参考，必须验证）：

- 我们的 bridge endpoint 是 `www.workbuddy.ai`
- 海外 IDE 也可能直连 `copilot.tencent.com`（WorkBuddy.app 曾观察到连接 Clash fake-ip `198.18.x.x` 与 `101.71.73.138` 等真实 IP）
- **本机实测线索（2026-09-23，会随 CDN 变化）**：WorkBuddy.app（`com.tencent.workbuddy.mac`）进程存在多条到
  `copilot.tencent.com` 当前解析 IP（`39.174.179.6`）的 ESTABLISHED 连接 → 高度怀疑海外 IDE 与国内**同用 chat 端点**；
  另观察到 `36.151.170.x` 连接（疑似 sidecar/遥测，非模型对话）。以第 1 步 lsof 当场实测为准。

先启动官方 WorkBuddy.app 并登录，然后在它发起模型请求时探测其真实连接（绕过 127.0.0.1 系统代理）：

```bash
# 1) 找出 WorkBuddy 相关进程的对外 ESTABLISHED 连接（排除本机代理）
for pid in $(pgrep -f "WorkBuddy"); do
  lsof -nP -iTCP -a -p "$pid" 2>/dev/null | grep ESTABLISHED | grep -v 127.0.0.1
done

# 2) 目标端口一般是 443；对上一步出现的 IP 反查域名辅助判断
for ip in <上一步出现的IP>; do
  echo "== $ip"; host "$ip" 2>/dev/null
done

# 3) 若是域名直连（非 IP），解析其当前 IP
dscacheutil -q host -a name <目标域名> | awk '/ip_address/{print $2; exit}'
```

判定要点：

- 抓到的目标应是**模型对话接口**（路径 `/v2/chat/completions`），不是计费/配置接口
- 若见到 `198.18.x.x`，那是 Clash/Surge 的 fake-ip（说明流量走了代理），需要临时关掉该代理或换探测方式，拿到**真实上游 IP** 再继续
- 记录：`TARGET_HOST`（域名）与 `TARGET_IP`（真实 IP），后续命令替换

## 2. 第二步：抓包三件套（与国内相同，macOS）

> 关键：反向代理的上游要写**真实 IP**（写域名会被 hosts 劫持回环）。

```bash
# 用实测值替换
TARGET_HOST=www.workbuddy.ai      # 或 copilot.tencent.com（以实测为准）
TARGET_IP=<第 1 步得到的真实 IP>

# 1) 静态路由：把目标 IP 引到本机
sudo route add -host "$TARGET_IP" 127.0.0.1

# 2) pf 重定向：lo0 上目标 IP:443 → 127.0.0.1:443
echo "rdr pass on lo0 inet proto tcp from any to $TARGET_IP port 443 -> 127.0.0.1 port 443" | sudo pfctl -f -

# 3) hosts 兜底（域名解析到本机）
echo "127.0.0.1 $TARGET_HOST" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache

# 4) mitmdump 反向代理（保持前台运行，勿关此终端）
sudo mitmdump --mode reverse:https://$TARGET_IP/ --listen-port 443 \
  --set save_stream_file=/tmp/wb-intl.mitm \
  --set keep_host_header=true \
  --set ssl_insecure=true
```

验证劫持链（可选）：

```bash
curl -sS --max-time 8 -o /dev/null -w "HTTP %{http_code}\n" "https://$TARGET_HOST/任意路径"
# 返回真实上游状态码（404/405/200 都算通）；超时=回环或 mitm 未监听 443
```

## 3. 第三步：触发请求并提取 token

1. 在官方 WorkBuddy.app 里**发一条 AI 对话**（触发 `/v2/chat/completions`）
2. 另开终端运行提取工具（自动过滤路径 + `v3:` 前缀 + 去重，写 0600 JSON）：

```bash
uv run server/extract_flows_token.py /tmp/wb-intl.mitm \
  --output ~/.workbuddy-device-token.json
```

工具只打印 token 长度与前 8 字符，不打印全量；`source` 形如 `mitm:/tmp/wb-intl.mitm`。
未抓到时会提示排查方向（见 §6）。

## 4. 第四步：清理（必须执行，否则本机网络被劫持）

```bash
sudo pkill -f mitmdump
sudo sed -i '' "/$TARGET_HOST/d" /etc/hosts
sudo route delete "$TARGET_IP"
sudo pfctl -F all
sudo dscacheutil -flushcache
```

## 5. 第五步：注入 8788 并验证

```bash
uv run server/codebuddy_proxy.py --port 8788 \
  --endpoint https://www.workbuddy.ai --platform workbuddy-ai \
  --device-token ~/.workbuddy-device-token.json
```

验证清单：

- [ ] `curl http://127.0.0.1:8788/health` → `status: ok` 且 `authenticated: true`
- [ ] 通过 bridge 发一条消息（客户端指向 `http://127.0.0.1:8788/v1/...`）能正常返回
- [ ] `curl http://127.0.0.1:8788/v1/credits` → `available: true`（海外计费接口已验证可用）
- [ ] 官方 WorkBuddy.app 的**控制台/消费记录**中，本次调用来源列应显示 `WorkBuddy`/官方标记（而非第三方/未知客户端）
- [ ] bridge 日志中 `x-device-token loaded from cli:--device-token`（启动日志）

## 6. 注意事项与排查

- **token 时效**：失效后按本流程重抓；若 8788 出现 429/6004 或来源标记异常，优先怀疑 token 过期
- **不要混用**：400101（海外）与 109137（国内）token 体系不同，串用大概率直接失败或触发风控
- **抓不到 token 的常见原因**：
  - 未真正触发对话（只在 IDE 里浏览未发消息）
  - 上游写域名导致回环 → 改用真实 IP（§2）
  - fake-ip/代理未关闭，真实上游 IP 判断错误（§1）
  - 目标不是 `/v2/chat/completions`（计费/配置路径的 token 场景不同，工具不认）
- **静态提取**：`uv run server/extract_device_token.py` 会扫描海外 plist（`com.tencent.workbuddy.mac.plist` 等）与数据目录，但 token 运行时生成不落盘，通常找不到——这是预期，抓包是唯一可靠途径
- 清理不完整会导致本机不通外网：执行完 §4 后 `sudo pfctl -s rules` 应无残留 rdr 规则
