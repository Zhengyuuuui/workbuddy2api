"""P2 行为层防护测试：429/5xx 熔断、全局限速、额度监控与消耗速率告警。

全部走 httpx mock + TestClient（复用 test_p1_features 夹具模式），不触真实上游。
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import codebuddy_proxy
from codebuddy_client_demo import CodeBuddyClient
from codebuddy_proxy import (
    ProxyState,
    RateLimiter,
    UpstreamCircuitBreaker,
)

TZ8 = timezone(timedelta(hours=8))
# 固定"当前时间"：让规格里的 2026-09-13 样例成为未来时间
FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=TZ8).timestamp()


def _expected_epoch(y, mo, d, h, mi, s) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ8).timestamp()


# ============================================================================
# 熔断器：reset 文案解析（中英文）
# ============================================================================

class TestParseResetAt:
    def test_english_reset_text(self):
        text = "usage will reset at 2026-09-13 10:46:15 UTC+8"
        assert UpstreamCircuitBreaker.parse_reset_at(text, now=FIXED_NOW) == pytest.approx(
            _expected_epoch(2026, 9, 13, 10, 46, 15)
        )

    def test_chinese_reset_text(self):
        text = '{"error":{"message":"将在 2026-09-13 14:48:28 UTC+8 重置","code":6004}}'
        assert UpstreamCircuitBreaker.parse_reset_at(text, now=FIXED_NOW) == pytest.approx(
            _expected_epoch(2026, 9, 13, 14, 48, 28)
        )

    def test_embedded_in_details(self):
        text = "quota exceeded; usage will reset at 2026-09-14 08:00:00 UTC+8, please retry later"
        assert UpstreamCircuitBreaker.parse_reset_at(text, now=FIXED_NOW) == pytest.approx(
            _expected_epoch(2026, 9, 14, 8, 0, 0)
        )

    def test_no_datetime_returns_none(self):
        assert UpstreamCircuitBreaker.parse_reset_at("too many requests", now=FIXED_NOW) is None
        assert UpstreamCircuitBreaker.parse_reset_at("", now=FIXED_NOW) is None
        assert UpstreamCircuitBreaker.parse_reset_at(None, now=FIXED_NOW) is None

    def test_past_time_returns_none(self):
        # 规格样例 2026-09-13 相对真实当前时间（2026-09-22 之后恒为过去）已过期
        text = "usage will reset at 2026-09-13 10:46:15 UTC+8"
        assert UpstreamCircuitBreaker.parse_reset_at(text) is None


# ============================================================================
# 熔断器：trip / check / 5xx 计数 / snapshot / clear
# ============================================================================

class TestBreakerUnit:
    def test_trip_without_reset_defaults_300s(self):
        b = UpstreamCircuitBreaker()
        b.trip("https://ep", "m1", None, reason="upstream 429")
        tripped, remaining = b.check("https://ep", "m1")
        assert tripped is True
        assert 295.0 <= remaining <= 300.0

    def test_trip_with_explicit_reset_at(self):
        b = UpstreamCircuitBreaker()
        reset_at = time.time() + 50
        b.trip("https://ep", "m1", reset_at, reason="parsed")
        tripped, remaining = b.check("https://ep", "m1")
        assert tripped is True
        assert 48.0 <= remaining <= 50.0

    def test_lazy_recovery_after_cooldown(self):
        b = UpstreamCircuitBreaker()
        b.trip("https://ep", "m1", time.time() - 1, reason="expired")
        assert b.check("https://ep", "m1") == (False, 0.0)
        # 条目被惰性清理
        assert b.check("https://ep", "m1") == (False, 0.0)
        assert b.snapshot() == []

    def test_key_isolation_by_endpoint_and_model(self):
        b = UpstreamCircuitBreaker()
        b.trip("https://ep-a", "m1", time.time() + 100, reason="x")
        assert b.check("https://ep-a", "m1")[0] is True
        assert b.check("https://ep-a", "m2")[0] is False
        assert b.check("https://ep-b", "m1")[0] is False

    def test_five_consecutive_5xx_trips_120s(self):
        b = UpstreamCircuitBreaker()
        for i in range(4):
            assert b.note_5xx("https://ep", "m1") == (False, 0.0)
            assert b.check("https://ep", "m1")[0] is False
        tripped, duration = b.note_5xx("https://ep", "m1")
        assert tripped is True
        assert duration == UpstreamCircuitBreaker.DEFAULT_5XX_COOLDOWN == 120.0
        tripped_now, remaining = b.check("https://ep", "m1")
        assert tripped_now is True
        assert 115.0 <= remaining <= 120.0

    def test_success_200_resets_5xx_streak(self):
        b = UpstreamCircuitBreaker()
        for _ in range(4):
            b.note_5xx("https://ep", "m1")
        b.note_success("https://ep", "m1")
        for _ in range(4):
            b.note_5xx("https://ep", "m1")
        assert b.check("https://ep", "m1")[0] is False
        tripped, _ = b.note_5xx("https://ep", "m1")  # 清零后重新数满 5 次
        assert tripped is True

    def test_snapshot_and_clear(self):
        b = UpstreamCircuitBreaker()
        b.trip("https://ep", "m1", time.time() + 100, reason="upstream 429")
        b.note_5xx("https://ep", "m2")  # 进行中的 streak 也可见
        snap = b.snapshot()
        assert len(snap) == 2
        by_model = {e["model"]: e for e in snap}
        assert by_model["m1"]["tripped"] is True
        assert by_model["m1"]["remaining_seconds"] > 0
        assert by_model["m2"]["tripped"] is False
        assert by_model["m2"]["streak_5xx"] == 1

        assert b.clear("m1") == 1
        assert b.check("https://ep", "m1")[0] is False
        assert b.clear() == 1  # 清掉 m2 的 streak
        assert b.snapshot() == []


# ============================================================================
# 全局限速 RateLimiter
# ============================================================================

class TestRateLimiter:
    def test_burst_requests_immediate(self):
        rl = RateLimiter(rate_qps=1.0, burst=5, jitter=0.0)

        async def run():
            t0 = time.monotonic()
            for _ in range(5):
                await rl.acquire()
            return time.monotonic() - t0

        assert asyncio.run(run()) < 0.5

    def test_over_burst_queues_with_increasing_elapsed(self):
        rl = RateLimiter(rate_qps=10, burst=3, jitter=0.0)

        async def run():
            done_times = []
            t0 = time.monotonic()
            for _ in range(5):
                await rl.acquire()
                done_times.append(time.monotonic() - t0)
            return done_times

        times = asyncio.run(run())
        # burst 内 3 个立即完成
        assert times[2] < 0.15
        # 超出部分排队，完成时刻依次递增
        assert times[3] >= times[2] + 0.05
        assert times[4] >= times[3] + 0.05

    def test_jitter_zero_means_no_extra_delay(self):
        rl = RateLimiter(rate_qps=100.0, burst=10, jitter=0.0)

        async def run():
            t0 = time.monotonic()
            for _ in range(3):
                await rl.acquire()
            return time.monotonic() - t0

        assert asyncio.run(run()) < 0.1

    def test_jitter_sleeps_uniform(self, monkeypatch):
        monkeypatch.setattr(random, "uniform", lambda a, b: 0.5)
        rl = RateLimiter(rate_qps=100.0, burst=10, jitter=3.0)

        async def run():
            t0 = time.monotonic()
            await rl.acquire()
            return time.monotonic() - t0

        assert asyncio.run(run()) >= 0.45

    def test_status_fields(self):
        rl = RateLimiter(rate_qps=1.0, burst=5, jitter=3.0)
        status = rl.status()
        assert status["rate_qps"] == 1.0
        assert status["burst"] == 5
        assert status["jitter_seconds"] == 3.0
        assert status["available_tokens"] == 5.0
        assert status["queued_requests"] == 0

    def test_proxy_state_rate_defaults(self, tmp_path):
        client = CodeBuddyClient("https://upstream.test", platform="VSCode",
                                 session_file=tmp_path / "session.json")
        state = ProxyState(client=client, mock_dir=tmp_path, log_file=None)
        assert state.rate_limiter.rate_qps == 1.0
        assert state.rate_limiter.burst == 5
        assert state.rate_limiter.jitter == 3.0
        assert state.credit_burn_threshold == 0.3


# ============================================================================
# httpx mock 夹具（不请求真实上游）
# ============================================================================

class _FakeResp:
    def __init__(self, lines=(), exc=None, status_code=200, body=b""):
        self._lines = list(lines)
        self._exc = exc
        self.status_code = status_code
        self._body = body

    async def aread(self):
        return self._body

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


class _FakeBillingResp:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else (
            json.dumps(json_data, ensure_ascii=False) if json_data is not None else ""
        )

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class Harness:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.captured = []
        self.script = []  # 每次上游 chat 请求弹出一项 dict(status, lines, exc, body)
        self.billing: dict | Exception = httpx.ConnectError("billing mock not configured")
        self.billing_calls: list[str] = []
        self.log_handler: _ListHandler | None = None

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
        # 测试环境：关闭抖动、放大限速桶，避免用例被排队拖慢/干扰
        kwargs.setdefault("rate_qps", 100.0)
        kwargs.setdefault("rate_burst", 100)
        kwargs.setdefault("rate_jitter", 0.0)
        return ProxyState(client=client, mock_dir=self.tmp_path, log_file=None, **kwargs)

    def attach_logger(self, state: ProxyState) -> _ListHandler:
        logger = logging.getLogger(f"p2-test-{id(state)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers.clear()
        handler = _ListHandler()
        logger.addHandler(handler)
        state.logger = logger
        self.log_handler = handler
        return handler

    def set_state(self, state: ProxyState):
        codebuddy_proxy.proxy_state = state

    @property
    def state(self) -> ProxyState:
        return codebuddy_proxy.proxy_state

    def post(self, body: dict):
        with TestClient(codebuddy_proxy.app) as client:
            return client.post("/v1/chat/completions", json=body)

    def get(self, path: str):
        with TestClient(codebuddy_proxy.app) as client:
            return client.get(path)

    def delete(self, path: str):
        with TestClient(codebuddy_proxy.app) as client:
            return client.delete(path)

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

    def _fake_send(self, request, *, stream=False, **kwargs):
        if h.script:
            cfg = h.script.pop(0)
        else:
            cfg = {"status": 200, "lines": ["data: [DONE]"]}
        h.captured.append({"method": request.method, "url": str(request.url),
                           "headers": request.headers, "content": request.content})
        return _FakeStreamCM(_FakeResp(
            lines=cfg.get("lines") or (),
            exc=cfg.get("exc"),
            status_code=cfg.get("status", 200),
            body=cfg.get("body", b""),
        ))

    async def _fake_post(self, url, *args, **kwargs):
        h.billing_calls.append(str(url))
        cfg = h.billing
        if isinstance(cfg, Exception):
            raise cfg
        return _FakeBillingResp(
            status_code=cfg.get("status", 200),
            json_data=cfg.get("json"),
            text=cfg.get("text"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    state = h.make_state()
    h.attach_logger(state)
    h.set_state(state)
    yield h
    codebuddy_proxy.proxy_state = None


def _chat_body(model: str = "gpt-5.5", **extra) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": f"p2-{model}"},
                     {"role": "user", "content": "hi"}],
        "stream": False,
    }
    body.update(extra)
    return body


def _future_reset_str(hours: float = 2.0) -> str:
    return (datetime.now(TZ8) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


SAMPLE_ACCOUNTS = {
    "Accounts": [
        {"PackageName": "codebuddy-pro-monthly", "CapacityRemainPrecise": 75.5,
         "CapacitySizePrecise": 100.0, "CycleEndTime": "2026-10-01T00:00:00Z"},
        {"PackageName": "bonus-credits", "CapacityRemainPrecise": 24.5,
         "CapacitySizePrecise": 100.0, "CycleEndTime": "2026-10-05T00:00:00Z"},
    ]
}


def _usage_chunk(prompt: int, completion: int, rid: str = "cmb-u1") -> str:
    return f'data: {json.dumps({"id": rid, "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}})}'


# ============================================================================
# 熔断端到端：429 快速失败 + /v1/breaker
# ============================================================================

class TestCircuitBreakerEndToEnd:
    def test_429_trips_and_fast_fails_without_upstream_call(self, harness):
        future = _future_reset_str(2.0)
        body = json.dumps({"error": {"message": f"usage will reset at {future} UTC+8", "code": 6004}})
        harness.script = [{"status": 429, "body": body.encode("utf-8")}]

        r1 = harness.post(_chat_body("m-429-en"))
        assert r1.status_code == 429
        assert len(harness.captured) == 1

        # 按解析出的 reset 时间熔断（约 2 小时）
        tripped, remaining = harness.state.breaker.check(
            harness.state.client.endpoint, "m-429-en")
        assert tripped is True
        assert 6500.0 <= remaining <= 7200.0

        # GET /v1/breaker 可见
        data = harness.get("/v1/breaker").json()
        assert data["count"] >= 1
        entry = next(e for e in data["breakers"] if e["model"] == "m-429-en")
        assert entry["tripped"] is True
        assert entry["remaining_seconds"] > 6000

        # 第二次请求：快速失败，上游零调用
        r2 = harness.post(_chat_body("m-429-en"))
        assert r2.status_code == 429
        err = r2.json()["detail"]["error"]
        assert "熔断中" in err["message"]
        assert "上游限频" in err["message"]
        assert err["type"] == "circuit_breaker"
        assert err["retry_after"] >= 1
        assert len(harness.captured) == 1

        # diagnostic 日志
        assert any("circuit_breaker_reject" in m for m in harness.log_handler.messages)

    def test_chinese_reset_text_drives_cooldown(self, harness):
        future = _future_reset_str(1.0)
        body = json.dumps({"error": {"message": f"将在 {future} UTC+8 重置", "code": 6004}})
        harness.script = [{"status": 429, "body": body.encode("utf-8")}]

        r1 = harness.post(_chat_body("m-429-zh"))
        assert r1.status_code == 429
        tripped, remaining = harness.state.breaker.check(
            harness.state.client.endpoint, "m-429-zh")
        assert tripped is True
        assert 3300.0 <= remaining <= 3600.0

        r2 = harness.post(_chat_body("m-429-zh"))
        assert r2.status_code == 429
        assert "熔断中" in r2.json()["detail"]["error"]["message"]

    def test_429_without_reset_text_defaults_300s(self, harness):
        harness.script = [{"status": 429,
                           "body": b'{"error":{"message":"too many requests"}}'}]
        r1 = harness.post(_chat_body("m-429-default"))
        assert r1.status_code == 429
        # 首次 429 透传时也带 retry_after
        assert r1.json()["detail"]["error"]["retry_after"] >= 295
        tripped, remaining = harness.state.breaker.check(
            harness.state.client.endpoint, "m-429-default")
        assert tripped is True
        assert 295.0 <= remaining <= 300.0

    def test_cooldown_expiry_auto_recovers(self, harness):
        # 直接注入已过期熔断 → 惰性恢复，请求正常放行
        harness.state.breaker.trip(
            harness.state.client.endpoint, "m-expired",
            time.time() - 1, reason="manual-expired")
        r = harness.post(_chat_body("m-expired"))
        assert r.status_code == 200
        assert len(harness.captured) == 1

    def test_five_5xx_trips_then_fast_fail(self, harness):
        harness.script = [{"status": 503, "body": b"upstream blew up"} for _ in range(5)]
        for _ in range(5):
            r = harness.post(_chat_body("m-5xx"))
            assert r.status_code == 503
        assert len(harness.captured) == 5

        r6 = harness.post(_chat_body("m-5xx"))
        assert r6.status_code == 429
        assert "熔断中" in r6.json()["detail"]["error"]["message"]
        assert len(harness.captured) == 5  # 上游无新调用

    def test_200_clears_5xx_streak(self, harness):
        harness.script = (
            [{"status": 502, "body": b"bad"} for _ in range(4)]
            + [{"status": 200, "lines": ["data: [DONE]"]}]
            + [{"status": 502, "body": b"bad"} for _ in range(4)]
        )
        statuses = []
        for _ in range(9):
            statuses.append(harness.post(_chat_body("m-5xx-reset")).status_code)
        assert statuses == [502] * 4 + [200] + [502] * 4
        # 200 清零后重新累计 4 次，尚未熔断
        assert harness.state.breaker.check(harness.state.client.endpoint, "m-5xx-reset")[0] is False
        snap = harness.state.breaker.snapshot()
        assert all(not e["tripped"] for e in snap)

    def test_stream_client_also_fast_fails_when_tripped(self, harness):
        harness.state.breaker.trip(
            harness.state.client.endpoint, "m-stream", time.time() + 100, reason="manual")
        r = harness.post(_chat_body("m-stream", stream=True))
        assert r.status_code == 429
        assert "熔断中" in r.json()["detail"]["error"]["message"]
        assert len(harness.captured) == 0

    def test_breaker_delete_endpoints(self, harness):
        ep = harness.state.client.endpoint
        harness.state.breaker.trip(ep, "model-a", time.time() + 100, reason="ra")
        harness.state.breaker.trip(ep, "model-b", time.time() + 100, reason="rb")

        data = harness.get("/v1/breaker").json()
        assert data["count"] == 2

        resp = harness.delete("/v1/breaker?model=model-a")
        assert resp.status_code == 200
        assert resp.json() == {"cleared": 1, "model": "model-a"}
        assert harness.state.breaker.check(ep, "model-a")[0] is False
        assert harness.state.breaker.check(ep, "model-b")[0] is True

        resp = harness.delete("/v1/breaker")
        assert resp.json()["cleared"] == 1
        assert harness.get("/v1/breaker").json() == {"breakers": [], "count": 0}


# ============================================================================
# /v1/credits
# ============================================================================

class TestCreditsEndpoint:
    def test_parses_accounts_and_totals(self, harness):
        harness.billing = {"status": 200, "json": SAMPLE_ACCOUNTS}
        data = harness.get("/v1/credits").json()
        assert data["available"] is True
        assert data["status"] == "ok"
        assert len(data["accounts"]) == 2
        acc = data["accounts"][0]
        assert acc["PackageName"] == "codebuddy-pro-monthly"
        assert acc["CapacityRemainPrecise"] == 75.5
        assert acc["CapacitySizePrecise"] == 100.0
        assert acc["CycleEndTime"] == "2026-10-01T00:00:00Z"
        assert data["total_remain"] == 100.0
        assert data["total_size"] == 200.0
        assert data["usage_ratio"] == pytest.approx(0.5)
        assert data["burn_warning"] is False
        assert data["cached"] is False
        assert len(harness.billing_calls) == 1

    def test_cache_60s_second_call_skips_upstream(self, harness):
        harness.billing = {"status": 200, "json": SAMPLE_ACCOUNTS}
        first = harness.get("/v1/credits").json()
        second = harness.get("/v1/credits").json()
        assert first["cached"] is False
        assert second["cached"] is True
        assert second["total_remain"] == first["total_remain"]
        assert len(harness.billing_calls) == 1

    def test_refresh_bypasses_cache(self, harness):
        harness.billing = {"status": 200, "json": SAMPLE_ACCOUNTS}
        harness.get("/v1/credits")
        harness.get("/v1/credits")
        harness.get("/v1/credits?refresh=1")
        assert len(harness.billing_calls) == 2

    def test_unavailable_403_falls_back_to_local(self, harness):
        harness.billing = {"status": 403,
                           "json": {"code": 10085, "message": "forbidden"},
                           "text": '{"code":10085,"message":"forbidden"}'}
        data = harness.get("/v1/credits").json()
        assert data["available"] is False
        assert data["status"] == "unavailable"
        assert data["http_status"] == 403
        assert "10085" in data["detail"]
        assert data["local"]["endpoint"] == "https://upstream.test"
        assert data["local"]["platform"] == "VSCode"
        assert data["local"]["window_seconds"] == 600
        assert "burn_warning" in data

    def test_network_error_marked_unavailable(self, harness):
        harness.billing = httpx.ConnectError("connection refused")
        data = harness.get("/v1/credits").json()
        assert data["available"] is False
        assert data["status"] == "unavailable"
        assert "error" in data


# ============================================================================
# 消耗速率监控
# ============================================================================

class TestCreditBurn:
    def test_usage_recorded_in_sliding_window(self, harness):
        harness.billing = {"status": 403, "text": "forbidden"}  # → 阈值兜底 500
        harness.script = [{"status": 200, "lines": [
            _usage_chunk(400, 300), "data: [DONE]"]}]
        r = harness.post(_chat_body("m-burn"))
        assert r.status_code == 200
        assert harness.state.burn_window_total() == 700

        data = harness.get("/v1/credits").json()
        assert data["burn_warning"] is True  # 700 > 500 兜底阈值
        assert any("credit_burn_warning" in m for m in harness.log_handler.messages)

    def test_below_threshold_no_warning(self, harness):
        harness.billing = {"status": 403, "text": "forbidden"}
        harness.script = [{"status": 200, "lines": [
            _usage_chunk(60, 40), "data: [DONE]"]}]
        harness.post(_chat_body("m-burn-low"))
        data = harness.get("/v1/credits").json()
        assert data["burn_warning"] is False

    def test_threshold_uses_credits_remain_ratio(self, harness):
        harness.billing = {"status": 200, "json": {"Accounts": [
            {"PackageName": "p", "CapacityRemainPrecise": 1000.0,
             "CapacitySizePrecise": 1000.0, "CycleEndTime": "2026-10-01"}]}}
        data = harness.get("/v1/credits").json()
        assert data["available"] is True
        assert data["total_remain"] == 1000.0
        # 阈值 = 1000 * 0.3 = 300

        harness.script = [{"status": 200, "lines": [
            _usage_chunk(350, 100), "data: [DONE]"]}]
        harness.post(_chat_body("m-burn-ratio"))
        assert harness.state.burn_window_total() == 450

        data = harness.get("/v1/credits").json()
        assert data["burn_warning"] is True  # 450 > 300

    def test_window_prunes_events_older_than_10min(self, harness):
        harness.state._usage_events.append((time.time() - 601, 99999))
        assert harness.state.burn_window_total() == 0.0

    def test_collect_without_usage_records_nothing(self, harness):
        harness.post(_chat_body("m-burn-nousage"))  # 默认 [DONE] 无 usage
        assert harness.state.burn_window_total() == 0.0


# ============================================================================
# health 附带限速器状态
# ============================================================================

class TestHealthRateLimiter:
    def test_health_includes_rate_limiter(self, harness):
        data = harness.get("/health").json()
        assert data["status"] == "ok"
        rl = data["rate_limiter"]
        assert rl["rate_qps"] == 100.0
        assert rl["burst"] == 100
        assert rl["jitter_seconds"] == 0.0
        assert rl["available_tokens"] == 100.0
        assert rl["queued_requests"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
