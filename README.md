# WorkBuddy2API

> WorkBuddy 国际国内多账号反代网关 — 双域独立路由与切换、稳定设备指纹防风控、国内成长任务全自动完成、后台定时调度器、Web监控看板，支持 Codex / Claude Code / DSH 与标准 OpenAI 客户端。

## 🔧 官方客户端指纹模拟

代理请求默认模拟官方 CodeBuddy IDE 的请求特征，避免因流量特征与官方客户端差异过大而触发平台风控（参考[踩坑记录](doc/incident-postmortem-and-hardening-checklist.md)）：

- 请求头对齐官方 `CodeBuddyIDE/4.12.0`（UA、x-ide-*、x-product-* 等）
- 会话级 conversation_id 复用，previous_response_id 随会话链式传递
- b3 追踪链同会话延续；大请求体自动 gzip；max_tokens 与官方一致
- `x-device-token` 支持通过环境变量 / `--device-token` 注入（获取方式见[抓包手册](doc/mitm-capture-playbook.md)）

## 项目简介

一个轻量级的本地 API 代理服务，将腾讯 **CodeBuddy（国内版）** 与 **WorkBuddy AI（海外版）** 的底层接口转换为标准的 OpenAI、Anthropic 和 Responses 协议格式。

**同一个 proxy，两个平台，一套模型池：**

- 🇨🇳 **国内 CodeBuddy**（`copilot.tencent.com`）→ GLM-5.3、DeepSeek-V4、Kimi-K3、MiniMax-M3、混元 Hy4 等**国产模型**
- 🌍 **海外 WorkBuddy AI**（`www.workbuddy.ai`）→ **GPT-5.6-Sol/Terra/Luna、GPT-5.5、GPT-5.4、GPT-5.3-Codex、Gemini-3.5-Flash** 等海外模型，外加 GLM / Kimi / 混元

两个平台使用**完全相同的认证协议**，因此本项目复用同一套代码，启动参数一键切换。
国内跑不了的 GPT / Gemini 系列，走海外端口即可使用。

## 项目概览

本项目支持两个平台，同一套代码，通过启动参数切换：

| | 国内版 CodeBuddy | 海外版 WorkBuddy AI |
|---|---|---|
| **产品** | CodeBuddy（腾讯云国内） | WorkBuddy AI（腾讯海外） |
| **后端 Endpoint** | `https://copilot.tencent.com` | `https://www.workbuddy.ai` |
| **认证 platform** | `VSCode` | `workbuddy-ai` |
| **Session 文件** | `~/.codebuddy-session.json` | `~/.workbuddy-ai-session.json` |
| **默认端口** | `8787` | `8788`（自定义） |
| **模型特点** | 国产模型为主（GLM / DeepSeek / Kimi / MiniMax / 混元） | 含 GPT-5.6 / Gemini 等海外模型 |

两个平台使用**完全相同的认证协议**（`cli-external-link`，`/v2/plugin/` 前缀），
因此本项目复用同一套认证与代理逻辑，仅 endpoint / platform / session 不同。

## 目录结构

```
workbuddy2api/
├── server/                          # 全部服务端代码
│   ├── codebuddy_proxy.py           # 主入口（两平台共用）
│   ├── codebuddy_client_demo.py     # 认证客户端（两平台共用）
│   ├── workbuddy_ai_client_demo.py  # 海外版独立客户端（登录/测试）
│   ├── dsml_parser.py               # DSML 工具调用解析
│   ├── desensitize.py               # 脱敏模块
│   ├── responses_adapter.py         # Responses API 转换
│   ├── responses_projection.py      # 消息压缩
│   ├── anthropic_adapter.py         # Anthropic API 转换
│   └── test_*.py                    # 测试
├── pyproject.toml
└── README.md
```

## ✨ 核心特性

