# Skill：AI 客户端 / 私有后端协议逆向工作流

> 本 Skill 由一次真实的逆向过程提炼而来：
> 目标是从本地 Electron 客户端（WorkBuddy AI）中还原其私有 OAuth 认证协议与后端 API，
> 并复用已知的姊妹产品（CodeBuddy）协议完成端到端接入。

---

## A. Research Methodology Map（方法论地图）

本次真实逆向的完整路径：

```
目标：复用 CodeBuddy 协议 → 接入海外 WorkBuddy AI 后端
  │
  ├─ [阶段1] 代码库侦察：找到已实现的 OAuth 客户端 (codebuddy_client_demo.py)
  │     → 确认认证协议 = external-link-v2 (POST /v2/plugin/auth/state 等)
  │     → 证据：本仓库已有可运行实现（Confirmed）
  │
  ├─ [阶段2] 产品定位：确认 WorkBuddy AI 是腾讯海外产品
  │     → Web 调研 (webfetch workbuddy.ai/cc/cn) → 只有产品介绍，无 API 文档
  │     → 结论：公开资料不足（Unknown → 转向本地静态分析）
  │
  ├─ [阶段3] 本地二进制/应用侦察
  │     → 发现 ~/Library/Application Support/com.workbuddy.workbuddy 与 /Applications/WorkBuddy AI.app
  │     → 确认是 Electron 应用（Contents/MacOS/Electron）
  │     → 主逻辑在 app.asar（283MB 压缩包）
  │
  ├─ [阶段4] 静态信息收集
  │     → strings app.asar | grep 域名 → 发现 api.lkeap.cloud.tencent.com, smh*.tencentsmh.com
  │     → 解包 app.asar（@electron/asar）→ 得到可读 JS + cli/product.json
  │     → product.json = 金矿：认证配置、endpoint、domain 白名单、models 全在此
  │
  ├─ [阶段5] 认证路径还原
  │     → cli/dist/codebuddy.js 中搜 "auth/state" → 确认 POST /v2{prefix}/auth/state?platform=
  │     → 确认 prefixPath=/plugin、platform=workbuddy-ai
  │
  ├─ [阶段6] 运行时配置确认（重要：静态声明 ≠ 运行时值）
  │     → 查 local_storage leveldb 缓存 → 实际 endpoint = https://www.codebuddy.ai
  │     → 静态 product.json 声明 www.workbuddy.ai，但运行时被 /v3/config 覆盖为 www.codebuddy.ai
  │
  ├─ [阶段7] 动态验证
  │     → curl POST /v2/plugin/auth/state?platform=workbuddy-ai → HTTP 200 + authUrl
  │     → 复用客户端改 endpoint → 浏览器登录成功 → token 入 session
  │     → 实测 chat（需补 system 首条消息）→ 多模型返回 OK
  │
  └─ [阶段8] 落地接入
        → 修改 proxy：加 --platform 参数、动态 /v1/models、自动补 system 首条
        → 独立端口 8788 启动，/v1/chat/completions 验证通过
```

**核心方法论教训：**
1. 有已实现协议时，优先**代码库侦察**而非重新逆向（省大量时间）
2. 公开文档不足时，**本地静态分析**优先级高于 Web 调研
3. 静态声明（product.json）与运行时值（缓存）可能不一致，**必须查运行时缓存**
4. 姊妹产品协议**可复用**，但要验证域名、platform、消息格式的差异

---

## B. Reusable Workflow（可复用工作流）

### Phase 0：目标定义
- **Goal**：明确要还原什么（认证？模型列表？聊天端点？Token 获取？）
- **Input**：目标软件名、已知线索、期望产出（协议文档 / 可运行脚本 / 接入 proxy）
- **Actions**：
  1. 把目标拆成可验证的原子问题（"它怎么认证？""端点在哪构造？""token 存哪？"）
  2. 检查本地是否有同门/姊妹产品的已知实现可复用
