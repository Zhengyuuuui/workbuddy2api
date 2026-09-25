"""官方客户端指纹伪装单元测试

测试覆盖：
1. official_ide_headers() 静态指纹字段与 x-domain 平台区分
2. _hex32 / _hex16 / UUID / 毫秒时间戳格式
3. ProxyState.get_conversation_id() 会话指纹复用
4. forward_chat 端到端 headers 构造（TestClient + httpx mock，不请求上游）
"""

import gzip
import json
import pathlib
import re
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy
from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import (
    INTL_IDE_VERSION,
    OFFICIAL_IDE_VERSION,
    ProxyState,
    _hex16,
    _hex32,
    gzip_body_if_large,
    official_ide_headers,
)

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MS_TS = re.compile(r"^\d{13}$")

STATIC_REQUIRED_HEADERS = [
    "user-agent",
    "accept",
    "accept-language",
    "sec-fetch-mode",
    "x-requested-with",
    "x-ide-name",
    "x-ide-type",
    "x-ide-version",
    "x-product",
    "x-product-code",
    "x-product-version",
    "x-env-id",
    "x-domain",
    "x-agent-intent",
    "x-model-id",
    "x-conversation-id",
]


class TestOfficialIdeHeaders:
    def test_contains_all_static_required_headers(self):
        headers = official_ide_headers("gpt-5.5", "a" * 32, "VSCode")
        for key in STATIC_REQUIRED_HEADERS:
            assert key in headers, f"missing header: {key}"

    def test_static_values(self):
        headers = official_ide_headers("gpt-5.5", "a" * 32, "VSCode")
        assert headers["user-agent"] == f"CodeBuddyIDE/{OFFICIAL_IDE_VERSION} CodeBuddy/{OFFICIAL_IDE_VERSION}"
        assert headers["x-ide-name"] == "CodeBuddyIDE"
        assert headers["x-ide-type"] == "CodeBuddyIDE"
        assert headers["x-ide-version"] == OFFICIAL_IDE_VERSION
        assert headers["x-product"] == "SaaS"
        assert headers["x-product-code"] == "codebuddy"
        assert headers["x-product-version"] == OFFICIAL_IDE_VERSION
        assert headers["x-env-id"] == "production"
        assert headers["x-agent-intent"] == "craft"
        assert headers["x-model-id"] == "gpt-5.5"
        assert headers["x-conversation-id"] == "a" * 32
        assert headers["accept"] == "*/*"
        assert headers["accept-language"] == "*"
        assert headers["sec-fetch-mode"] == "cors"
        assert headers["x-requested-with"] == "XMLHttpRequest"

    def test_domain_by_platform(self):
        assert official_ide_headers("m", "c", "workbuddy-ai")["x-domain"] == "www.workbuddy.ai"
        assert official_ide_headers("m", "c", "VSCode")["x-domain"] == "www.codebuddy.cn"
        assert official_ide_headers("m", "c", "codebuddy")["x-domain"] == "www.codebuddy.cn"

    def test_device_token_reserved_interface(self):
        assert "x-device-token" not in official_ide_headers("m", "c", "VSCode")
        headers = official_ide_headers("m", "c", "VSCode", device_token="tok-123")
        assert headers["x-device-token"] == "tok-123"


class TestIdFormats:
    def test_hex32(self):
        value = _hex32()
        assert HEX32.match(value), value

    def test_hex16(self):
        value = _hex16()
        assert HEX16.match(value), value

    def test_uuid_and_timestamp_formats(self):
        assert UUID_RE.match(str(uuid.uuid4()))
        ts = str(int(time.time() * 1000))
        assert MS_TS.match(ts), ts


class TestGzipBodyIfLarge:
    def test_small_body_passthrough(self):
        data = b'{"a":1}'
        out, extra = gzip_body_if_large(data)
        assert out == data
        assert extra == {}

    def test_large_body_compressed(self):
        data = b"x" * 5000
        out, extra = gzip_body_if_large(data)
        assert extra == {"Content-Encoding": "gzip"}
        assert gzip.decompress(out) == data