- **双域独立路由与切换** - 国内 CodeBuddy 与海外 WorkBuddy AI 一键切换，独立路由互不干扰
- **官方客户端指纹模拟** - 请求特征对齐官方 CodeBuddyIDE 客户端（UA、会话 ID 链、previous_response_id 延续、b3 追踪链等）
- **协议转换** - 支持 OpenAI Chat Completions、Anthropic Messages API 和 Responses 三种标准格式，兼容 Codex / Claude Code / DSH 等主流客户端
- **脱敏处理** - 内置智能脱敏模块，自动过滤敏感信息（账号、密码、密钥、品牌词、路径等），有效缓解审核误拦
- **消息压缩** - 智能压缩历史消息，大幅降低 token 使用量（适用于 Codex CLI 等长上下文场景）
- **工具调用支持** - 完整支持 function calling 和 tool use 特性
- **DSML 解析** - 自动识别和转换 DSML 格式的工具调用
- **流式响应** - 支持 SSE 流式输出，实时返回生成内容
- **多账号管理** - 支持多个登录态隔离，方便工作/个人账号切换
- **国内成长任务自动化** - 签到等成长任务全自动完成（规划中）
- **后台定时调度器 / Web 监控看板** - 额度监控、任务调度、状态可视化（规划中）

## 安装

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 直接运行（uv 会自动安装依赖）
uv run server/codebuddy_proxy.py
```

## 快速开始

所有命令均在 `server/` 目录下执行（或用 `uv run server/codebuddy_proxy.py` 从根目录执行）。

### 登录信息

| | 国内版 CodeBuddy | 海外版 WorkBuddy AI |
|---|---|---|
| **登录 URL** | `https://copilot.tencent.com/login?platform=VSCode&state={state}` | `https://www.workbuddy.ai/login?platform=workbuddy-ai&state={state}` |
| **Session 文件** | `~/.codebuddy-session.json` | `~/.workbuddy-ai-session.json` |
| **认证 platform** | `VSCode` | `workbuddy-ai` |

> 登录流程：proxy 启动时会自动获取 `state` 并打开浏览器，用户完成登录后 token 自动保存到 session 文件。

### 1. 国内版 CodeBuddy（端口 8787）

```bash
cd server

# 首次使用（浏览器登录）
uv run codebuddy_proxy.py --login

# 日常使用
uv run codebuddy_proxy.py --desensitize
```

默认监听 `http://127.0.0.1:8787`

### 2. 海外版 WorkBuddy AI（端口 8788）

```bash
cd server

# 首次使用（浏览器登录）
uv run codebuddy_proxy.py --port 8788 \
  --endpoint https://www.workbuddy.ai \
  --platform workbuddy-ai \
  --session-file ~/.workbuddy-ai-session.json \
  --login

# 日常使用
uv run codebuddy_proxy.py --port 8788 \
  --endpoint https://www.workbuddy.ai \
  --platform workbuddy-ai \
  --session-file ~/.workbuddy-ai-session.json \
  --log-file logs/proxy-intl.jsonl
```

监听 `http://127.0.0.1:8788`（自定义）

### 3. 验证

```bash
# 国内
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models

# 海外
curl http://127.0.0.1:8788/health
curl http://127.0.0.1:8788/v1/models
```

### 4. 接入客户端

#### Codex CLI

编辑 `~/.codex/config.toml`：

```toml
[model_providers.codebuddy]
name = "CodeBuddy (via local proxy)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.codebuddy]
model = "glm-5.3"
model_provider = "codebuddy"
```

#### Claude Code + CC Switch

在 CC Switch 配置中添加：

```json
{
  "DeepSeek-V4": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

#### 其他 OpenAI 兼容客户端

- Base URL: `http://127.0.0.1:8787/v1`（国内）或 `http://127.0.0.1:8788/v1`（海外）
- API Key: 留空
- 模型名见下方模型列表

## 模型列表

### 国内版 CodeBuddy（19 个）

