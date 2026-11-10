"""流式防护测试。

设计（参考 workbuddy2api-hub 的 upstream_timeouts）：
  - 不设总时长上限、不设 chunk 数上限、不设连续空 chunk 上限
  - 数据在流就算健康；真正静默由 httpx read 超时（= idle）兜底
  - 只保留累计缓冲字节上限作为内存护栏

曾经做错的方向：加过 600s 总时长 + 500 连续空 chunk 上限，把
reasoning_effort=high 的长思考阶段误判为上游卡死并掐断。这里的用例用于锁住
「不再犯」。
"""

import asyncio
import json

import pytest

import codebuddy_proxy as proxy
from codebuddy_proxy import (
    STREAM_MAX_BUFFERED_BYTES,
    StreamWatchdogError,
)


class _FakeResponse:
    """模拟一个 SSE 响应。"""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.headers = {"content-type": "text/event-stream"}
        self.closed = False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    async def aclose(self):
        self.closed = True


def _content_chunks(n, text="x"):
    return [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        })
        for _ in range(n)
    ]


def _reasoning_chunks(n, with_final_content=True):
    """模拟 reasoning_effort=high：先只流 reasoning_content，再给正文。"""
    lines = [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {
                "reasoning_content": f"思考{i}...", "content": "",
            }, "finish_reason": None}],
        })
        for i in range(n)
    ]
    if with_final_content:
        lines.append('data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "最终答案"},
                         "finish_reason": None}],
        }))
    lines.append("data: [DONE]")
    return lines


def _empty_chunks(n):
    return [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        })
        for _ in range(n)
    ]


@pytest.fixture
def patched(monkeypatch):
    import httpx

    class _FakeClient:
        sent = {}

        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def build_request(self, method, url, **kw):
            return object()

        async def send(self, request, **kw):
            return _FakeClient.sent["response"]

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(proxy, "get_state", lambda: _FakeState())
    return _FakeClient


class _FakeState:
    verbose_llm = False
    logger = None
    device_token = None
    enable_desensitize = False

    class client:
        endpoint = "https://copilot.tencent.com"
        platform = "VSCode"

        @staticmethod
        def auth_headers():
            return {}

    class breaker:
        @staticmethod
        def note_5xx(*a, **kw):
            pass

        @staticmethod
        def note_success(*a, **kw):
            pass

        @staticmethod
        def check(*a, **kw):
            return False, 0.0

    def ensure_auth(self):
        pass

    def write_log(self, *a, **kw):
        pass

    def peek_previous_response_id(self, conv_id):
        return None

    def record_response_id(self, *a, **kw):
        pass

    def record_burn(self, *a, **kw):
        pass

    def get_conversation_id(self, body):
        return ""


async def _drain(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


def _run(patched, lines, protocol="openai", body=None):
    patched.sent["response"] = _FakeResponse(lines)

    async def go():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, body or {"model": "deepseek-v4.1-flash", "messages": []},
            protocol, None,
        )
        return await _drain(agen)

    return asyncio.run(asyncio.wait_for(go(), timeout=60))


# ---------------------------------------------------------------------------
# 回归：长思考阶段不能被掐断
# ---------------------------------------------------------------------------

def test_very_long_reasoning_phase_not_killed(patched):
    """远超旧阈值（500）的纯 reasoning 阶段必须放行。"""
    chunks = _run(patched, _reasoning_chunks(3000),
                  body={"model": "deepseek-v4.1-flash", "messages": [],
                        "reasoning_effort": "high"})
    joined = b"".join(chunks)
    assert b"stalled" not in joined
    assert b"[DONE]" in joined


def test_long_content_stream_not_killed(patched):
    """长正文流（远超旧的 20000 chunk 上限）必须放行。"""
    chunks = _run(patched, _content_chunks(30000))
    joined = b"".join(chunks)
    assert b"stalled" not in joined
    assert b"[DONE]" in joined


def test_long_empty_stream_not_killed(patched):
    """持续空 chunk 也不杀——数据在流即健康，真正静默由 idle 超时兜底。"""
    chunks = _run(patched, _empty_chunks(5000))
    joined = b"".join(chunks)
    assert b"stalled" not in joined
    assert b"[DONE]" in joined


def test_normal_stream_completes(patched):
    lines = _content_chunks(3, "hi") + ["data: [DONE]"]
    joined = b"".join(_run(patched, lines))
    assert b"hi" in joined
    assert b"[DONE]" in joined


# ---------------------------------------------------------------------------
# 新增：流内 error 必须被识别并上报，不能静默吞掉
# ---------------------------------------------------------------------------

def test_in_stream_error_is_surfaced_openai(patched):
    """上游在 SSE 流内返回错误对象时，必须转成错误帧而不是空回复。"""
    lines = [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "PARTIAL"},
                         "finish_reason": None}],
        }),
        'data: ' + json.dumps({
            "error": {"message": '{"code":11155,"msg":"the reasoning content from '
                                 'the previous turn must be passed back in thinking mode"}',
                      "type": "invalid_request_error"}
        }),
        "data: [DONE]",
    ]
    joined = b"".join(_run(patched, lines))
    assert b"11155" in joined
    assert b"reasoning content" in joined
    assert b"PARTIAL" in joined


def test_in_stream_error_anthropic_uses_event_frame(patched):
    """Anthropic 协议必须用 `event: error` 帧，裸 data: 会让客户端 422。"""
    lines = [
        'data: ' + json.dumps({"error": {"message": "upstream said no"}}),
    ]
    joined = b"".join(_run(patched, lines, protocol="anthropic"))
    assert joined.startswith(b"event: error\n")
    assert b"upstream said no" in joined


def test_in_stream_error_responses_uses_event_frame(patched):
    lines = [
        'data: ' + json.dumps({"error": {"message": "upstream said no"}}),
    ]
    joined = b"".join(_run(patched, lines, protocol="responses"))
    assert joined.startswith(b"event: error\n")
    assert b"upstream said no" in joined


def test_in_stream_error_plain_message(patched):
    """message 不是 JSON 字符串时也要能提取。"""
    lines = ['data: ' + json.dumps({"error": {"message": "simple failure"}})]
    joined = b"".join(_run(patched, lines))
    assert b"simple failure" in joined


# ---------------------------------------------------------------------------
# 内存护栏仍然生效
# ---------------------------------------------------------------------------

def test_buffered_bytes_guard_still_trips(patched, monkeypatch):
    """累计缓冲超限仍要掐断——这是资源保护，不影响正确性。

    注意：已有部分内容时按设计优雅收尾（补发 [DONE]）而非报错帧，
    所以断言「提前终止」而不是断言错误文本。
    """
    monkeypatch.setattr(proxy, "STREAM_MAX_BUFFERED_BYTES", 1024)
    # 每行约 136 字节，100 行共 13612 字节
    lines = _content_chunks(100) + ["data: [DONE]"]
    out = _run(patched, lines)
    joined = b"".join(out)
    # 必须远早于 100 帧就停
    assert joined.count(b"chat.completion.chunk") < 20
    assert b"[DONE]" in joined


def test_buffered_bytes_guard_allows_normal_stream(patched):
    """正常流不能撞到内存护栏。"""
    joined = b"".join(_run(patched, _content_chunks(50) + ["data: [DONE]"]))
    assert joined.count(b"chat.completion.chunk") == 50
    assert b"[DONE]" in joined