def _make_state(tmp_path: pathlib.Path, platform: str = "VSCode") -> ProxyState:
    client = CodeBuddyClient(
        "https://upstream.test",
        platform=platform,
        session_file=tmp_path / "session.json",
    )
    client.session = {
        "account": {"uid": "uid-1"},
        "auth": {"accessToken": "test-token", "expiresAt": int(time.time() * 1000) + 3600_000},
    }
    return ProxyState(
        client=client,
        mock_dir=tmp_path,
        log_file=None,
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        logger=None,
        rate_jitter=0.0,  # P2：测试中关闭抖动避免拖慢用例
    )


class TestConversationId:
    def test_same_fingerprint_reuses_conv_id(self, tmp_path):
        state = _make_state(tmp_path)
        body_a = {
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "hi"}],
            "tools": [{"name": "t1"}],
        }
        body_b = {
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "sys prompt"}, {"role": "user", "content": "other turn"}],
            "tools": [{"name": "t1"}],
        }
        id1 = state.get_conversation_id(body_a)
        id2 = state.get_conversation_id(body_b)
        assert id1 == id2
        assert HEX32.match(id1)

    def test_different_fingerprint_different_conv_id(self, tmp_path):
        state = _make_state(tmp_path)
        base = {
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "sys prompt"}],
            "tools": [],
        }
        other_system = {
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "different system"}],
            "tools": [],
        }
        other_model = {**base, "model": "hy3"}
        other_tools = {**base, "tools": [{"name": "t1"}]}
        ids = [
            state.get_conversation_id(base),
            state.get_conversation_id(other_system),
            state.get_conversation_id(other_model),
            state.get_conversation_id(other_tools),
        ]
        assert len(set(ids)) == 4

    def test_dict_overflow_clears(self, tmp_path):
        state = _make_state(tmp_path)
        for i in range(1002):
            state.get_conversation_id({"model": f"m{i}", "messages": [], "tools": []})
        assert len(state._conversations) <= 1001


class _FakeResp:
    status_code = 200

    async def aread(self):
        return b""

    async def aclose(self):
        return None

    async def aiter_lines(self):
        yield 'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{}}'
        yield "data: [DONE]"


class _FakeStreamCM:
    async def __aenter__(self):
        return _FakeResp()

    async def __aexit__(self, *exc):
        return False