| 模型 ID | 显示名 | 厂商 |
|---|---|---|
| `auto` | Auto | codebuddy |
| `default` | Default | codebuddy |
| `hy4-preview` | Hy4 preview | tencent |
| `hy3` | Hy3 | tencent |
| `hunyuan-chat` | Hunyuan-Turbos | tencent |
| `glm-5.3` | GLM-5.3 | zhipu |
| `glm-5.3-flash` | GLM-5.3-flash | zhipu |
| `glm-5.2` | GLM-5.2 | zhipu |
| `glm-5.1` | GLM-5.1 | zhipu |
| `glm-5v-turbo` | GLM-5v-Turbo | zhipu |
| `kimi-k3` | Kimi-K3 | moonshot |
| `kimi-k3-1` | Kimi-K3.1（未公开发布） | moonshot |
| `kimi-k2.7` | Kimi-K2.7-Code | moonshot |
| `kimi-k2.6` | Kimi-K2.6 | moonshot |
| `minimax-m3` | MiniMax-M3 | minimax |
| `minimax-m2.7` | MiniMax-M2.7 | minimax |
| `deepseek-v4-pro` | Deepseek-V4-Pro | deepseek |
| `deepseek-v4-flash` | Deepseek-V4-Flash | deepseek |
| `deepseek-v4.1-flash` | Deepseek-V4.1-Flash | deepseek |

### 海外版 WorkBuddy AI（21 个）

| 模型 ID | 显示名 | 厂商 |
|---|---|---|
| `default-model` | Auto | codebuddy |
| `fast-model` | Fast | codebuddy |
| `balanced-model` | Balanced | codebuddy |
| `primary-model` | Primary | codebuddy |
| `deep-model` | Deep | codebuddy |
| `hy4-preview` | Hy4 preview | tencent |
| `hy3` | Hy3 | tencent |
| `gpt-5.6-sol` | GPT-5.6-Sol | openai |
| `gpt-5.6-terra` | GPT-5.6-Terra | openai |
| `gpt-5.6-luna` | GPT-5.6-Luna | openai |
| `gpt-5.5` | GPT-5.5 | openai |
| `gpt-5.4` | GPT-5.4 | openai |
| `gpt-5.3-codex` | GPT-5.3-Codex | openai |
| `gemini-3.5-flash` | Gemini-3.5-Flash | google |
| `glm-5.3` | GLM-5.3 | zhipu |
| `glm-5.2` | GLM-5.2 | zhipu |
| `kimi-k3` | Kimi-K3 | moonshot |
| `kimi-k2.6` | Kimi-K2.6 | moonshot |
| `kimi-k2.5` | Kimi-K2.5 | moonshot |
| `minimax-m3` | MiniMax-M3 | minimax |
| `deepseek-v4.1-flash` | Deepseek-V4.1-Flash | deepseek |

> 模型列表会随平台更新变化，以 `GET /v1/models` 的实际返回为准。

## 命令行参数

```bash
--host HOST              监听地址（默认 127.0.0.1）
--port PORT              监听端口（默认 8787）
--endpoint ENDPOINT      后端地址（国内默认 copilot.tencent.com，海外用 www.workbuddy.ai）
--platform PLATFORM      认证 platform（国内 VSCode，海外 workbuddy-ai）
--session-file PATH      会话文件路径
--log-file PATH          JSONL 日志文件
--desensitize            启用脱敏处理（推荐）
--optimize-context       启用消息压缩优化（Codex CLI 推荐）
--login                  启动时执行浏览器登录
--no-browser             登录时不打开浏览器
--verbose-llm            记录完整请求/响应内容（默认仅摘要）
--mock-dir DIR           使用 mock 数据（测试用）
```

### 环境变量

```bash
CODEBUDDY_PROXY_HOST      # 等同 --host
CODEBUDDY_PROXY_PORT      # 等同 --port
CODEBUDDY_ENDPOINT        # 等同 --endpoint
CODEBUDDY_PLATFORM        # 等同 --platform
CODEBUDDY_PROXY_LOG_FILE  # 等同 --log-file
```

## API 接口

所有接口默认不需要在请求中额外携带 token，代理会使用本地 session 完成认证。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/health` | 查询本地服务和认证状态 |
| GET | `/v1/models` | 查询模型列表 |
| POST | `/v1/chat/completions` | OpenAI Chat Completions，支持 tools 和流式响应 |
| POST | `/v1/responses` | Responses API，兼容 Codex CLI |
| POST | `/v1/messages` | Anthropic Messages API，兼容 Claude Code / CC Switch |

### `/v1/chat/completions` - OpenAI Chat

**非流式请求：**

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "写一个快排"}]
  }'
```

