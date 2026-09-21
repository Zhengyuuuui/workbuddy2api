# WorkBuddy2API 封禁事件复盘与改进执行清单

> 时间线：2026-09-12 ~ 2026-09-21
> 结论先行：封禁 = 通道可识别 + 设备指纹缺失 + 机器行为画像，三者叠加触发腾讯云 p_tcaca 产品线风控（错误码 6004 → 11140）。

---

## 1. 事件时间线

| 时间 | 事件 |
|---|---|
| 09-12 17:10 | 国内 8787 首次出现请求失败（2552 条消息的大请求连发） |
| 09-13 09:29 | 国内+海外同时 429（code 6004 频率限制），上游给出重置时间 |
| 09-13 14:48 | 国内 429 重置后恢复（小请求正常，大请求仍易触发） |
| 09-15 14:37 | 海外最后一次成功（428→434 条消息高频连发） |
| 09-15 17:11 | 海外开始全部 403（11140 request illegal） |
| 09-16 09:24 | 官方 WorkBuddy IDE（5.3.13）同样 403 → 实锤账号级封锁 |
| 09-16 | 提交工单申诉（官方答复：临时风控 1-3 工作日自动解除） |
| 09-19 20:24 | 国内最后一次成功（987~993 条消息连发后中断） |
| 09-20 | 国内也开始 403（11140），官方 IDE 同样报错 |
| 09-21 中午 | 按官方指引等待后，国内账号晚间恢复正常 |
| 09-21 20:47 | mitmproxy 抓包成功，获得官方 IDE 完整请求指纹 |

## 2. 封禁根因（抓包实锤）

### 2.1 通道本身可识别

- bridge 走 `/v2/plugin/`（IDE 插件外部协议），官方 App 走自有通道
- 消费记录中请求 ID 前缀不同：bridge `cmb-` / 计费网关 `crb-` / App 纯 hex
- 消费记录来源列：bridge 显示 "-"，官方 App 显示 "WorkBuddy"
- **结论：从第一笔请求起，后台就能区分哪些是程序化流量**

### 2.2 请求画像差异（抓包对比，详见 mitm-capture-playbook.md 第 3 节）

| 维度 | 官方 IDE | 旧 bridge |
|---|---|---|
| x-device-token（465 字符设备指纹） | ✅ | ❌ |
| x-conversation-id/message-id/request-id | ✅ 链式 | ❌ 孤立 |
| previous_response_id（会话延续） | ✅ | ❌ |
| user-agent | CodeBuddyIDE/4.12.0 | Python |
| x-b3-traceid 链路追踪 | ✅ | ❌ |
| max_tokens | 393216 | 10~100 |
| body 压缩 | gzip | 未压缩 |
| 请求节奏 | 人类操作 | Agent 连发 |
| 单请求体量 | 常规 | 最多 2552 条消息 |

### 2.3 风控机制推断

1. **6004（429）**：频率限制 = 警告阶段，按小时/天重置
2. **11140（403）**：request illegal = 执行阶段，账号级拦截
   - 最小请求（"hi"）也被拒 → 与内容无关
   - 官方 IDE 同样报错 → 与客户端无关，纯账号标记
   - 申诉可解 + 定时解封 → 启发式风控，非实锤
3. 触发画像：纯 "-" 来源 + 高频连发 + 超大上下文 + 无设备指纹 + 429 后热重试

## 3. 改进执行清单

### P0 立即执行（低风险，半小时）

- [ ] `codebuddy_proxy.py`：转发上游时替换/补齐 headers
  - [ ] `user-agent: CodeBuddyIDE/4.12.0 CodeBuddy/4.12.0`
  - [ ] `x-ide-name: CodeBuddyIDE` / `x-ide-type: CodeBuddyIDE` / `x-ide-version: 4.12.0`
  - [ ] `x-product: SaaS` / `x-product-code: codebuddy` / `x-product-version: 4.12.0`
  - [ ] `x-env-id: production` / `x-domain: www.codebuddy.cn`（海外按平台换）
  - [ ] `x-requested-with: XMLHttpRequest` / `accept: */*`
  - [ ] `x-model-id: <实际请求的模型>`（当前 body 里的 model）
