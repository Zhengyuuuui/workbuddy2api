#!/usr/bin/env python3
"""Minimal WorkBuddy AI (overseas) external-link-v2 client demo.

The protocol is identical to Tencent Cloud CodeBuddy's external-link-v2
(cli-external-link).  This is a copy of codebuddy_client_demo.py pointed at the
overseas WorkBuddy AI backend.

Runtime backend endpoint for the overseas build is resolved via `/v3/config`
and lands on https://www.codebuddy.ai (see externalDomain in product.json).
Also exposes the SMH session host and the model API host for reference.

This demo uses only the Python standard library and stores the session locally
with 0600 permissions.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import ssl
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Iterator

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# Overseas WorkBuddy AI runtime values (from app.asar cli/product.json + /v3/config)
PROD_ENDPOINT = "https://www.codebuddy.ai"
PROD_ENDPOINT_ALIAS = "https://www.workbuddy.ai"
STAGING_ENDPOINT = "https://staging-codebuddy.tencent.com"
SMH_HOST_OVERSEAS = "https://smh38ewydmp37j7v.ap-singapore.api.tencentsmh.com"
MODEL_API_HOST = "https://api.lkeap.cloud.tencent.com"
AUTH_PLATFORM = "workbuddy-ai"  # product.json authentication.attributes.platform


class WorkBuddyAIError(RuntimeError):
    pass


class WorkBuddyAIClient:
    def __init__(
        self,
        endpoint: str = PROD_ENDPOINT,
        platform: str = AUTH_PLATFORM,
        session_file: pathlib.Path | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.platform = platform
        self.prefix = "/plugin"
        self.session_file = session_file or pathlib.Path.home() / ".workbuddy-ai-session.json"
        self.session: dict[str, Any] = self._load_session()

    def _load_session(self) -> dict[str, Any]:
        try:
            return json.loads(self.session_file.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkBuddyAIError(f"无法读取 session 文件: {self.session_file}: {exc}") from exc

    def _save_session(self, session: dict[str, Any]) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(self.session_file, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(session, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        finally:
            try:
                os.chmod(self.session_file, 0o600)
            except OSError:
                pass
        self.session = session

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            nested = payload["data"]
            if "data" in nested:
                return nested["data"]
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 30,
    ) -> Any:
        request_headers = {"User-Agent": "WorkBuddyAIClientDemo/1.0"}
        request_headers.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            urllib.parse.urljoin(self.endpoint + "/", path.lstrip("/")),
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkBuddyAIError(f"HTTP {exc.code} {path}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise WorkBuddyAIError(f"请求失败 {path}: {exc.reason}") from exc
        if "json" not in content_type and not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkBuddyAIError(f"{path} 返回的不是 JSON: {raw[:300]!r}") from exc

    def auth_headers(self, *, access: bool = True, refresh: bool = False) -> dict[str, str]:
        account = self.session.get("account") or {}
        auth = self.session.get("auth") or {}
        headers: dict[str, str] = {}
        if account.get("uid"):
            headers["X-User-Id"] = str(account["uid"])
        if access and auth.get("accessToken"):
            headers["Authorization"] = f"Bearer {auth['accessToken']}"
        if refresh and auth.get("refreshToken"):
            headers["X-Refresh-Token"] = str(auth["refreshToken"])
        if account.get("enterpriseId"):
            headers["X-Enterprise-Id"] = str(account["enterpriseId"])
            headers["X-Tenant-Id"] = str(account["enterpriseId"])
        if auth.get("domain"):
            headers["X-Domain"] = str(auth["domain"])
        return headers

    def login(self, *, open_browser: bool = True, timeout: int = 300) -> None:
        no_auth = {
            "X-No-Authorization": "true",
            "X-No-User-Id": "true",
            "X-No-Enterprise-Id": "true",
            "X-No-Department-Info": "true",
        }
        state_payload = self._unwrap(
            self._request(
                "POST",
                f"/v2{self.prefix}/auth/state?platform={urllib.parse.quote(self.platform)}",
                headers=no_auth,
                body={},
            )
        )
        if not isinstance(state_payload, dict) or not state_payload.get("authUrl"):
            raise WorkBuddyAIError(f"登录状态响应缺少 authUrl: {state_payload!r}")
        auth_url = str(state_payload["authUrl"])
        state = state_payload.get("state")
        if not state:
            raise WorkBuddyAIError("登录状态响应缺少 state")
        print(f"请在浏览器中完成登录：\n{auth_url}")
        if open_browser:
            webbrowser.open(auth_url)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                token = self._unwrap(
                    self._request(
                        "GET",
                        f"/v2{self.prefix}/auth/token?state={urllib.parse.quote(str(state))}",
                        headers=no_auth,
                    )
                )
            except WorkBuddyAIError:
                continue
            if isinstance(token, dict) and token.get("accessToken"):
                account = self._unwrap(
                    self._request(
                        "GET",
                        f"/v2{self.prefix}/login/account?state={urllib.parse.quote(str(state))}",
                        headers={
                            "X-No-User-Id": "true",
                            "X-No-Enterprise-Id": "true",
                            "X-No-Department-Info": "true",
                            **self._token_headers(token),
                        },
                    )
                )
                if not isinstance(account, dict):
                    raise WorkBuddyAIError(f"登录账户响应格式异常: {account!r}")
                self._save_session({"auth": token, "account": account})
                print(f"登录成功，用户: {account.get('nickname') or account.get('uid', '<unknown>')}")
                return
        raise WorkBuddyAIError("登录超时")

    @staticmethod
    def _token_headers(token: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        if token.get("accessToken"):
            headers["Authorization"] = f"Bearer {token['accessToken']}"
        if token.get("domain"):
            headers["X-Domain"] = str(token["domain"])
        return headers

    def refresh(self) -> bool:
        auth = self.session.get("auth") or {}
        refresh_token = auth.get("refreshToken")
        if not refresh_token:
            return False
        payload = self._unwrap(
            self._request(
                "POST",
                f"/v2{self.prefix}/auth/token/refresh",
                headers={
                    **self.auth_headers(access=False, refresh=True),
                    "X-Auth-Refresh-Source": "plugin",
                },
                body={},
            )
        )
        if not isinstance(payload, dict) or not payload.get("accessToken"):
            return False
        self._save_session({**self.session, "auth": payload})
        return True

    def ensure_authenticated(self, *, open_browser: bool = True) -> None:
        auth = self.session.get("auth") or {}
        now_ms = int(time.time() * 1000)
        expires_at = int(auth.get("expiresAt") or 0)
        if auth.get("accessToken") and (not expires_at or expires_at > now_ms + 60_000):
            return
        if self.refresh():
            print("access token 已刷新")
            return
        self.login(open_browser=open_browser)

    def fetch_cloud_config(self) -> dict[str, Any]:
        """拉取 /v3/config?repos= 云端产品配置（含 endpoint / models）。"""
        self.ensure_authenticated()
        payload = self._request(
            "GET",
            "/v3/config?repos=",
            headers=self.auth_headers(),
            timeout=30,
        )
        return self._unwrap(payload) if isinstance(payload, dict) else payload

    def stream_chat(
        self,
        prompt: str,
        *,
        model: str = "default",
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Iterator[str]:
        self.ensure_authenticated()
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": "You are a helpful assistant."},
                         {"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        request = urllib.request.Request(
            f"{self.endpoint}/v2/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                **self.auth_headers(),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=180, context=_SSL_CONTEXT)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and self.refresh():
                yield from self.stream_chat(
                    prompt, model=model, temperature=temperature, max_tokens=max_tokens
                )
                return
            raise WorkBuddyAIError(f"聊天请求 HTTP {exc.code}: {detail[:1000]}") from exc
        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy AI login/refresh/chat demo")
    parser.add_argument("prompt", nargs="?", help="要发送的问题")
    parser.add_argument("--endpoint", default=os.getenv("WORKBUDDY_AI_ENDPOINT", PROD_ENDPOINT))
    parser.add_argument("--staging", action="store_true", help="使用 staging endpoint")
    parser.add_argument("--model", default=os.getenv("WORKBUDDY_AI_MODEL", "default"))
    parser.add_argument("--session-file", type=pathlib.Path)
    parser.add_argument("--no-browser", action="store_true", help="只打印登录 URL，不自动打开浏览器")
    parser.add_argument("--login", action="store_true", help="强制重新登录")
    parser.add_argument("--config", action="store_true", help="拉取 /v3/config 云端配置")
    args = parser.parse_args()
    if args.staging:
        args.endpoint = STAGING_ENDPOINT
    client = WorkBuddyAIClient(args.endpoint, session_file=args.session_file)
    try:
        if args.config:
            config = client.fetch_cloud_config()
            ep = config.get("endpoint") or args.endpoint
            print(f"endpoint: {ep}")
            print(f"smhHost: {config.get('smhHost')}")
            print("models:")
            for m in config.get("models", []) or []:
                if isinstance(m, dict):
                    print("  ", m.get("id"), "|", m.get("name", ""))
            return 0
        if args.login:
            client.login(open_browser=not args.no_browser)
        prompt = args.prompt or input("Prompt: ")
        for text in client.stream_chat(prompt, model=args.model):
            print(text, end="", flush=True)
        print()
        return 0
    except (WorkBuddyAIError, KeyboardInterrupt) as exc:
        print(f"\n错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