**流式请求：**

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm-5.2",
    "stream": true,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

支持 `tools`、`tool_choice`、`stream_options` 等完整 OpenAI 特性。

### `/v1/responses` - Responses API

用于兼容 Codex CLI：

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "default",
    "input": "写一个快排"
  }'
```

支持 `instructions` (system prompt)、消息形式的 `input`、`tools`、`tool_choice` 和 `stream`。

**💡 提示：** 使用 `--optimize-context` 可大幅减少 Codex CLI 的 token 使用。

### `/v1/messages` - Anthropic Messages

用于兼容 Claude Code / CC Switch：

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-v4-pro",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

设置 `"stream": true` 时返回 Anthropic SSE 事件流。

## 高级功能

### 脱敏处理 (`--desensitize`)

对 system 消息中的敏感词插入零宽空格（U+200B），打断后端关键词匹配，缓解合规模板被审核误拦。

#### 何时需要使用

**强烈推荐启用的场景：**

1. **对接 Claude Code / CC Switch**
   - Claude Code 的 system prompt 包含大量品牌词和安全合规声明
   - 后端可能将竞争品牌词视为敏感内容
   - 不启用脱敏时，几乎每次请求都会被审核拦截

2. **对接 Codex CLI 等 agentic 工具**
   - 这些工具的 system prompt 含有大量安全术语（DoS、exploit、credential testing 等）
   - 即使是合规的"拒绝有害请求"声明，也可能被关键词匹配误拦

**不需要启用的场景：**
- ✅ 普通对话（无安全术语）
- ✅ 纯粹的代码生成（无品牌词/安全声明）

#### 注意事项

- ✅ 只处理合规声明，不绕过对有害输入的审核
- ✅ 只改 system 消息，真实用户输入保持原样
- ⚠️ 零宽空格对人眼/模型透明，但会影响精确字符串匹配

---

### 消息压缩优化 (`--optimize-context`)

仅对 `/v1/responses` 端点生效，将长历史、大 schema、超长工具输出压缩成"最小语义闭包"，大幅减少 token 使用（可能减少 60-90%）。

```bash
# 同时启用两个功能（推荐用于 Codex CLI）
uv run codebuddy_proxy.py --desensitize --optimize-context
```

- ✅ 只用于 `/v1/responses`，不影响 chat/messages 端点
- ✅ 保留语义闭包，模型仍可推理
- ⚠️ 历史被摘要化，精确细节需重新运行工具获取

---

### 日志

```bash
# 实时查看
tail -f logs/codebuddy-proxy.jsonl

# 查看流式事件
tail -100 logs/codebuddy-proxy.jsonl | jq 'select(.event | startswith("stream"))'
```

## 常见问题

### 找不到 session 文件 / 401 认证失败

重新登录：

```bash
# 国内
uv run codebuddy_proxy.py --login

# 海外
uv run codebuddy_proxy.py --port 8788 --endpoint https://www.workbuddy.ai --platform workbuddy-ai --session-file ~/.workbuddy-ai-session.json --login
```

### 端口被占用

```bash
lsof -i :8787
uv run codebuddy_proxy.py --port 8789
```

### 海外版调用报错 "first message is not system prompt"

proxy 已自动处理（会自动补 system 首条消息），如仍有问题请更新到最新代码。

## 技术细节

- **架构**: FastAPI + httpx（异步）
- **并发**: 支持 1000+ 并发请求
- **流式**: 完整的流式日志（started / progress / completed / timeout）
- **认证协议**: `cli-external-link`（浏览器 SSO 登录 → 轮询 token → 自动刷新）

## 测试

```bash
uv run pytest server/test_*.py -v
```

## 免责声明

**本项目仅用于个人学习与研究。** 与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

- 本项目不提供任何形式的担保
- 使用本项目产生的任何后果由使用者自行承担
- 请勿将本项目用于任何违反相关服务条款的用途
- 请勿将本项目用于商业用途
