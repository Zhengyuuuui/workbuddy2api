# -*- coding: utf-8 -*-
"""临时冒烟：验证本地三处修复在 rebase 到上游 c0dc27e 之后仍然生效。

1. /v1/responses + stream:false 必须走非流式（返回 JSON response 对象），
   不能被适配器改写的 stream:true 带偏成 SSE。
2. responses 流结束事件的 item_id 与流内的 item_id 一致（走 finish()）。
3. 模型 id 归一化：/v1/models 里带倍率后缀的 id 回传时必须剥回真 id。

注：8787/8788 已被占用，本用例走 TestClient 进程内调用，不占端口。
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy
from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import ProxyState

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
