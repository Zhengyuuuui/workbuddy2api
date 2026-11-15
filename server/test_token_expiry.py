"""token 过期判断的回归测试。

线上事故：CN bridge 突然全部请求 401（APISIX/openresty 的
"401 Authorization Required" HTML 页），根因是 accessToken 已过期 6 小时，
但 bridge 一直没刷新。

为什么没刷新：上游 /auth/token/refresh 的响应**不含 expiresAt 字段**，
而 ensure_authenticated 写的是

    expires_at = int(auth.get("expiresAt") or 0)
    if auth.get("accessToken") and (not expires_at or ...):
        return

expires_at 为 0 时 `not expires_at` 恒为 True，直接返回——即「字段缺失」
被当成了「永不过期」。于是 token 过期后继续使用，被网关拒绝。

修复：expiresAt 缺失时从 JWT 的 exp claim 兜底解析。
"""

import base64
import json
import time

import pytest

from codebuddy_client_demo import CodeBuddyClient


def _make_jwt(exp_seconds: float) -> str:
    """构造一个带 exp claim 的假 JWT（签名无所谓，只解 payload）。"""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp_seconds, "sub": "u1"}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


class _FakeClient(CodeBuddyClient):
    """不发起任何网络请求的客户端。

    refresh() 模仿真实实现：没有 refreshToken 时返回 False。
    """

    def __init__(self, session):
        self.session = session
        self.refreshed = False
        self.logged_in = False

    def refresh(self) -> bool:
        if not (self.session.get("auth") or {}).get("refreshToken"):
            return False
        self.refreshed = True
        return True

    def login(self, *, open_browser: bool = True) -> None:
        self.logged_in = True


def _session(**auth_over):
    auth = {"accessToken": "tok", "refreshToken": "rtok"}
    auth.update(auth_over)
    return {"auth": auth, "account": {"uid": "u1"}}


# ---------------------------------------------------------------------------
# JWT exp 解析
# ---------------------------------------------------------------------------

def test_token_exp_ms_parses_jwt():
    exp = time.time() + 3600
    ms = CodeBuddyClient._token_exp_ms(_make_jwt(exp))
    assert abs(ms - exp * 1000) < 1000


def test_token_exp_ms_non_jwt_returns_zero():
    assert CodeBuddyClient._token_exp_ms("not-a-jwt") == 0
    assert CodeBuddyClient._token_exp_ms(None) == 0
    assert CodeBuddyClient._token_exp_ms("") == 0
    assert CodeBuddyClient._token_exp_ms("a.b.c") == 0


def test_token_exp_ms_garbage_payload_returns_zero():
    """payload 不是合法 JSON 时不能抛异常。"""
    assert CodeBuddyClient._token_exp_ms("aaa.!!!.ccc") == 0
    assert CodeBuddyClient._token_exp_ms("aaa.eyJub3QiOiJqc29ufQ.ccc") == 0


# ---------------------------------------------------------------------------
# 核心回归：expiresAt 缺失 + token 已过期 → 必须刷新
# ---------------------------------------------------------------------------

def test_expired_jwt_without_expires_at_field_refreshes():
    """事故回归：session 无 expiresAt、JWT 已过期 → 必须触发刷新。

    旧逻辑 `not expires_at` 恒为 True，直接 return，永远不刷新。
    """
    expired = _make_jwt(time.time() - 3600)
    c = _FakeClient(_session(accessToken=expired))
    c.ensure_authenticated(open_browser=False)
    assert c.refreshed is True, "JWT 已过期但未触发刷新"
    assert c.logged_in is False


def test_valid_jwt_without_expires_at_field_does_not_refresh():
    """JWT 仍在有效期内时不应刷新（避免每个请求都打一次刷新接口）。"""
    valid = _make_jwt(time.time() + 7 * 86400)
    c = _FakeClient(_session(accessToken=valid))
    c.ensure_authenticated(open_browser=False)
    assert c.refreshed is False


def test_expires_at_field_takes_precedence():
    """显式声明了 expiresAt 时以它为准（覆盖 JWT）。"""
    expired_jwt = _make_jwt(time.time() + 7 * 86400)   # JWT 说有效
    future_ms = int((time.time() - 3600) * 1000)        # expiresAt 说已过期
    c = _FakeClient(_session(accessToken=expired_jwt, expiresAt=future_ms))
    c.ensure_authenticated(open_browser=False)
    assert c.refreshed is True


def test_expires_at_in_future_no_refresh():
    expired_jwt = _make_jwt(time.time() - 3600)         # JWT 说过期
    future_ms = int((time.time() + 7 * 86400) * 1000)   # expiresAt 说有效
    c = _FakeClient(_session(accessToken=expired_jwt, expiresAt=future_ms))
    c.ensure_authenticated(open_browser=False)
    assert c.refreshed is False


def test_near_expiry_refreshes_early():
    """60 秒内就过期的 token 应提前刷新，避免请求中途失效。"""
    near = _make_jwt(time.time() + 30)
    c = _FakeClient(_session(accessToken=near))
    c.ensure_authenticated(open_browser=False)
    assert c.refreshed is True


def test_refresh_failure_falls_back_to_login():
    valid_jwt = _make_jwt(time.time() - 3600)

    class _NoRefresh(_FakeClient):
        def refresh(self):
            self.refreshed = True
            return False

    c = _NoRefresh(_session(accessToken=valid_jwt))
    c.ensure_authenticated(open_browser=False)
    assert c.refreshed is True
    assert c.logged_in is True, "刷新失败应回退到登录"


def test_no_token_triggers_login():
    c = _FakeClient({"auth": {}, "account": {}})
    c.ensure_authenticated(open_browser=False)
    assert c.logged_in is True
