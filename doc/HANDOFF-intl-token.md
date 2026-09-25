# 海外 WorkBuddy AI（国际版）device-token 抓取——交接文档

> 写给下一个接手的模型/助手/未来自己。包含已验证事实、失败教训、以及一条被验证过的最短路径。
> 时间：2026-09-24 深夜。此前国内版（8787）已成功：token 注入后消费记录归因 `CodeBuddyIDE`。
> 海外版（8788）token 未抓到，卡在抓包环境搭建上——所有技术障碍已定位，见下。

## 一、已验证的硬事实（不用再探）

1. **海外 App 网络结构（静态分析 + lsof 双重确认）**：
   - 主域 `www.workbuddy.ai`（43.160.158.125）：App 主进程的账户/配置/feature-flags
   - **AI chat 由 sidecar CLI 子进程发出**（`app.asar.unpacked/cli/bin`，即 codebuddy-headless.js，
     product.json 的 endpoint 就是 www.workbuddy.ai）——只在对话时活跃
   - `sg.tgalileo.com`（43.156.86.196/223）+ `tgalileo.com`（43.129.115.113）：OTel 遥测
     /v1/traces + 长连接。**App 空闲时这些连接是唯一的对外活动，容易误判为 chat 通道**
   - 设备指纹：native `turing-sdk`（overseas variant，channelId 400101），运行时生成不落盘

2. **劫持链路本身已跑通**：hosts(127.0.0.1) + pf rdr(443→8444) + mitm reverse 上游真实 IP，
   curl https://www.workbuddy.ai/v3/config 返回 200——方案没问题。

3. **App 对 mitm 证书无抵抗**：劫持期间 App 的主域请求全部正常进 mitm（v3/config 200 等），
   说明钥匙串信任的 mitm CA 对它有效。chat 抓不到不是证书问题。

## 二、为什么一直 3002（真实原因，有铁证）

App 报错原文：`connect ECONNREFUSED 127.0.0.1:443 (target: https://www.workbuddy.ai)`

即：hosts 劫持生效、App 去连 127.0.0.1:443，但**那一刻 mitmdump 进程没在运行**。
三个失败模式（每一轮 3002 对应其一）：

1. mitmdump 前台进程被误关/退出（最常见，本轮最后就是这样——0 个进程还让用户发消息）
2. pfctl -f 是**整表替换**：单独执行第二条 rdr 会清掉第一条，必须 echo -e 一次写两条
3. 静态路由残留造成 mitm→上游回环（删 route 后 curl 即通——这个坑已踩实）

**教训：每一步之后必须立刻自测（nc 端口、pfctl -s rules、curl 链路），确认绿灯再让用户操作。
本轮失败 80% 责任在"推进前不验证状态"。**

## 三、被验证过的最短路径（照做必成，预计 10 分钟）

前置：WorkBuddy AI 已登录且能正常对话（Clash 开关均可，hosts 劫持与 Clash 不冲突）。

```bash
# ===== 终端 A（sudo mitmdump，保持前台不关！两实例用两个终端）=====
# A1: 主域
sudo mitmdump --mode reverse:https://43.160.158.125/ --listen-host 127.0.0.1 --listen-port 8444 \
  -s /tmp/wb-fix.py --set save_stream_file=/tmp/wb-intl3.mitm \
  --set keep_host_header=true --set ssl_insecure=true
# A2: tgalileo（第二终端）
sudo mitmdump --mode reverse:https://43.156.86.196/ --listen-host 127.0.0.1 --listen-port 8443 \
  -s /tmp/wb-fix.py --set save_stream_file=/tmp/wb-tg.mitm \
  --set keep_host_header=true --set ssl_insecure=true
# （/tmp/wb-fix.py 是 feature-flags 404→200 修复 + 全请求记录 addon，内容见文末）

# ===== 终端 B：hosts + pf（一次性两条 rdr，勿分开执行）=====
sudo cp /etc/hosts /tmp/hosts.bak
echo "127.0.0.1 www.workbuddy.ai" | sudo tee -a /etc/hosts
echo "127.0.0.2 sg.tgalileo.com" | sudo tee -a /etc/hosts
sudo dscacheutil -flushcache
echo -e "rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 443 -> 127.0.0.1 port 8444\nrdr pass on lo0 inet proto tcp from any to 127.0.0.2 port 443 -> 127.0.0.1 port 8443" | sudo pfctl -f -

# ===== 自测（必须全绿再进行下一步）=====
nc -z 127.0.0.1 8443 && nc -z 127.0.0.1 8444          # 两端口可达
curl -sS --max-time 8 -o /dev/null -w "%{http_code}" https://www.workbuddy.ai/v3/config   # 应 200
curl -sS --max-time 8 -o /dev/null -w "%{http_code}" https://sg.tgalileo.com/v1/traces    # 任意码都行，能连即可

# ===== 重启 WorkBuddy AI（Cmd+Q 再开）→ 等 30 秒 → 发一条消息 =====

# ===== 提取 token =====
cd /Users/zhengyusheng/Documents/workbuddy2api/server
uv run extract_flows_token.py /tmp/wb-intl3.mitm --output ~/.workbuddy-device-token.json
# 若提示无匹配，再试 tgalileo 的 flows：
uv run extract_flows_token.py /tmp/wb-tg.mitm --output ~/.workbuddy-device-token.json

# ===== 清理（必须完整执行）=====
sudo pkill -f mitmdump
sudo sed -i '' -e '/www.workbuddy.ai/d' -e '/sg.tgalileo.com/d' /etc/hosts
sudo pfctl -F all
sudo dscacheutil -flushcache

# ===== 注入 8788 =====
lsof -nP -iTCP:8788 -sTCP:LISTEN | awk 'NR>1{print $2}' | xargs kill
cd /Users/zhengyusheng/Documents/workbuddy2api/server
nohup uv run codebuddy_proxy.py --port 8788 --desensitize \
  --endpoint https://www.workbuddy.ai --platform workbuddy-ai \
  --device-token ~/.workbuddy-device-token.json \
  --session-file ~/.workbuddy-ai-session.json \
  --log-file logs/proxy-intl.jsonl > /tmp/proxy8788.log 2>&1 &
# 验证：health → 发一条消息 → 控制台消费记录来源列应显示官方标记
```