- **Tools**：仓库搜索（grep/glob）、README、已知脚本
- **Expected Evidence**：一份问题清单 + 是否已有可复用代码的结论
- **Stop Conditions**：问题清单已明确、已知线索已穷尽
- **Common Mistakes**：一开始就扑向二进制，跳过"是否已有实现"的检查
- **Next Decision**：若已有同门实现 → 走"复用 + 改参数"路径；否则 → 静态分析

### Phase 1：环境与文件侦察
- **Goal**：定位目标软件的安装位置、形态、运行时产物
- **Input**：目标软件名
- **Actions**：
  1. 查 `/Applications`、`~/Applications`、`~/Library/Application Support`
  2. 判断应用形态（Electron/原生/容器化）→ 看 `Contents/MacOS/`、`Info.plist`
  3. 查运行时数据目录（`~/Library/Application Support/<bundle-id>`、`~/.<product>`）
- **Tools**：`ls`、`/usr/libexec/PlistBuddy`、`find`
- **Expected Evidence**：应用形态、bundle-id、主资源文件路径、userData 目录
- **Stop Conditions**：已找到主代码资源（如 app.asar）与配置目录
- **Common Mistakes**：忽略 userData/缓存目录（运行时值往往在里面，比二进制更关键）
- **Next Decision**：Electron → 解包 asar；原生 → 查动态库/二进制；纯 Web → 查浏览器存储

### Phase 2：静态信息收集（先字符串后解包）
- **Goal**：快速收集域名、路径、标识符，建立线索面
- **Input**：主资源文件（app.asar / 二进制）
- **Actions**：
  1. 先用 `strings <file> | grep -E "https?://..."` 快速捞域名（低代价、高信号）
  2. 按域名/关键字分类：后端 API、认证、遥测、第三方、示例地址
  3. 识别"信号强"的域名（内部/自建如 *.tencent.com 优先于第三方如 api.anthropic.com）
  4. 对 Electron 用 `@electron/asar extract` 完整解包
- **Tools**：`strings`、`grep -oE`、`@electron/asar`、`find`、`sort | uniq -c`
- **Expected Evidence**：域名清单（带出现次数）、解包后的可读源码 + 配置文件
- **Stop Conditions**：已得到可读源码或产品配置文件（product.json 类）
- **Common Mistakes**：
  - 被大量第三方 SDK 域名干扰（api.example.com、api.anthropic.com 等）——过滤掉
  - 只解包不解码：asar 解包后仍有压缩 JS，需用 `grep -oE` / `python` 提取上下文
- **Next Decision**：有 product.json/配置 → 读配置（最高价值）；否则 → 在 JS 里搜路径

### Phase 3：模块定位
- **Goal**：锁定承载认证/核心逻辑的模块与配置文件
- **Input**：解包后的目录树
- **Actions**：
  1. 找 `product*.json`、`config*.json`（认证配置常在此）
  2. 按文件名推断模块（`auth*.js`、`*-client*.js`、`*product-config*.js`）
  3. 用 `python` 在大 JS 里定位关键路径（认证路径、endpoint、header 名）
- **Tools**：`find`、`grep -l`、`python`（regex 提取上下文）
- **Expected Evidence**：认证模块文件 + 产品配置文件的完整字段
- **Stop Conditions**：已找到 `authentication` 配置块（type/prefixPath/platform/domain 列表）
- **Common Mistakes**：根据文件名断言功能而不读内容（文件名可能误导）
- **Next Decision**：配置含认证 → 提取认证路径 + domain 白名单；否则 → 继续搜

### Phase 4：关键逻辑分析（认证协议还原）
- **Goal**：还原完整的认证时序：请求方法/路径/header/参数/返回
- **Input**：认证模块 JS + product.json 的 authentication 块
- **Actions**：
  1. 提取 `authentication`：`type`、`prefixPath`、`tokenHeader`、`platform`、`internalDomain`/`externalDomain`
  2. 在 JS 中搜路径片段（`/auth/state`、`/auth/token`、`login/account`、`token/refresh`）确认时序
  3. 记录每个请求：方法、完整路径、必填 header、body、期望返回字段
  4. 记录 token 的持久化位置（session 文件路径、权限 0600）