class TestIntlOfficialHeaderParity:
    """国际版最终发送头必须是官方抓包头集的子集（mitm 实测，2026-09-25 t006 报文）。

    官方 /v2/chat/completions 头集（41 项，见 doc/HANDOFF-intl-token.md）：
    httpx 框架头 host/connection/content-length/accept + content-encoding(gzip 时) 在内，
    缺的只有 acp-connection-id（我们没有真实 ACP 连接，伪造风险更高，按决策不加）。
    """

    OFFICIAL_HEADERS = {
        # httpx 框架层（官方 undici 也有等价头）
        "host", "connection", "content-length", "accept", "content-encoding",
        # 业务指纹头
        "user-agent", "authorization", "x-user-id", "x-domain", "accept",
        "content-type", "x-requested-with",
        "x-ide-name", "x-ide-type", "x-ide-version", "x-product",
        "x-agent-intent", "x-agent-purpose", "x-agent-type",
        "x-private-data", "x-codebuddy-request",
        "x-conversation-id", "x-conversation-message-id", "x-conversation-request-id",
        "x-request-id", "x-root-request-id",
        "x-stainless-arch", "x-stainless-lang", "x-stainless-os",
        "x-stainless-package-version", "x-stainless-retry-count",
        "x-stainless-runtime", "x-stainless-runtime-version",
        "traceparent", "b3", "x-b3-traceid", "x-b3-spanid",
        "x-b3-parentspanid", "x-b3-sampled", "x-trace-id",
        "acp-connection-id",  # 官方有我们没有；允许出现在集合中，宽松计入
    }

    @pytest.fixture()
    def captured(self, tmp_path, monkeypatch):
        requests = []

        async def _fake_send(self, request, *, stream=False, **kwargs):
            requests.append({
                "method": request.method,
                "url": str(request.url),
                "headers": request.headers,
                "content": request.content,
            })
            return _FakeResp()

        monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
        state = _make_state(tmp_path, platform="workbuddy-ai")
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)
        yield requests
        codebuddy_proxy.proxy_state = None

    def _post_intl(self) -> dict:
        with TestClient(codebuddy_proxy.app) as client:
            return client.post("/v1/chat/completions", content=json.dumps({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
            }), headers={"Content-Type": "application/json"}).json()

    def test_sent_headers_subset_of_official(self, captured):
        self._post_intl()
        headers = captured[0]["headers"]
        extra = sorted(set(headers) - self.OFFICIAL_HEADERS)
        assert extra == [], f"官方没有的头不允许发送: {extra}"

    def test_no_accept_encoding_noise(self, captured):
        self._post_intl()
        headers = captured[0]["headers"]
        assert "accept-encoding" not in headers
        for banned in ("x-model-id", "x-product-code", "x-product-version",
                       "x-env-id", "accept-language", "sec-fetch-mode",
                       "monitor_httpsendtime", "x-request-trace-id",
                       "x-device-token", "x-refresh-token"):
            assert banned not in headers, banned

    def test_intl_fingerprint_values(self, captured):
        self._post_intl()
        headers = captured[0]["headers"]
        assert headers["user-agent"] == f"WorkBuddy/{INTL_IDE_VERSION} WorkBuddy/{INTL_IDE_VERSION} CLI/2.137.1"
        assert headers["x-ide-name"] == "WorkBuddy"
        assert headers["x-ide-version"] == INTL_IDE_VERSION
        assert headers["x-product"] == "SaaS"
        assert headers["x-agent-purpose"] == "conversation"
        assert headers["x-stainless-runtime"] == "node"
        assert INTL_IDE_VERSION == "5.5.2"
        # 官方行为：X-Request-ID 与 X-Conversation-Message-ID 同值
        assert headers["x-request-id"] == headers["x-conversation-message-id"]
        assert headers["x-request-id"] != headers["x-conversation-request-id"]

    def test_trace_headers_same_origin(self, captured):
        """OPtel 头必须同源：traceparent/b3/X-Trace-ID/X-B3-TraceId/X-Root-Request-ID。

        逆向结论（xiaofan6ya §10.9）：同一请求里出现两个不同 traceId 是自相矛盾的
        特征，比不发更糟。本断言为纯本地内存校验，不发起任何网络请求。
        """
        self._post_intl()
        headers = captured[0]["headers"]
        trace_id = headers["x-b3-traceid"]
        span_id = headers["x-b3-spanid"]
        assert headers["x-trace-id"] == trace_id
        assert headers["traceparent"] == f"00-{trace_id}-{span_id}-01"
        assert headers["b3"] == f"{trace_id}-{span_id}-1"
        assert headers["x-b3-sampled"] == "1"
        assert HEX32.match(trace_id), trace_id
        assert HEX16.match(span_id), span_id
        # 国际版：root/request/conversation-request 三层同源
        assert headers["x-root-request-id"] == headers["x-conversation-request-id"]


