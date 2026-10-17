# -*- coding: utf-8 -*-
"""临时冒烟：验证本地三处修复在 rebase 到上游 c0dc27e 之后仍然生效。

1. /v1/responses + stream:false 必须走非流式（返回 JSON response 对象），
   不能被适配器改写的 stream:true 带偏成 SSE。
2. responses 流结束事件的 item_id 与流内的 item_id 一致（走 finish()）。
3. 模型 id 归一化：/v1/models 里带倍率后缀的 id 回传时必须剥回真 id。

注：8787/8788 已被占用，本用例走 TestClient 进程内调用，不占端口。
"""
import json
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy
from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import ProxyState
from responses_adapter import (
    ResponsesStreamConverter,
    _convert_tools_for_chat,
    responses_request_to_chat,
)

GOOD = 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}'


class _Resp:
    status_code = 200
    headers = httpx.Headers({"content-type": "text/event-stream"})

    def __init__(self, lines=()):
        self._lines = list(lines)

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    async def aclose(self):
        return None


def _state(tmp_path):
    client = CodeBuddyClient("https://copilot.tencent.com", platform="VSCode",
                             session_file=tmp_path / "session.json")
    client.session = {
        "account": {"uid": "uid-1"},
        "auth": {"accessToken": "test-token", "expiresAt": 4102444800000},
    }
    return ProxyState(client=client, mock_dir=tmp_path, log_file=None,
                      enable_desensitize=False, enable_optimize_context=False,
                      verbose_llm=False, logger=None, rate_jitter=0.0)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    codebuddy_proxy.proxy_state = _state(tmp_path)
    captured = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def build_request(self, method, url, **kw):
            return httpx.Request(method, url, **kw)

        async def send(self, request, stream=False):
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return _Resp([GOOD, "data: [DONE]"])

    def _factory(*a, **kw):
        return _Client()

    monkeypatch.setattr(codebuddy_proxy.httpx, "AsyncClient", _factory)
    return captured, TestClient(codebuddy_proxy.app)


# --- 1) 归一化：纯 id 不变（后端用的就是这个） ------------------------------

@pytest.mark.parametrize("mid", ["fast-model", "hy4-preview-f", "gpt-6-sol",
                                 "default-model", "auto", "auto2"])
def test_pure_id_unchanged(mid):
    assert codebuddy_proxy._normalize_model_id(mid) == mid


# --- 2) 归一化：带倍率后缀 / 上下文标记必须剥回真 id ------------------------

@pytest.mark.parametrize("raw,expected", [
    ("fast-model (x0.34)", "fast-model"),
    ("hy4-preview-f (x0.00)", "hy4-preview-f"),
    ("gpt-6-sol (x1.33)", "gpt-6-sol"),
    ("hy4-preview-f[1M]", "hy4-preview-f"),
    ("hy4-preview-f[1M] (x0.00)", "hy4-preview-f"),
    ("hy3[1M]", "hy3"),
    ("  kimi-k3 (x1.62)  ", "kimi-k3"),
    # 已是真 id（无后缀）时不得误伤：剥完为空则原样返回
    ("(x0.34)", "(x0.34)"),
])
def test_suffix_stripped(raw, expected):
    assert codebuddy_proxy._normalize_model_id(raw) == expected


def test_non_string_passthrough():
    assert codebuddy_proxy._normalize_model_id(None) is None
    assert codebuddy_proxy._normalize_model_id(123) == 123


# --- 3) 端到端：cc-switch 回传的带后缀 id，实际发到上游的 body 是真 id -----

@pytest.mark.parametrize("sent,expected_upstream", [
    ("fast-model (x0.34)", "fast-model"),
    ("hy4-preview-f[1M]", "hy4-preview-f"),
    ("fast-model", "fast-model"),
])
def test_openai_forward_strips_suffix(client, sent, expected_upstream):
    captured, tc = client
    with tc.stream("POST", "/v1/chat/completions", json={
            "model": sent,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
    }) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass
    assert captured["body"]["model"] == expected_upstream


# --- 4) 部署形态：/v1/models 的 id 分红蓝队，均能被自己归一化 --------------

def test_models_id_vs_normalize_roundtrip():
    """所有 /v1/models 返回的 id，归一化后必须仍是合法上游 id（非空、无后缀）。"""
    decorated = ["fast-model (x0.34)", "hy4-preview-f (x0.00)",
                 "gpt-6-sol (x1.33)", "default-model"]
    for mid in decorated:
        cleaned = codebuddy_proxy._normalize_model_id(mid)
        assert cleaned
        assert "(x" not in cleaned
        assert "[" not in cleaned


# --- 5) 回归锁定：/v1/responses + stream:false 走非流式 --------------------

def test_responses_stream_false_is_json(client):
    captured, tc = client
    body = {
        "model": "fast-model (x0.34)",
        "stream": False,
        "input": [{"role": "user", "content": "hi"}],
    }
    r = tc.post("/v1/responses", json=body)
    assert r.status_code == 200
    payload = r.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    # 归一化发生在转发前，上游收到的必须是真 id
    assert captured["body"]["model"] == "fast-model"


# --- 6) 回归锁定：Responses 事件必须满足官方 schema ------------------------

def _all_events() -> list[tuple[str, dict]]:
    """文本 + 工具调用两类路径的事件全集。"""
    conv = ResponsesStreamConverter("deepseek-v4.1-flash")
    evs = conv.feed_chunk({
        "choices": [{"delta": {"content": "hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    })
    evs += conv.feed_chunk({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "fc1",
         "function": {"name": "shell", "arguments": "{}"}}]}}]})
    evs += conv.finish()
    return evs