- **Tools**：`python` regex、`grep`、源码阅读
- **Expected Evidence**：一份完整认证时序表（端到端），含所有 header 与状态机
- **Stop Conditions**：`/v2{prefix}/auth/state` → `auth/token` → `login/account` 全链路可写清
- **Common Mistakes**：
  - 只找到 type 而不找时序（type 是声明，时序才是可执行协议）
  - 忽略 `X-No-*` 系列 header（无认证状态标记，常被漏掉）
- **Next Decision**：有 session 逻辑 → 还原 refresh 状态机；还要运行时值 → 进入 Phase 5

### Phase 5：运行时配置确认（关键！）
- **Goal**：确认静态声明 vs 运行时实际值（endpoint、已登录账户、模型列表）
- **Input**：Electron 的 userData、local_storage、leveldb、设置文件
- **Actions**：
  1. 找 `local_storage/*.info`、`settings.json`、leveldb（`*/.log`）
  2. 用 `strings` / `grep` 搜 `endpoint`、`accessToken`、`userId`、模型 id
  3. 对比静态 product.json 的 endpoint 与缓存中的 endpoint
- **Tools**：`strings`、`grep`、`python`、`find`
- **Expected Evidence**：运行时 endpoint（可能 ≠ 静态值）、真实 userId、已配置模型 id 列表
- **Stop Conditions**：得到"运行时实际使用的 endpoint" + "该账户可用的模型 id"
- **Common Mistakes**：
  - 把静态配置当作运行时值直接使用（本案例两者不同：workbuddy.ai vs codebuddy.ai）
  - 忽略缓存目录，只分析 asar（丢失运行时真相）
- **Next Decision**：endpoint 已确定 → 进入动态验证

### Phase 6：动态验证
- **Goal**：用真实请求验证还原的协议，确认真实可用
- **Input**：还原的时序 + 运行时 endpoint + 需要登录
- **Actions**：
  1. 先无 token 探测：`curl -X POST <endpoint>/v2/plugin/auth/state?platform=<p>` 看是否返回 authUrl（验证协议 + 域名可达）
  2. 用还原的 client 跑登录（打印 authUrl → 用户浏览器登录 → 轮询 token）
  3. 用拿到的 token 实测目标功能（模型列表 / chat）
  4. 逐模型测试，记录可用/不可用
- **Tools**：`curl`、自研 client（`urllib`）、`webbrowser`
- **Expected Evidence**：真实 authUrl、有效 token、可用的模型调用返回
- **Stop Conditions**：chat/列表端到端返回预期结果
- **Common Mistakes**：
  - 忽略请求差异（如"首条消息必须 system"）→ 用 4xx/5xx 响应体定位差异
  - SSL 证书问题（Python 默认 CA 不足）→ 显式用 certifi
  - 在 `main()` 里用 `input()` 阻塞导致 EOFError → 测试时传参或 `-u`
- **Next Decision**：验证通过 → 落地；部分模型失败 → 判断是否账户未开通（记录，不扩大结论）

### Phase 7：证据链整理
- **Goal**：把 Confirmed / Strong Evidence / Hypothesis 分层归档
- **Input**：全程观察
- **Actions**：按证据等级分类每条结论，标注来源（哪个命令/哪个文件哪一行）
- **Tools**：文档、证据引用
- **Expected Evidence**：可追溯的证据清单（结论 → 证据 → 来源）
- **Stop Conditions**：每条关键结论都有 Confirmed/Strong Evidence 支撑
- **Common Mistakes**：把 Hypothesis 写成 Confirmed
- **Next Decision**：证据充分 → 出报告

### Phase 8：研究报告 / 落地
- **Goal**：产出可执行的产物（协议文档、脚本、proxy 改动）
- **Input**：证据链 + 验证结果
- **Actions**：
  1. 写清：endpoint、认证时序、header、模型列表、已知失败项
  2. 落地为脚本/proxy（加参数化，便于复用）
  3. 记录"复用 vs 新建"的取舍
- **Tools**：脚本、proxy 修改、文档
- **Expected Evidence**：可运行产物 + 参数化切换说明
- **Stop Conditions**：产物通过真实调用验证
- **Common Mistakes**：产物写死单一配置（应参数化 platform/endpoint/session）
- **Next Decision**：交付

