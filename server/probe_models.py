#!/usr/bin/env python3
"""探测上游真实可用的模型列表。

背景：`/v3/config?repos=` 返回的 `models` 与真实可调用集合并不一致 —— 云端会列出
已下线的条目，也会漏掉付费账号可用的条目。因此本脚本对每个候选模型发一条
`max_tokens` 很小的 chat 请求，以真实响应为准。

用法：
    # 探测国内版（8787）
    uv run server/probe_models.py

    # 探测海外版（8788）
    uv run server/probe_models.py --port 8788

    # 追加自定义候选
    uv run server/probe_models.py --extra "glm-9.9 kimi-k9"

    # 从云端配置自动收集候选（遍历所有嵌套字段里的模型 ID）
    uv run server/probe_models.py --from-config

先启动 proxy，再运行本脚本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

# 国内版 /v3/config 中出现过的模型 ID（含大量已下线与折扣变体，作为探测候选）
CANDIDATES: list[str] = [
    # codebuddy 内置
    "auto", "default", "default-model", "fast-model", "balanced-model",
    "primary-model", "deep-model",
    # 腾讯混元
    "hy4-preview", "hy4-preview-f", "hy3", "hy3-x",
    "hy4-night-discount-daytime-badge-202609", "hy3-free-trial-202607",
    "hunyuan-chat", "hunyuan-image-alpha",
    # 智谱 GLM
    "glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2", "glm-5.1",
    "glm-5v-turbo", "glm",
    "glm-52-night-discount-202607", "glm-52-night-discount-daytime-badge-202607",
    # DeepSeek
    "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4.1-flash",
    "deepseek-v4-pro-ioa", "deepseek-v4-pro-peak-202608",
    "deepseek-v4-flash-ioa", "deepseek-v4-flash-peak-202608",
    # Kimi
    "kimi-k3", "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7",
    "kimi-k2.6", "kimi-k2.5", "kimi",
    # MiniMax
    "minimax-m3", "minimax-m3.1-flash", "minimax-m2.7",
    # 小米 MiMo / 阶跃星辰 StepFun
    "mimo-v2.6-pro", "mimo-v2.6", "mimo-v2", "mimo-v1", "mimo-7b", "mimo-vl",
    "step-5-preview", "step-5", "step-3", "step-2", "step-r1", "step-1v",
    "step-2-16k",
    # OpenAI / Google / Anthropic
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash", "o4-mini",
    "claude-opus-4.8",
    "gpt-6-sol", "gpt-6-luna", "gpt-6-astra", "gpt-6.1-sol",
    "gemini-3.8-flash", "grok-4.7", "seedance-2.5", "step-5-preview",
    "gpt-image-2.5-sunburst",
    # 海外区域变体
    "deepseek-v4.1-flash-sg",
    # 补全 / 编辑类
    "space-bunny", "space-bunny-limited-discount-202610",
    "codewise-default-model-v2", "codewise-completions",
    "codewise-rewrite", "codewise-jump",
    "nes-gf", "nes-1.1", "nes-1.2", "completion-1.0",
]

IDE_HEADERS = {
    "User-Agent": "CodeBuddyIDE/4.10.33259736",
    "X-Product": "SaaS",
    "X-IDE-Type": "VSCode",
    "X-IDE-Name": "VSCode",
    "X-IDE-Version": "1.70.2",
    "X-Product-Version": "4.10.33259736",
}

# 已知会误判为"不可用"的错误码：
#   11150 reasoning effort 不支持 —— glm-5.1 未传 reasoning_effort 时的假失败
FALSE_NEGATIVE_CODES = {"11150"}


def collect_from_config(endpoint: str, session_file: str | None) -> list[str]:
    """从 /v3/config 的所有嵌套字段里收集模型 ID 候选。"""
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    if "workbuddy" in endpoint:
        from workbuddy_ai_client_demo import WorkBuddyAIClient as Client
    else:
        from codebuddy_client_demo import CodeBuddyClient as Client

    kwargs = {"session_file": session_file} if session_file else {}
    client = Client(endpoint, **kwargs)
    client.ensure_authenticated(open_browser=False)
    payload = client._unwrap(
        client._request(
            "GET", "/v3/config?repos=",
            headers={**IDE_HEADERS, **client.auth_headers()},
            timeout=30,
        )
    )

    tokens: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            # 只取看起来像模型 id 的短 token，跳过 URL / 长提示词
            if 2 < len(node) <= 40 and " " not in node and "/" not in node:
                tokens.add(node)

    walk(payload)
    return sorted(tokens)


async def probe_one(client: httpx.AsyncClient, model: str) -> tuple[str, str, str]:
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            },
        )
    except Exception as exc:  # 网络层异常
        return model, "ERR", str(exc)[:90]

    try:
        payload = response.json()
    except Exception:
        return model, "ERR", response.text[:90]

    if "choices" in payload:
        return model, "OK", ""

    detail = str(payload.get("detail", payload))
    code = "?"
    for candidate in FALSE_NEGATIVE_CODES:
        if f"code\\\":{candidate}" in detail or f"code:{candidate}" in detail:
            code = candidate
    # 上游把 JSON 塞在 message 字段里，抽取 code
    for token in ("11150", "11158", "11102", "11134", "11103", "11133"):
        if token in detail:
            code = token
            break
    return model, f"FAIL({code})", detail[:90]


async def probe_all(port: int, models: list[str], concurrency: int) -> int:
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", timeout=120
    ) as client:
        async def guarded(model: str) -> tuple[str, str, str]:
            async with semaphore:
                return await probe_one(client, model)

        results = await asyncio.gather(*(guarded(m) for m in models))

    ok = [r for r in results if r[1] == "OK"]
    failed = [r for r in results if r[1] != "OK"]
    for status, rows in (("可用", ok), ("不可用", failed)):
        print(f"\n=== {status} ({len(rows)}) ===")
        for model, code, detail in sorted(rows):
            line = f"  {code:11} {model}"
            if detail:
                line += f"  {detail[:70]}"
            print(line)
    return len(ok)


def main() -> int:
    parser = argparse.ArgumentParser(description="探测上游真实可用模型")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--endpoint", default="https://copilot.tencent.com")
    parser.add_argument("--session-file", default=None)
    parser.add_argument("--concurrency", type=int, default=3,
                        help="并发数，建议 ≤3，避免触发上游限频")
    parser.add_argument("--extra", default="", help="空格分隔的额外候选模型 ID")
    parser.add_argument("--from-config", action="store_true",
                        help="额外从 /v3/config 的所有嵌套字段收集候选")
    args = parser.parse_args()

    models = list(CANDIDATES)
    if args.extra:
        models += args.extra.split()
    if args.from_config:
        try:
            models += collect_from_config(args.endpoint, args.session_file)
        except Exception as exc:
            print(f"从云端配置收集候选失败（忽略）: {exc}", file=sys.stderr)

    # 去重并保持顺序
    seen: set[str] = set()
    unique = [m for m in models if not (m in seen or seen.add(m))]
    print(f"探测 {len(unique)} 个候选模型 @ {args.host}:{args.port} "
          f"(并发 {args.concurrency})")

    ok_count = asyncio.run(
        probe_all(args.port, unique, args.concurrency)
    )
    print(f"\n合计可用: {ok_count}/{len(unique)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
