"""流式看门狗测试：上游永不结束时必须主动掐断，不能占满事件循环。

背景：线上出现过一次上游对 2 字符 prompt 发出 1560+ 个空 chunk 且不给 [DONE]，
httpx 的 read 超时是「两次读之间」的间隔，拦不住持续吐字节的上游，
导致单 worker 的 uvicorn 事件循环被占满，/health 都无响应。
"""

import asyncio
import json

import pytest

import codebuddy_proxy as proxy
from codebuddy_proxy import (
    STREAM_MAX_CHUNKS,
    STREAM_MAX_DURATION_S,
    STREAM_MAX_EMPTY_CHUNKS,
    StreamWatchdogError,
)


class _FakeResponse:
    """模拟一个永不结束的 SSE 响应。"""

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


def _endless_empty_chunks(n):
    """n 个空 delta chunk（没有 content，也没有 [DONE]）。"""
    return [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        })
        for _ in range(n)
    ]


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """把 httpx 客户端和全局 state 换成假的，隔离测试。"""
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
    # 绕过认证与限速
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


def test_endless_empty_chunks_trips_watchdog(patched):
    """上游发远超上限的空 chunk：流必须在有限时间内终止，不能挂起。

    注意：看门狗异常会被 stream_upstream 捕获后按协议转成结构化错误帧发给
    客户端（而非向上抛），所以这里断言「会结束」+「有错误帧」。
    """
    patched.sent["response"] = _FakeResponse(
        _endless_empty_chunks(STREAM_MAX_EMPTY_CHUNKS + 50)
    )

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "hy3", "messages": []}, "openai", None,
        )
        return await _drain(agen)

    # 关键：wait_for 超时即视为「挂起未修复」
    chunks = asyncio.run(asyncio.wait_for(run(), timeout=30))
    joined = b"".join(chunks)
    assert b"stalled" in joined
    assert b"[DONE]" not in joined


def test_chunk_cap_trips_watchdog(patched):
    """即使每个 chunk 都带内容（避开空 chunk 检测），chunk 数上限也要生效。

    已有内容时看门狗按设计优雅收尾（补发 [DONE]）而非报错，所以这里只断言
    「流会在有限时间内结束」——这正是修复挂起的关键性质。
    """
    lines = [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        })
        for _ in range(STREAM_MAX_CHUNKS + 50)
    ]
    patched.sent["response"] = _FakeResponse(lines)

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "hy3", "messages": []}, "openai", None,
        )
        return await _drain(agen)

    # wait_for 超时即视为「仍然挂起」
    chunks = asyncio.run(asyncio.wait_for(run(), timeout=120))
    joined = b"".join(chunks)
    # 不应吐出全部 20050 帧（说明确实被掐断了）
    assert joined.count(b"chat.completion.chunk") < STREAM_MAX_CHUNKS + 50
    assert joined.count(b"chat.completion.chunk") <= STREAM_MAX_CHUNKS + 1


def test_duration_cap_trips_watchdog(patched, monkeypatch):
    """总时长上限生效：伪造一个比上限更久的流。"""
    monkeypatch.setattr(proxy, "STREAM_MAX_DURATION_S", 0.0)
    lines = [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}],
        }),
        "data: [DONE]",
    ]
    patched.sent["response"] = _FakeResponse(lines)

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "hy3", "messages": []}, "openai", None,
        )
        return await _drain(agen)

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=30))
    joined = b"".join(chunks)
    # 第一帧都没处理就被掐断，无内容可优雅收尾，只能给错误帧
    assert joined.count(b"chat.completion.chunk") == 0
    assert b"stalled" in joined
    assert b"[DONE]" not in joined


def test_normal_stream_completes(patched):
    """正常流（有内容 + [DONE]）不能被看门狗误杀。"""
    lines = [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
        }),
        "data: [DONE]",
    ]
    patched.sent["response"] = _FakeResponse(lines)

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "hy3", "messages": []}, "openai", None,
        )
        return await _drain(agen)

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=30))
    joined = b"".join(chunks)
    assert b"[DONE]" in joined
    assert b"hi" in joined


def test_empty_chunk_streak_resets_on_content(patched):
    """空 chunk 后接有内容 chunk，计数应归零，不触发看门狗。"""
    lines = _endless_empty_chunks(STREAM_MAX_EMPTY_CHUNKS - 5)
    lines += [
        'data: ' + json.dumps({
            "id": "cmb-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
        }),
        "data: [DONE]",
    ]
    patched.sent["response"] = _FakeResponse(lines)

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "hy3", "messages": []}, "openai", None,
        )
        return await _drain(agen)

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert b"[DONE]" in b"".join(chunks)


def _reasoning_chunks(n, with_final_content=True):
    """模拟 reasoning_effort=high 的上游：先只流 reasoning_content，再给正文。"""
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


def test_long_reasoning_phase_not_treated_as_stall(patched):
    """回归：reasoning_effort=high 时上游先只流 reasoning_content。

    实测 reasoning 阶段可达 500+ chunk，只数 content 会把请求误判为上游卡死。
    此前这条路径被看门狗掐断，用户侧表现为「对话一半卡住/失败」。
    """
    patched.sent["response"] = _FakeResponse(
        _reasoning_chunks(STREAM_MAX_EMPTY_CHUNKS + 200)
    )

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "deepseek-v4.1-flash", "messages": [],
                 "reasoning_effort": "high"}, "openai", None,
        )
        return await _drain(agen)

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=60))
    joined = b"".join(chunks)
    assert b"stalled" not in joined, "长思考阶段被误判为上游卡死"
    assert b"[DONE]" in joined


def test_delta_has_payload_counts_reasoning():
    """_delta_has_payload 必须把 reasoning_content 视为有效载荷。"""
    from codebuddy_proxy import _delta_has_payload

    assert _delta_has_payload({"choices": [{"delta": {"content": "x"}}]})
    assert _delta_has_payload({"choices": [{"delta": {"reasoning_content": "想"}}]})
    assert _delta_has_payload({"choices": [{"delta": {"reasoning": "想"}}]})
    assert _delta_has_payload({"choices": [{"delta": {"tool_calls": [{"id": "1"}]}}]})
    assert _delta_has_payload({"usage": {"total_tokens": 1}})
    assert not _delta_has_payload({"choices": [{"delta": {"content": ""}}]})
    assert not _delta_has_payload({"choices": [{"delta": {}}]})
    assert not _delta_has_payload({"choices": []})
    assert not _delta_has_payload({})


def test_truly_empty_stream_still_trips(patched):
    """真正全空的流（无 content 也无 reasoning）仍必须被看门狗掐断。"""
    patched.sent["response"] = _FakeResponse(
        _endless_empty_chunks(STREAM_MAX_EMPTY_CHUNKS + 50)
    )

    async def run():
        agen = proxy.stream_upstream(
            "https://copilot.tencent.com/v2/chat/completions",
            {}, {"model": "hy3", "messages": []}, "openai", None,
        )
        return await _drain(agen)

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert b"stalled" in b"".join(chunks)
