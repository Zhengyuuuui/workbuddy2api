"""P1 阶段测试：device-token 提取/注入、previous_response_id 延续、b3 复用。

全部测试通过 mock httpx 上游运行，不请求真实上游。
"""

import json
import re
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy
from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import (
    ProxyState,
    _extract_upstream_response_id,
    official_ide_headers,
    resolve_device_token,
)
from extract_device_token import coerce_token_value, find_device_token

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX16 = re.compile(r"^[0-9a-f]{16}$")

TOKEN_LONG = "v3:" + "A" * 120
TOKEN_OTHER = "v3:" + "B" * 130


# ============================================================================
# extract_device_token
# ============================================================================

class TestFindDeviceToken:
    def test_finds_token_in_scanned_file(self, tmp_path):
        fake = tmp_path / "storage.bin"
        fake.write_bytes(b"prefix junk " + TOKEN_LONG.encode() + b" suffix")
        scanned = []
        result = find_device_token(scan_dirs=[tmp_path], scanned=scanned)
        assert result is not None
        assert result["token"] == TOKEN_LONG
        assert result["source"] == str(fake)
        assert result["found_at"]
        assert str(tmp_path) in scanned

    def test_env_var_has_highest_priority(self, tmp_path, monkeypatch):
        fake = tmp_path / "disk.bin"
        fake.write_bytes(TOKEN_OTHER.encode())
        monkeypatch.setenv("CODEBUDDY_DEVICE_TOKEN", TOKEN_LONG)
        result = find_device_token(scan_dirs=[tmp_path])
        assert result is not None
        assert result["token"] == TOKEN_LONG
        assert result["source"] == "env:CODEBUDDY_DEVICE_TOKEN"

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEBUDDY_DEVICE_TOKEN", raising=False)
        empty = tmp_path / "empty"
        empty.mkdir()
        scanned = []
        result = find_device_token(scan_dirs=[empty], scanned=scanned)
        assert result is None
        assert scanned  # 记录了已扫描目录供人工排查

    def test_invalid_env_value_falls_through(self, tmp_path, monkeypatch):
        fake = tmp_path / "disk.bin"
        fake.write_bytes(TOKEN_LONG.encode())
        monkeypatch.setenv("CODEBUDDY_DEVICE_TOKEN", "not-a-token")
        result = find_device_token(scan_dirs=[tmp_path])
        assert result is not None
        assert result["token"] == TOKEN_LONG
        assert result["source"] == str(fake)

    def test_sqlite_scan_finds_plain_token(self, tmp_path):
        db = tmp_path / "state.vscdb"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("someKey", TOKEN_LONG.encode()))
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)",
                     ("secret://pylance", b"v10" + b"\x00" * 40))  # 加密值跳过不崩溃
        conn.commit()
        conn.close()
        scanned = []
        result = find_device_token(db=db, scan_dirs=[], scanned=scanned)
        assert result is not None
        assert result["token"] == TOKEN_LONG
        assert result["source"].startswith(f"sqlite:{db}#")

    def test_missing_db_and_dirs_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CODEBUDDY_DEVICE_TOKEN", raising=False)
        result = find_device_token(db=tmp_path / "nope.vscdb", scan_dirs=[tmp_path / "nope"])
        assert result is None

    def test_coerce_token_value(self, tmp_path):
        assert coerce_token_value(TOKEN_LONG) == TOKEN_LONG
        assert coerce_token_value(json.dumps({"token": TOKEN_LONG})) == TOKEN_LONG
        f = tmp_path / "tok.json"
        f.write_text(json.dumps({"token": TOKEN_LONG, "source": "x"}))
        assert coerce_token_value(str(f)) == TOKEN_LONG
        bare = tmp_path / "tok.txt"
        bare.write_text(TOKEN_LONG + "\n")
        assert coerce_token_value(str(bare)) == TOKEN_LONG
        assert coerce_token_value(None) is None
        assert coerce_token_value("   ") is None