def test_every_event_has_sequence_number():
    """Codex 的 Rust 解析器要求每个事件都带 sequence_number；Claude Code
    容错缺失，所以该缺陷只在 Codex 路径暴露（stream closed before completed）。"""
    for name, payload in _all_events():
        assert "sequence_number" in payload, f"{name} 缺 sequence_number"


def test_sequence_numbers_are_contiguous_from_zero():
    seqs = [p["sequence_number"] for _, p in _all_events()]
    assert seqs == list(range(len(seqs))), f"sequence_number 不连续: {seqs}"


def test_response_object_is_responses_api_not_realtime():
    """object 必须是 "response"。早期版本误用 Realtime API 的
    "realtime.response"，在 Responses API 里是非法枚举值。"""
    for name, payload in _all_events():
        resp = payload.get("response")
        if resp is not None:
            assert resp["object"] == "response", f"{name}: object={resp['object']!r}"


def test_completed_carries_model_and_output():
    conv = ResponsesStreamConverter("deepseek-v4.1-flash")
    conv.feed_chunk({"choices": [{"delta": {"content": "hi"}}]})
    events = dict(conv.finish())
    resp = events["response.completed"]["response"]
    assert resp["model"] == "deepseek-v4.1-flash"
    assert resp["status"] == "completed"
    assert resp["output"], "completed.response.output 不应为空"
    # usage.input_tokens_details 必须有 cache_write_tokens（官方必填）
    details = resp["usage"]["input_tokens_details"]
    assert "cache_write_tokens" in details


def test_content_part_type_is_output_text():
    """part.type 官方取值为 output_text，不是 text。"""
    conv = ResponsesStreamConverter("m")
    evs = conv.feed_chunk({"choices": [{"delta": {"content": "hi"}}]})
    added = dict(evs)["response.content_part.added"]
    assert added["part"]["type"] == "output_text"
    # content_part.* 需要 item_id / output_index / content_index
    for field in ("item_id", "output_index", "content_index"):
        assert field in added, f"content_part.added 缺 {field}"


def test_output_item_added_has_output_index_and_status():
    conv = ResponsesStreamConverter("m")
    evs = conv.feed_chunk({"choices": [{"delta": {"content": "hi"}}]})
    added = dict(evs)["response.output_item.added"]
    assert added["output_index"] == 0
    assert added["item"]["status"] == "in_progress"


def test_function_call_output_index_follows_index():
    """多工具调用时 output_index 必须跟随 call.index，不能硬编码 0。"""
    conv = ResponsesStreamConverter("m")
    evs = conv.feed_chunk({"choices": [{"delta": {"tool_calls": [
        {"index": 2, "id": "fc2", "function": {"name": "b", "arguments": "{}"}}]}}]})
    delta = dict(evs)["response.function_call_arguments.delta"]
    assert delta["output_index"] == 2


# --- 7) 回归锁定：Codex 的 Responses 工具必须过滤掉 chat 不认的类型 --------

def test_namespace_tools_are_dropped():
    """Codex (wire_api=responses) 会发 namespace 类型工具，它没有 parameters。
    转成空 schema 的 function 会让上游以 code 11129
    'invalid function call parameters' 拒绝整个请求 Codex 侧表现为
    stream closed before response.completed。"""
    tools = [
        {"type": "function", "name": "shell",
         "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        {"type": "namespace", "name": "mcp__node_repl",
         "description": "...", "tools": [{"name": "js"}]},
    ]
    out = _convert_tools_for_chat(tools)
    assert [t["function"]["name"] for t in out] == ["shell"]


def test_web_search_tool_is_dropped():
    tools = [
        {"type": "function", "name": "shell",
         "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        {"type": "web_search", "external_web_access": True},
    ]
    out = _convert_tools_for_chat(tools)
    assert [t["function"]["name"] for t in out] == ["shell"]


def test_empty_parameters_is_dropped():
    """空 properties 的 function 工具同样会被上游拒。"""
    tools = [{"type": "function", "name": "noop",
              "parameters": {"type": "object", "properties": {}}}]
    assert _convert_tools_for_chat(tools) == []


def test_chat_format_tool_is_validated_not_passed_through():
    """已是 chat 格式的工具也要校验 parameters，不能再原样透传。"""
    tools = [{"type": "function",
              "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}}}]
    assert _convert_tools_for_chat(tools) == []


def test_all_tools_filtered_out_yields_empty_list():
    tools = [{"type": "namespace", "name": "ns"}, {"type": "web_search"}]
    assert _convert_tools_for_chat(tools) == []


def test_real_codex_request_converts_to_valid_chat_body():
    """端到端：Codex 真实请求体经 responses_request_to_chat 后，
    所有 tools 都必须是带非空 properties 的 function。

    只取带 tools 的 dump（目录里可能混有自测用的无 tools 请求）。
    """
    dump = pathlib.Path("logs/req-dump")
    samples = sorted(dump.glob("req-*.json")) if dump.exists() else []
    bodies = []
    for f in samples:
        try:
            b = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if b.get("tools"):
            bodies.append(b)
    if not bodies:
        pytest.skip("无 Codex 请求 dump（CODEBUDDY_DUMP_REQ=1 时才会生成）")
    body = bodies[0]
    chat = responses_request_to_chat(body)
    tools = chat.get("tools") or []
    assert tools, "过滤后 tools 不应为空（Codex 有 16 个工具）"
    for t in tools:
        assert t["type"] == "function"
        assert t["function"]["parameters"].get("properties"), t["function"]["name"]
    # 16 个原始工具里 5 个非 function，应被过滤
    assert len(tools) < len(body["tools"]), "非 function 工具应被过滤掉"