## 四、/tmp/wb-fix.py 内容（若已丢失，重建）

```python
"""mitm addon: feature-flags 404→200 修复 + 全请求记录（含 token）"""
import json
from mitmproxy import http

FAKE = {
    '/feature-flags/check': {"data": {"flags": {}, "enabled": []}, "code": 0, "msg": "OK"},
    '/v2/as/users/private-configs/query': {"data": {}, "code": 0, "msg": "OK"},
    '/v2/as/connector/oauth/ardot/status': {"data": {"status": 0}, "code": 0, "msg": "OK"},
    '/v2/backgroundagent/localProxy/register': {"data": {"endpoint": ""}, "code": 0, "msg": "OK"},
    '/console/agent-gateway/netdrive/mcp': {"data": {"servers": []}, "code": 0, "msg": "OK"},
}

def response(flow: http.HTTPFlow) -> None:
    for k, v in FAKE.items():
        if k in flow.request.path and flow.response.status_code in (403, 404, 503):
            flow.response.status_code = 200
            flow.response.headers['content-type'] = 'application/json'
            flow.response.set_text(json.dumps(v))
            break
    dt = flow.request.headers.get('x-device-token')
    rec = {'method': flow.request.method, 'path': flow.request.path[:80],
           'status': flow.response.status_code if flow.response else None,
           'dt_prefix': dt[:12] if dt else ''}
    if dt:
        rec['token'] = dt
        rec['uid'] = flow.request.headers.get('x-user-id', '')
    with open('/tmp/wb-all-requests.jsonl', 'a') as f:
        f.write(json.dumps(rec) + '\n')
```

## 五、其他已知信息（省得重新查）

- 海外 token 是场景化的：chat 请求上的 token 与 billing/console 路径的不同，取 /v2/chat/completions 上的
- token 时效：国内那份 24h+ 实测仍有效，不是小时级
- 提取工具已就绪：`server/extract_flows_token.py`（121 测试全过，用国内 flows 端到端验证过正确性）
- 国内 8787 当前完整防护运行中（指纹+token+熔断+限速），消费归因已验证 `CodeBuddyIDE`
- 仓库状态：HEAD=dd2a6af 已推送，工作区干净，两个 bridge 在跑

## 六、给接手者的三句话

1. 先跑第三节自测命令，四个全绿再碰 App——任何一步红了不要推进，先修
2. mitmdump 是前台进程，终端一关就死，这就是之前所有 3002 的原因
3. 用户已不满，全程用"已验证事实"说话，每个动作前先自测并汇报结果

---

## 七、最终结论（2026-09-25，三轮抓包审计后定稿）

### 7.1 国际版 x-device-token：确认不存在，任务关闭

三轮完整抓包（00:28、02:24、02:26，含 t006 实测报文）结论一致：

- `/v2/chat/completions` 请求头**从不携带** `x-device-token`，也从不携带
  `X-Device-Token-Error`；全部 113 条 addon 记录中带 token 的 0 条
- 唯一授权方式是 JWT Bearer（claims 无任何设备字段）
- App 日志每轮都是同一条：`[TuringSdk] device token fetch failed /
  TuringShield standardService is unavailable`