class TestResolveDeviceToken:
    def test_cli_beats_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEBUDDY_DEVICE_TOKEN", TOKEN_OTHER)
        token, source = resolve_device_token(TOKEN_LONG)
        assert token == TOKEN_LONG
        assert source == "cli:--device-token"

    def test_cli_accepts_json_file_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODEBUDDY_DEVICE_TOKEN", TOKEN_OTHER)
        f = tmp_path / "device-token.json"
        f.write_text(json.dumps({"token": TOKEN_LONG}))
        token, source = resolve_device_token(str(f))
        assert token == TOKEN_LONG
        assert source == "cli:--device-token"

    def test_env_beats_auto_discovery(self, monkeypatch):
        monkeypatch.setenv("CODEBUDDY_DEVICE_TOKEN", TOKEN_LONG)
        monkeypatch.setattr(codebuddy_proxy, "_find_device_token",
                            lambda **kw: {"token": TOKEN_OTHER, "source": "auto"})
        token, source = resolve_device_token(None)
        assert token == TOKEN_LONG
        assert source == "env:CODEBUDDY_DEVICE_TOKEN"

    def test_auto_discovery_fallback(self, monkeypatch):
        monkeypatch.delenv("CODEBUDDY_DEVICE_TOKEN", raising=False)
        monkeypatch.setattr(codebuddy_proxy, "_find_device_token",
                            lambda **kw: {"token": TOKEN_OTHER, "source": "auto:x"})
        token, source = resolve_device_token(None)
        assert token == TOKEN_OTHER
        assert source == "auto:x"

    def test_nothing_found_returns_none(self, monkeypatch):
        monkeypatch.delenv("CODEBUDDY_DEVICE_TOKEN", raising=False)
        monkeypatch.setattr(codebuddy_proxy, "_find_device_token", lambda **kw: None)
        token, source = resolve_device_token(None)
        assert token is None
        assert source == ""


# ============================================================================
# httpx mock 夹具（不请求真实上游）
# ============================================================================

class _FakeResp:
    def __init__(self, lines, exc=None, status_code=200):
        self._lines = lines
        self._exc = exc
        self.status_code = status_code

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._exc is not None:
            raise self._exc


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class Harness:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.captured = []
        self.script = []  # 每次上游请求弹出一项：(lines, exc)

    def make_state(self, **kwargs) -> ProxyState:
        client = CodeBuddyClient(
            "https://upstream.test",
            platform="VSCode",
            session_file=self.tmp_path / "session.json",
        )
        client.session = {
            "account": {"uid": "uid-1"},
            "auth": {"accessToken": "test-token", "expiresAt": int(time.time() * 1000) + 3600_000},
        }
        return ProxyState(client=client, mock_dir=self.tmp_path, log_file=None, **kwargs)

    def set_state(self, state: ProxyState):
        codebuddy_proxy.proxy_state = state

    @property
    def state(self) -> ProxyState:
        return codebuddy_proxy.proxy_state

    def post(self, body: dict):
        with TestClient(codebuddy_proxy.app) as client:
            return client.post("/v1/chat/completions", json=body)

    def payloads(self):
        out = []
        for req in self.captured:
            content = req["content"]
            if isinstance(content, bytes) and req["headers"].get("Content-Encoding") != "gzip":
                out.append(json.loads(content.decode("utf-8")))
            else:
                out.append(content)
        return out


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    h = Harness(tmp_path)

    def _fake_stream(self, method, url, headers=None, content=None, **kwargs):
        if h.script:
            lines, exc = h.script.pop(0)
        else:
            lines, exc = ["data: [DONE]"], None
        h.captured.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "content": content})
        return _FakeStreamCM(_FakeResp(lines, exc))

    monkeypatch.setattr(httpx.AsyncClient, "stream", _fake_stream)
    h.set_state(h.make_state())
    yield h
    codebuddy_proxy.proxy_state = None


def _chat_body(system: str, user: str = "hi", **extra) -> dict:
    body = {
        "model": "gpt-5.5",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
    }
    body.update(extra)
    return body


def _sse_chunk(rid: str, content: str = "ok") -> str:
    return f'data: {json.dumps({"id": rid, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}]})}'


# ============================================================================
# device-token 注入
# ============================================================================

class TestDeviceTokenInjection:
    def test_official_ide_headers_includes_device_token(self):
        headers = official_ide_headers("m", "c", "VSCode", device_token=TOKEN_LONG)
        assert headers["x-device-token"] == TOKEN_LONG

    def test_forward_chat_sends_device_token_when_injected(self, harness):
        harness.set_state(harness.make_state(device_token=TOKEN_LONG))
        resp = harness.post(_chat_body("dt-inject"))
        assert resp.status_code == 200
        assert harness.captured[0]["headers"]["x-device-token"] == TOKEN_LONG

    def test_forward_chat_omits_device_token_when_missing(self, harness):
        resp = harness.post(_chat_body("dt-missing"))
        assert resp.status_code == 200
        assert "x-device-token" not in harness.captured[0]["headers"]

    def test_device_token_not_overriding_auth(self, harness):
        harness.set_state(harness.make_state(device_token=TOKEN_LONG))
        harness.post(_chat_body("dt-auth"))
        headers = harness.captured[0]["headers"]
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["X-User-Id"] == "uid-1"


# ============================================================================
# previous_response_id 会话延续
# ============================================================================

