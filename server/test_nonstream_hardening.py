"""非流式路径的两项硬化回归测试（issue: Claude Code 在已有项目里报
"API returned an empty or malformed response (HTTP 200) — body is an event stream
(the non-streaming request was answered with a stream), 0 stream events received"）。

1. 上游对 stream:false 回普通 JSON（content-type 非 text/event-stream）时，
   桥接要整体读入解析成标准 OpenAI 响应，而不是逐行等 data: 最后交出空响应。
2. 上游 HTTP 200 但一帧都没给（空事件流）时，换模型重试一次，且
   重试复用同一份会话/trace 身份（换模型不换会话），并去掉内部字段。
"""
import json
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy
from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import ProxyState, _looks_like_transient_reject, _pick_retry_model

GOOD_CHUNK = 'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}'
JSON_BODY = json.dumps({
    "id": "cmb-" + "a" * 32,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "来自非流式上游"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
}).encode()


class _Resp:
    def __init__(self, lines=(), status=200, body=b"", content_type="text/event-stream"):
        self._lines = list(lines)
        self.status_code = status
        self._body = body
        self.headers = httpx.Headers({"content-type": content_type})

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._body

    async def aclose(self):
        return None


def _state(tmp_path, platform="VSCode"):
    client = CodeBuddyClient("https://copilot.tencent.com", platform=platform,
                             session_file=tmp_path / "session.json")
    client.session = {
        "account": {"uid": "uid-1"},
        "auth": {"accessToken": "test-token", "expiresAt": 4102444800000},
    }
    return ProxyState(client=client, mock_dir=tmp_path, log_file=None,
                      enable_desensitize=False, enable_optimize_context=False,
                      verbose_llm=False, logger=None, rate_jitter=0.0)


class TestUpstreamNonSseFallback:
    def test_json_body_is_returned_instead_of_empty(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)
        seen = []

        async def fake_send(self, request, *, stream=False, **kw):
            seen.append(dict(request.headers))
            return _Resp(body=JSON_BODY, content_type="application/json")

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        with TestClient(codebuddy_proxy.app) as client:
            r = client.post("/v1/chat/completions", content=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }), headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        data = r.json()
        assert data["choices"][0]["message"]["content"] == "来自非流式上游"
        assert data["usage"]["total_tokens"] == 13
        # 内部判定字段不得外泄
        assert "_upstream_frames" not in data
        # 只打了一次上游（JSON 响应是有效答复，不该触发换模型重试）
        assert len(seen) == 1

    def test_unparseable_json_body_raises_502(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)

        async def fake_send(self, request, *, stream=False, **kw):
            return _Resp(body=b"<html>nope</html>", content_type="text/html")

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        with TestClient(codebuddy_proxy.app) as client:
            r = client.post("/v1/chat/completions", content=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }), headers={"Content-Type": "application/json"})
        assert r.status_code == 502
        assert r.json()["detail"]["error"]["type"] == "upstream_error"


class TestTransientRejectRetry:
    def test_empty_stream_retries_once_with_other_model(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)
        bodies = []

        async def fake_send(self, request, *, stream=False, **kw):
            bodies.append(json.loads(request.content.decode()))
            if len(bodies) == 1:
                return _Resp(lines=["data: [DONE]"])  # 200 但零帧
            return _Resp(lines=[GOOD_CHUNK, "data: [DONE]"])

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        with TestClient(codebuddy_proxy.app) as client:
            r = client.post("/v1/chat/completions", content=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }), headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "ok"
        assert len(bodies) == 2
        assert bodies[0]["model"] != bodies[1]["model"], "重试必须换模型"

    def test_retry_reuses_conversation_and_trace(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)
        reqs = []

        async def fake_send(self, request, *, stream=False, **kw):
            h = {k.lower(): v for k, v in request.headers.items()}
            reqs.append((h, json.loads(request.content.decode())))
            if len(reqs) == 1:
                return _Resp(lines=["data: [DONE]"])
            return _Resp(lines=[GOOD_CHUNK, "data: [DONE]"])

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        with TestClient(codebuddy_proxy.app) as client:
            client.post("/v1/chat/completions", content=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }), headers={"Content-Type": "application/json"})
        h1, b1 = reqs[0]
        h2, b2 = reqs[1]
        # 同一次 user send 内的重试必须复用同一份会话/trace 身份，
        # 否则上游看到的是多个并发会话而非一次对话的一次重试
        for key in ("x-conversation-id", "x-conversation-request-id",
                    "x-b3-traceid", "traceparent", "x-trace-id"):
            assert h1[key] == h2[key], key
        # 换模型时 x-model-id 要跟着换（仅国内版有该头）
        assert h1["x-model-id"] == b1["model"]
        assert h2["x-model-id"] == b2["model"]

    def test_no_retry_when_upstream_answered(self, tmp_path, monkeypatch):
        state = _state(tmp_path)
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)
        calls = []

        async def fake_send(self, request, *, stream=False, **kw):
            calls.append(1)
            return _Resp(lines=[GOOD_CHUNK, "data: [DONE]"])

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        with TestClient(codebuddy_proxy.app) as client:
            client.post("/v1/chat/completions", content=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }), headers={"Content-Type": "application/json"})
        assert len(calls) == 1


class TestPredicates:
    def test_transient_detect_only_on_zero_frames(self):
        assert _looks_like_transient_reject({"_upstream_frames": 0}) is True
        assert _looks_like_transient_reject({"_upstream_frames": 1}) is False
        assert _looks_like_transient_reject({}) is True  # JSON 兜底分支不设该字段

    def test_pick_retry_model_differs_and_skips_completion_models(self, tmp_path):
        state = _state(tmp_path)
        alt = _pick_retry_model(state, "hy3")
        assert alt and alt != "hy3"
        assert not alt.startswith(("codewise", "nes-gf", "hunyuan-image"))

    def test_pick_retry_model_overseas(self, tmp_path):
        state = _state(tmp_path, platform="workbuddy-ai")
        assert _pick_retry_model(state, "fast-model") not in (None, "fast-model")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])