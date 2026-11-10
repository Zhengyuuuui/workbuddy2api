"""Anthropic 转换器的 thinking block 测试。

上游（deepseek 风格）在 delta.reasoning_content 里流思考过程。此前转换器整体
丢弃它，用户为 reasoning token 付费却看不到任何输出。现在转成 Anthropic 原生
thinking 块转发给客户端。
"""

import json

import pytest

from anthropic_adapter import AnthropicStreamConverter


def _events(converter, chunks):
    out = []
    for chunk in chunks:
        out += converter.feed_chunk(chunk)
    out += converter.finish()
    return out


def _check_block_order(events):
    """Anthropic 要求块按顺序打开/关闭，不能交错。"""
    open_idx = set()
    for name, data in events:
        if name == "content_block_start":
            assert data["index"] not in open_idx, f"块 {data['index']} 重复打开"
            open_idx.add(data["index"])
        elif name == "content_block_stop":
            open_idx.discard(data["index"])
    assert not open_idx, f"流结束后仍有未关闭的块: {open_idx}"


def test_reasoning_becomes_thinking_block():
    c = AnthropicStreamConverter("claude-x")
    events = _events(c, [
        {"choices": [{"delta": {"reasoning_content": "先想一下", "content": ""}}]},
        {"choices": [{"delta": {"reasoning_content": "再想一下", "content": ""}}]},
        {"choices": [{"delta": {"content": "最终答案"}}]},
    ])

    starts = [d for n, d in events if n == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "thinking"
    assert starts[1]["content_block"]["type"] == "text"

    thinking_deltas = [
        d["delta"]["thinking"] for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "thinking_delta"
    ]
    assert thinking_deltas == ["先想一下", "再想一下"]

    text_deltas = [
        d["delta"]["text"] for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "text_delta"
    ]
    assert text_deltas == ["最终答案"]

    _check_block_order(events)


def test_thinking_block_closed_before_text_opens():
    """ thinking 块必须在 text 块打开前关闭，否则客户端报块交错。"""
    c = AnthropicStreamConverter("claude-x")
    events = _events(c, [
        {"choices": [{"delta": {"reasoning_content": "想", "content": ""}}]},
        {"choices": [{"delta": {"content": "答"}}]},
    ])

    seq = [(n, d.get("index")) for n, d in events
           if n in ("content_block_start", "content_block_stop")]
    # thinking start(0) -> thinking stop(0) -> text start(1) -> text stop(1)
    assert seq == [
        ("content_block_start", 0),
        ("content_block_stop", 0),
        ("content_block_start", 1),
        ("content_block_stop", 1),
    ], seq


def test_reasoning_only_stream():
    """只有思考没有正文（max_tokens 被推理吃满）也要能正常收尾。"""
    c = AnthropicStreamConverter("claude-x")
    events = _events(c, [
        {"choices": [{"delta": {"reasoning_content": "只想不说", "content": ""}}]},
    ])
    names = [n for n, _ in events]
    assert "message_stop" in names
    assert c.thinking == "只想不说"
    assert c.text == ""
    _check_block_order(events)


def test_no_reasoning_unchanged():
    """没有 reasoning_content 时行为与之前一致（单 text 块）。"""
    c = AnthropicStreamConverter("claude-x")
    events = _events(c, [
        {"choices": [{"delta": {"content": "hello"}}]},
    ])
    starts = [d for n, d in events if n == "content_block_start"]
    assert len(starts) == 1
    assert starts[0]["content_block"]["type"] == "text"
    assert c.thinking == ""
    _check_block_order(events)


def test_thinking_then_tool_use():
    """思考后接工具调用，块顺序也要合法。"""
    c = AnthropicStreamConverter("claude-x")
    events = _events(c, [
        {"choices": [{"delta": {"reasoning_content": "想", "content": ""}}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"p":"a"}'},
        }]}}]},
    ])
    _check_block_order(events)
    names = [n for n, _ in events]
    assert "message_stop" in names


# ---------------------------------------------------------------------------
# 请求方向：Anthropic → Chat 必须透传思考强度参数
# ---------------------------------------------------------------------------

def test_request_passthrough_reasoning_effort():
    """Anthropic 请求里的 reasoning_effort 必须透传到上游。

    此前透传列表只有 max_tokens/temperature/top_p/stop/top_k，reasoning_effort
    被丢掉后上游不进入思考模式，reasoning_content 全为空——客户端的思考配置
    静默失效，且很难从表现上发现。
    """
    from anthropic_adapter import anthropic_request_to_chat

    chat = anthropic_request_to_chat({
        "model": "deepseek-v4.1-flash",
        "max_tokens": 1024,
        "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert chat.get("reasoning_effort") == "high"


def test_request_passthrough_thinking_block():
    """Anthropic 原生 thinking 参数也要透传。"""
    from anthropic_adapter import anthropic_request_to_chat

    chat = anthropic_request_to_chat({
        "model": "deepseek-v4.1-flash",
        "max_tokens": 1024,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert chat.get("thinking") == {"type": "enabled", "budget_tokens": 2048}


def test_request_without_reasoning_params_unchanged():
    """没发思考参数时不应凭空注入。"""
    from anthropic_adapter import anthropic_request_to_chat

    chat = anthropic_request_to_chat({
        "model": "deepseek-v4.1-flash",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert "reasoning_effort" not in chat
    assert "thinking" not in chat