class TestPreviousResponseId:
    RID = "cmb-aaaabbbbccccddddeeeeffff00001111"

    def test_second_request_chains_previous_response_id(self, harness):
        harness.script = [([_sse_chunk(self.RID), "data: [DONE]"], None),
                          (["data: [DONE]"], None)]
        r1 = harness.post(_chat_body("prev-chain", "turn1"))
        assert r1.status_code == 200
        r2 = harness.post(_chat_body("prev-chain", "turn2"))
        assert r2.status_code == 200
        payloads = harness.payloads()
        assert "previous_response_id" not in payloads[0]
        assert payloads[1]["previous_response_id"] == self.RID
        # conv 链路正常：同会话
        assert (harness.captured[0]["headers"]["x-conversation-id"]
                == harness.captured[1]["headers"]["x-conversation-id"])

    def test_explicit_client_value_is_respected(self, harness):
        harness.script = [([_sse_chunk(self.RID), "data: [DONE]"], None),
                          (["data: [DONE]"], None)]
        harness.post(_chat_body("prev-explicit", "turn1"))
        harness.post(_chat_body("prev-explicit", "turn2",
                                previous_response_id="cmb-client-keep"))
        assert harness.payloads()[1]["previous_response_id"] == "cmb-client-keep"

    def test_broken_stream_does_not_record(self, harness):
        harness.script = [([_sse_chunk(self.RID)], httpx.ReadError("upstream broke")),
                          (["data: [DONE]"], None)]
        body = _chat_body("prev-broken", "turn1")
        r1 = harness.post(body)
        assert r1.status_code == 502
        conv_id = harness.state.get_conversation_id(body)
        assert harness.state.peek_previous_response_id(conv_id) is None
        r2 = harness.post(_chat_body("prev-broken", "turn2"))
        assert r2.status_code == 200
        assert "previous_response_id" not in harness.payloads()[1]

    def test_stream_protocol_records_on_done(self, harness):
        harness.script = [([_sse_chunk(self.RID), "data: [DONE]"], None),
                          (["data: [DONE]"], None)]
        body = _chat_body("prev-stream", "turn1", stream=True)
        resp = harness.post(body)
        assert resp.status_code == 200
        conv_id = harness.state.get_conversation_id(body)
        assert harness.state.peek_previous_response_id(conv_id) == self.RID
        harness.post(_chat_body("prev-stream", "turn2"))
        assert harness.payloads()[1]["previous_response_id"] == self.RID

    def test_overflow_protection(self, harness):
        state = harness.state
        for i in range(1002):
            state.record_response_id(f"conv-{i}", f"cmb-{i}")
        assert len(state._last_response_id) <= 1001


class TestExtractUpstreamResponseId:
    def test_top_level_cmb_id(self):
        assert _extract_upstream_response_id({"id": "cmb-abc123"}) == "cmb-abc123"

    def test_responses_protocol_response_id(self):
        assert _extract_upstream_response_id({"response": {"id": "resp_xyz"}}) == "resp_xyz"

    def test_non_cmb_top_level_id_ignored(self):
        assert _extract_upstream_response_id({"id": "chatcmpl-other"}) is None
        assert _extract_upstream_response_id({}) is None
        assert _extract_upstream_response_id({"id": 123}) is None


# ============================================================================
# b3 链路同会话复用
# ============================================================================

class TestB3TraceReuse:
    def test_same_conv_reuses_traceid_spanid_differs(self, harness):
        harness.post(_chat_body("b3-stable", "turn1"))
        harness.post(_chat_body("b3-stable", "turn2"))
        h1, h2 = harness.captured[0]["headers"], harness.captured[1]["headers"]
        assert h1["x-conversation-id"] == h2["x-conversation-id"]
        assert HEX32.match(h1["x-b3-traceid"])
        assert HEX16.match(h1["x-b3-spanid"])
        assert h1["x-b3-traceid"] == h2["x-b3-traceid"]
        assert h1["x-b3-spanid"] != h2["x-b3-spanid"]
        for h in (h1, h2):
            assert h["b3"] == f"{h['x-b3-traceid']}-{h['x-b3-spanid']}-1"
            assert h["x-b3-sampled"] == "1"

    def test_different_conv_gets_different_traceid(self, harness):
        harness.post(_chat_body("b3-conv-a"))
        harness.post(_chat_body("b3-conv-b"))
        h1, h2 = harness.captured[0]["headers"], harness.captured[1]["headers"]
        assert h1["x-conversation-id"] != h2["x-conversation-id"]
        assert h1["x-b3-traceid"] != h2["x-b3-traceid"]

    def test_trace_id_overflow_protection(self, harness):
        state = harness.state
        for i in range(1002):
            state.get_trace_id(f"conv-{i}")
        assert len(state._trace_ids) <= 1001


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