---

## C. Agent Decision Rules（决策规则）

1. **先查是否已有实现，再逆向**：仓库里若有同门/姊妹产品协议，优先复用而非从零开始。
2. **公开文档不足 → 本地静态分析优先**：webfetch 只拿到产品介绍时，果断转向本地应用与缓存。
3. **静态声明 ≠ 运行时值**：任何 endpoint/域名，最后都要以运行时缓存或真实请求验证为准。
4. **Electron 优先解包 + 读产品配置**：`product.json` 常一次性给出认证、域名、模型全部关键信息。
5. **按证据等级下结论**：无 Confirmed 支撑的推断一律标 Hypothesis，禁止写成事实。
6. **失败路径同样记录**：哪些命令无效、哪些方向误判，写进 Skill 供下次跳过。
7. **请求差异用响应体定位**：遇到 4xx/5xx，读返回的 code/msg，据其修正（如补 system 首条消息）。
8. **模型不可用 ≠ 协议错误**：某模型 "service info not found" 多为账户未开通，不扩大为协议失败。

---

## D. Tool Selection Rules（工具选择规则）

| 场景 | 首选工具 | 何时用它 | 何时换掉 |
| -- | -- | -- | -- |
| 快速捞域名/路径 | `strings <file> | grep -oE 'https?://...'` | 二进制/asar 未解包 | 需上下文时改用 python regex |
| 解包 Electron | `@electron/asar extract` | 主逻辑在 app.asar | 非 Electron |
| 搜关键路径 | `grep -oE ".{80}<kw>.{120}"` | 压缩 JS 中定位上下文 | 单行可读时直接 grep |
| 读运行时值 | `strings leveldb/*.log` / 读 `.info` | 确认实际 endpoint/token/模型 | 无缓存时回到静态 |
| 动态探测协议 | `curl -X POST <ep>/...` | 无 token 验证协议可达 | 需要会话时用脚本 |
| 认证/会话流程 | 自研 `urllib` client + certifi | 浏览器登录 + 轮询 token | 简单探测用 curl |
| 落地 proxy | 参数化命令行（--platform/--endpoint/--session-file） | 多环境复用 | 单机固定时直接改常量 |

---

## E. Evidence Classification Rules（证据分级规则）

| 等级 | 定义 | 本例示例 |
| -- | -- | -- |
| **Confirmed** | 直接证据（可运行代码、真实响应、配置文件原文） | `product.json` 中 `"prefixPath": "/plugin"`；curl 返回 HTTP 200 + authUrl |
| **Strong Evidence** | 多个独立来源一致 | 静态 product.json 与运行时缓存都指向同一认证协议类型 |
| **Hypothesis** | 合理推测，需验证 | "WorkBuddy 可能复用 CodeBuddy 认证"（已被动态验证升级） |
| **Unknown** | 当前无法确定 | WorkBuddy 后端是否有公开 API 文档 |

**关键区分**：声明（config 写的）与事实（请求实测的）必须分开。本案例中
`www.workbuddy.ai`（静态声明）与 `www.codebuddy.ai`（运行时值）不同，正是
"Strong Evidence 指向声明、但 Confirmed 需要实测"的典型。

---

## F. Skill Draft

以下是可直接放入 `~/.config/opencode/skills/<name>/SKILL.md` 的通用 Skill 草稿。

---