- [ ] `max_tokens`：客户端未指定时默认 393216
- [ ] `x-agent-intent: craft`（chat 场景）

### P1 核心对抗（需要开发）

- [ ] **x-device-token 提取与注入**
  - 从 CodeBuddy CN IDE 用户数据目录提取（leveldb/local_storage）
  - 提取脚本 + 注入开关（`--device-token` 参数或自动发现）
  - ⚠️ 纪律：一设备一账号一 token，严禁多账号共用
- [ ] **会话链路维护**
  - proxy 按"客户端连接的会话"生成 `x-conversation-id`（UUID→32hex）
  - 每消息生成 `x-conversation-message-id`，每请求生成 `x-conversation-request-id`/`x-request-id`
  - 跨请求保持同会话同 conversation-id
- [ ] **previous_response_id 透传**
  - 检查上游非流式/流式响应中的该字段
  - proxy 按会话缓存，下一请求自动带上
- [ ] **b3 追踪链**
  - 每请求生成 `x-b3-traceid`（32hex）/`x-b3-spanid`（16hex）/`x-b3-sampled: 1`/`b3` 复合头
  - 同会话内 traceid 可复用（更像真实链路）

### P2 行为层（生产必配）

- [ ] **429 熔断器**
  - 解析上游 429/6004 → 熔断该模型 ≥5 分钟（读上游 reset 时间更佳）
  - 熔断期间直接快速失败，不打上游
- [ ] **限速与抖动**
  - 全局 QPS 上限 + 请求间随机延迟 3~10s
  - 客户端重试指引写入 README（禁止 <5s 热重试）
- [ ] **上下文治理**
  - `--optimize-context` 默认开启
  - 单请求消息数硬上限（建议 ≤100），超出自动摘要压缩
- [ ] **额度监控**
  - 轮询 `POST /billing/meter/get-user-resource`（海外可用，国内 403 需换路径）
  - 暴露 `GET /v1/credits`；消耗速率异常（如 5 分钟烧掉 20%）自动熔断
- [ ] **账号卫生**
  - 每个账号保持官方 IDE 真实使用记录（账单来源混合）
  - 一台设备一个账号；不注册小号、不共享设备指纹
  - 签到/积分等 API 轮询频率压到人工水平

### P3 观察项

- [ ] 关注官方版本更新后 header 变化（版本号硬编码会过期）
- [ ] 长期评估腾讯云官方 OpenAPI（`p_tcaca` 产品线）作为生产替代

## 4. 应急预案（再次被封时）

1. **立即停手**：停掉 bridge 全部流量（风控惩罚与持续请求正相关）
2. **判断阶段**：
   - 429/6004 → 等上游给的 reset 时间，期间换模型
   - 403/11140 → 官方 IDE 验证：IDE 也报错 = 账号级
3. **官方 IDE 自测**：确认账号级后停止一切 API 请求
4. **申诉**：官方工单（WorkBuddy Enterprise 分类），描述"正常使用出现 11140"，
   附 Trace ID，强调未用代理/VPN、版本最新、余额正常
   - 官方口径：1-3 工作日自动解除，勿重复提单
5. **解封后**：先官方 IDE 轻度使用 1-2 天，再恢复 bridge（小流量渐进）
6. **红线**：解封前严禁换号继续打同一设备——设备指纹会连坐

## 5. 本次抓包方法沉淀

完整操作手册已固化：`doc/mitm-capture-playbook.md`（macOS + Electron/Node 应用通用，
含方案选型决策树、踩坑记录、清理规范）。其他软件逆向时直接按该文档执行。

原始数据：
- `.ydevsphere/official-ide-headers.txt` —— 官方请求 headers 快照（脱敏后）
- `/tmp/wb-capture/flows-*.mitm` —— 原始流量包（/tmp 重启丢失，重要内容已摘录进文档）