class TestForwardChatFingerprint:
    @pytest.fixture()
    def captured(self, tmp_path, monkeypatch):
        requests = []

        async def _fake_send(self, request, *, stream=False, **kwargs):
            requests.append({
                "method": request.method,
                "url": str(request.url),
                "headers": request.headers,  # httpx.Headers：大小写不敏感
                "content": request.content,
                "kwargs": kwargs,
            })
            return _FakeResp()

        monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
        state = _make_state(tmp_path)
        monkeypatch.setattr(codebuddy_proxy, "proxy_state", state)
        yield requests
        codebuddy_proxy.proxy_state = None

    def _post(self, body: dict) -> dict:
        with TestClient(codebuddy_proxy.app) as client:
            resp = client.post("/v1/chat/completions", json=body)
        assert resp.status_code == 200
        return resp.json()

    def test_forward_headers_fingerprint(self, captured):
        self._post({
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "you are helpful"}, {"role": "user", "content": "hi"}],
            "stream": False,
        })
        assert len(captured) == 1
        req = captured[0]
        assert req["url"] == "https://upstream.test/v2/chat/completions"
        headers = req["headers"]

        # 认证头保留
        assert headers.get("Authorization") == "Bearer test-token"
        assert headers.get("X-User-Id") == "uid-1"
        assert headers.get("Content-Type") == "application/json"
    
        # 指纹字段（除 x-device-token 外全部存在）
        assert headers["user-agent"] == f"CodeBuddyIDE/{OFFICIAL_IDE_VERSION} CodeBuddy/{OFFICIAL_IDE_VERSION}"
        assert headers["x-ide-name"] == "CodeBuddyIDE"
        assert headers["x-ide-type"] == "CodeBuddyIDE"
        assert headers["x-ide-version"] == OFFICIAL_IDE_VERSION
        assert headers["x-product"] == "SaaS"
        assert headers["x-product-code"] == "codebuddy"
        assert headers["x-product-version"] == OFFICIAL_IDE_VERSION
        assert headers["x-env-id"] == "production"
        assert headers["x-domain"] == "www.codebuddy.cn"
        assert headers["x-agent-intent"] == "craft"
        assert headers["x-model-id"] == "gpt-5.5"
        assert headers["x-requested-with"] == "XMLHttpRequest"
        assert headers["accept"] == "*/*"
        assert headers["sec-fetch-mode"] == "cors"
        assert "x-device-token" not in headers

        # 格式断言
        conv_id = headers["x-conversation-id"]
        msg_id = headers["x-conversation-message-id"]
        req_id = headers["x-conversation-request-id"]
        trace_id = headers["x-b3-traceid"]
        span_id = headers["x-b3-spanid"]
        assert HEX32.match(conv_id), conv_id
        assert HEX32.match(msg_id), msg_id
        assert req_id == headers["x-request-id"]
        assert HEX32.match(req_id), req_id
        assert UUID_RE.match(headers["x-request-trace-id"]), headers["x-request-trace-id"]
        assert HEX32.match(trace_id), trace_id
        assert HEX16.match(span_id), span_id
        assert headers["x-b3-sampled"] == "1"
        assert headers["b3"] == f"{trace_id}-{span_id}-1"
        assert MS_TS.match(headers["monitor_httpsendtime"]), headers["monitor_httpsendtime"]

    def test_forward_max_tokens_default_and_payload(self, captured):
        self._post({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        })
        req = captured[0]
        assert "Content-Encoding" not in req["headers"]
        payload = json.loads(req["content"].decode("utf-8"))
        assert payload["max_tokens"] == 393216
        assert payload["stream"] is True

    def test_forward_max_tokens_respected(self, captured):
        self._post({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1024,
            "stream": False,
        })
        payload = json.loads(captured[0]["content"].decode("utf-8"))
        assert payload["max_tokens"] == 1024

    def test_forward_conversation_id_reuse(self, captured):
        body = {
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "stable system"}, {"role": "user", "content": "turn1"}],
            "stream": False,
        }
        self._post(body)
        body2 = {
            "model": "gpt-5.5",
            "messages": [{"role": "system", "content": "stable system"}, {"role": "user", "content": "turn2"}],
            "stream": False,
        }
        self._post(body2)
        assert len(captured) == 2
        assert captured[0]["headers"]["x-conversation-id"] == captured[1]["headers"]["x-conversation-id"]
        # 消息级/请求级 id 每请求新生成
        assert captured[0]["headers"]["x-conversation-message-id"] != captured[1]["headers"]["x-conversation-message-id"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