- 二进制层根因（已 RE 验证）：turing 节点的 serverURL 全局变量
  （+0x965f8）只由 `setupTMFHTTPServer:` 写入，而该函数**零调用者**；
  国内节点（协议=5）跳过这个返回 nil 的 standardService，所以国内能用
- 结论：这不是抓包姿势问题，是官方客户端本身拿不到。**不要再尝试抓取。**

### 7.2 最终方案：头指纹伪装（已上线 8788 并回归测试）

国际版 chat 请求头**100% 对齐官方真实抓包**（三轮审计，最终端到端实测
38 项头全部 ⊆ 官方头集，零多余头）：

- User-Agent: `WorkBuddy/5.5.2 WorkBuddy/5.5.2 CLI/2.137.1`
  （x-stainless-runtime=node/v22.21.1 同源）
- X-IDE-Name/Type: WorkBuddy；X-IDE-Version: 5.5.2（国内 4.12.0）
- X-Product: SaaS（t006 实测官方会带，无 x-product-code/version/env-id）
- X-Request-ID **与 X-Conversation-Message-ID 同值**（国际版特性；国内与
  request-id 同值），代码按平台分支
- 国际版**不发**：x-model-id、x-request-trace-id、accept-language、
  sec-fetch-mode、monitor_httpsendtime、x-device-token
- httpx 默认注入的 `accept-encoding` 在国际版发送前剥离
  （`_strip_http_framework_noise()`，官方 undici 不发）
- 唯一已知缺口：`acp-connection-id`（无真实 ACP 值，伪造风险 > 收益）

回归测试锁定：`test_fingerprint_headers.py::TestIntlOfficialHeaderParity`
（官方头集白名单 + 禁发名单 + 值断言，共 103 测试全过）。

### 7.3 抓包环境已全部拆除（safety checklist）

- hosts 条目已清、pf rdr 已清、mitmdump×2 + 看门狗×2 已杀
- `~/.workbuddy-ai/system-ca-bundle.pem`（mitm CA）**已删除**
- 两个 bridge：8787（国内，uptime 连续未动）/ 8788（国际，伪装头新版）

## 八、外部项目交叉验证（2026-09-25，ardeyouxipianyi/workbuddy2api-hub + xiaofan6ya/workbuddy2api）

两家独立项目的逆向结论与本仓库互相印证：

- **xiaofan6ya §10.9.1**：Turing SDK 对 HTTP 只暴露 `X-Device-Token`／
  `X-Device-Token-Error` 两个互斥头，无任何 `X-Turing-*` 签名头，token 是
  原生 DLL 采集硬件指纹后换取的**不透明字符串**，纯 Python 无法复刻；官方
  客户端自带"未配置就降级"路径，**不发该头不算异常**
- 其 §10.9 与我们一样用**反向断言测试**锁死官方头型
  （`test_client_header_shape_matches_official`），并强调 traceparent/b3/
  X-Trace-ID **必须同源**，"发得不一致比不发更糟"——已落地为
  `TestIntlOfficialHeaderParity::test_trace_headers_same_origin`
- **hub v1.5.0** 修正记录：官方是 `X-Product: SaaS`（自创值会被识别），与
  我们 t006 抓包一致；hub 另确认 `www.codebuddy.ai` 无法解析、国际版 CLI
  身份也只能走 `www.workbuddy.ai`
- 两家都**刻意不做跨区域同名模型混合轮询**（指纹对齐 + 防关联考量），与
  本仓库双 bridge 物理隔离一致
- 注意：他们的 `X-Private-Data:false` / `Accept-Language:zh-CN` 是**国内
  5.5.6 CLI 身份**观测，与本国际版抓包不同，**不照抄**

## 九、生产事故记录：httpx `send()` 不是上下文管理器（2026-09-25）

- 现象：唯一一次真实请求返回空 body，日志 `TypeError: 'coroutine' object
  does not support the asynchronous context manager protocol`
- 根因：`httpx.AsyncClient.send()` 是协程（返回需 await 的 Response），
  **不支持 `async with`**；必须 `resp = await client.send(req, stream=True)`
  后 `try/finally: await resp.aclose()` 手动关闭。为剥离 accept-encoding 从
  `client.stream()` 改 `build_request + send` 时踩中
- 教训：mock 必须镜像真实库语义——本仓库测试 fake 曾用同步函数返回 CM，
  与真实 httpx 不一致，导致 104 个测试全过但生产即挂。现已改为 async
  send + 带 `aclose` 的 fake，并用本地假上游服务器（127.0.0.1）验证过
  真实 httpx 的 await+aclose 流式机制后才发真实请求
- 该 TypeError 在任何网络 I/O 之前抛出，**上游零请求、账号零接触**
- 另一教训：后台进程必须 double-fork（`/tmp/daemon8788.py`），普通
  `nohup &` 会被工具会话的 SIGHUP 带走（本仓库历史上第 4 次踩坑）