```markdown
---
name: reverse-engineer-client-protocol
description: 系统逆向未知软件/客户端/AI IDE 的认证协议与后端 API。当需要从本地应用还原私有协议、定位后端 endpoint、获取 token 流程、或复用姊妹产品协议时使用。
---

# 私有客户端协议逆向

系统化还原未知软件的认证与 API 协议，产出可执行产物（脚本 / proxy / 文档）。
严格遵循 Evidence → Hypothesis → Verification → Conclusion。

## 核心原则
- 禁止根据文件名/字符串直接断言逻辑。
- 所有结论必须标注可信等级：Confirmed / Strong Evidence / Hypothesis / Unknown。
- 静态声明 ≠ 运行时值，运行时值以缓存与真实请求为准。
- 先查是否已有同门/姊妹产品实现可复用。

## 工作流（8 阶段）

### Phase 0 目标定义
拆解可验证原子问题：认证方式？端点在哪构造？token 如何获取/存储？
先查仓库内是否已有可复用实现。

### Phase 1 环境与文件侦察
- 查 `/Applications`、`~/Library/Application Support/<bundle-id>`
- 判断形态（Electron?）→ `Contents/MacOS/`、`/usr/libexec/PlistBuddy Print :CFBundleIdentifier`
- 查运行时数据目录（userData、`~/.<product>`）

### Phase 2 静态信息收集
- `strings <app.asar/二进制> | grep -oE 'https?://[a-zA-Z0-9._/-]+' | sort -u`
- 过滤第三方 SDK 域名，聚焦自建/内部域名
- Electron 用 `@electron/asar extract` 解包

### Phase 3 模块定位
- 找 `product*.json`（认证配置金矿）、`auth*.js`
- 用 python regex 定位关键路径/header/endpoint

### Phase 4 关键逻辑分析
- 提取 `authentication`：type / prefixPath / tokenHeader / platform / domain 白名单
- 还原完整时序：方法、路径、header（含 X-No-*）、body、返回字段
- 记录 token 持久化位置与权限

### Phase 5 运行时配置确认
- 查 `local_storage/*.info`、`settings.json`、leveldb
- 对比静态声明 vs 缓存中的 endpoint / userId / 模型列表
- ⚠️ 静态与运行时可能不同，以运行时为准

### Phase 6 动态验证
- 无 token 探测：`curl -X POST <ep>/v2/plugin/auth/state?platform=<p>`
- 浏览器登录 + 轮询 token，用 session 实测目标功能
- 逐模型/逐端点测试，读响应体定位请求差异

### Phase 7 证据链整理
- 结论 → 证据 → 来源 一一对应，分级归档

### Phase 8 落地
- 产物参数化（platform / endpoint / session-file），不写死单配置

## 工具速查
- 捞域名：`strings` + `grep -oE`
- 解包 Electron：`@electron/asar`
- 运行时值：`strings leveldb/*.log`
- 动态探测：`curl`
- 认证/会话：`urllib` + certifi（显式 CA，避免 SSL 证书失败）
- 落地：参数化 proxy / 脚本

## 常见失败路径（务必跳过）
- webfetch 官方站拿到的是产品介绍而非 API 文档 → 转向本地静态分析
- 只解包不解码压缩 JS → 用 python regex 提上下文
- 把静态声明当运行时值（workbuddy.ai ≠ codebuddy.ai）
- Python 默认 CA 不足导致 SSL 失败 → 用 certifi
- chat 首条非 system 被拒 → 自动补 system 首条
- 模型 "service info not found" → 多属账户未开通，非协议错误

## 证据分级
- Confirmed：可运行代码 / 真实响应 / 配置原文
- Strong Evidence：多来源一致
- Hypothesis：合理推测，待验证
- Unknown：无法确定
```

---

## 附：本次真实对话的失败路径实录（用于校准）

| 步骤 | 尝试 | 结果 | 教训 |
| -- | -- | -- | -- |
| 1 | webfetch workbuddy.ai/cc/cn | 只有产品介绍，无 API 文档 | 公开文档不足 → 转本地分析 |
| 2 | strings app.asar 找认证路径 | 只找到模型域名，无 auth 路径 | 认证路径在压缩 JS，需解包后搜 |
| 3 | Python 直接登录 | SSL CERTIFICATE_VERIFY_FAILED | 需显式 certifi CA |
| 4 | 复用 client 打 /v3/config | HTTP 400 "check ua" | 需正确 User-Agent |
| 5 | chat 请求 | HTTP 500 "first message is not system" | 海外要求首条 system |
| 6 | 测 deep-model/deepseek | "service info not found" | 账户未开通，非协议错误 |
| 7 | 用 `timeout` 命令 | macOS 无此命令 | 用后台 + kill / `gtimeout` |
