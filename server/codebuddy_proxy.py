#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.104.0",
#     "uvicorn>=0.24.0",
#     "httpx[socks]>=0.25.0",
#     "requests>=2.31.0",
# ]
# ///
"""Local OpenAI/Responses/Anthropic compatible proxy for CodeBuddy.

High-performance async implementation with FastAPI + httpx.

Features:
- High concurrency: 1000+ concurrent requests
- Low memory footprint: ~5KB per request
- Robust timeout handling with async iterators

Usage with uv:
    uv run codebuddy_proxy.py
    uv run codebuddy_proxy.py --desensitize
    uv run codebuddy_proxy.py --host 0.0.0.0 --port 8787
"""

import argparse
import asyncio
import base64
import gzip
import hashlib
import io
import json
import logging
import logging.handlers
import math
import os
import pathlib
import random
import re
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn
from dsml_parser import DSMLStreamBuffer, parse_all_tool_calls, remove_all_tool_call_markers

from codebuddy_client_demo import CodeBuddyClient, CodeBuddyError

# 尝试导入高级功能模块（可选）
try:
    from desensitize import desensitize_body
    HAS_DESENSITIZE = True
except ImportError:
    HAS_DESENSITIZE = False
    def desensitize_body(body, **kwargs):
        return body

try:
    from responses_projection import project_responses_chat_body
    HAS_PROJECTION = True
except ImportError:
    HAS_PROJECTION = False
    def project_responses_chat_body(body):
        return body, {}

# 导入协议转换器
try:
    from responses_adapter import responses_request_to_chat, ResponsesStreamConverter
    HAS_RESPONSES_ADAPTER = True
except ImportError:
    HAS_RESPONSES_ADAPTER = False
    def responses_request_to_chat(body): 
        raise RuntimeError("responses_adapter not available - cannot convert /v1/responses requests")
    ResponsesStreamConverter = None

try:
    from anthropic_adapter import anthropic_to_chat, AnthropicStreamConverter
    HAS_ANTHROPIC_ADAPTER = True
except ImportError:
    HAS_ANTHROPIC_ADAPTER = False
    def anthropic_to_chat(body): 
        raise RuntimeError("anthropic_adapter not available - cannot convert /v1/messages requests")
    AnthropicStreamConverter = None

# x-device-token 提取器（P1：官方 IDE 指纹设备令牌）
try:
    from extract_device_token import (
        coerce_token_value as _coerce_token_value,
        find_device_token as _find_device_token,
    )
    HAS_DEVICE_TOKEN_EXTRACTOR = True
except ImportError:
    HAS_DEVICE_TOKEN_EXTRACTOR = False
    def _coerce_token_value(raw):
        return raw.strip() if isinstance(raw, str) and raw.strip() else None
    def _find_device_token(**kwargs):
        return None


# ============================================================================
# 日志配置
# ============================================================================

def setup_logging(log_dir: pathlib.Path) -> logging.Logger:
    """配置滚动日志：按天分片，保留30天。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "proxy.log"
    
    logger = logging.getLogger("codebuddy_proxy")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    
    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when='midnight', interval=1, backupCount=30, encoding='utf-8'
    )
    handler.suffix = "%Y-%m-%d"
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger


def now_s() -> int:
    return int(time.time())


# ============================================================================
# 全局状态管理
# ============================================================================

class ProxyState:
    """管理 proxy 的全局状态：认证、日志、配置。"""
    
    def __init__(
        self,
        client: CodeBuddyClient,
        mock_dir: pathlib.Path | None,
        log_file: pathlib.Path | None,
        enable_desensitize: bool = False,
        enable_optimize_context: bool = False,
        verbose_llm: bool = False,
        logger: logging.Logger | None = None,
        device_token: str | None = None,
        rate_qps: float = 1.0,
        rate_burst: int = 5,
        rate_jitter: float = 3.0,
        credit_burn_threshold: float = 0.3,
    ):
        self.client = client
        self.mock_dir = mock_dir
        self.log_file = log_file
        self.enable_desensitize = enable_desensitize
        self.enable_optimize_context = enable_optimize_context
        self.verbose_llm = verbose_llm
        self.logger = logger
        self.started_at = time.time()
        # 官方 IDE 设备指纹令牌（x-device-token）；None 表示不发送该头
        self.device_token = device_token
        # P2：全局限速器（全局单例）+ 429/5xx 熔断器（按 endpoint×model 隔离）
        self.rate_limiter = RateLimiter(rate_qps=rate_qps, burst=rate_burst, jitter=rate_jitter)
        self.breaker = UpstreamCircuitBreaker()
        # P2：消耗速率监控——滑动窗口（最近 10 分钟）(timestamp, 估算消耗)
        self.credit_burn_threshold = credit_burn_threshold
        self._usage_events: deque[tuple[float, float]] = deque()
        self._credits_total_remain: float | None = None  # 最近一次成功获取的剩余额度
        self._credits_cache: dict[str, Any] | None = None  # {"fetched_at": float, "payload": dict}
        self._burn_warned = False
        # 客户端会话键（body 轻量指纹）→ conversation_id，模拟 IDE 会话链路
        self._conversations: dict[str, str] = {}
        # conv_id → 上一轮上游响应 id（previous_response_id 会话延续）
        self._last_response_id: dict[str, str] = {}
        # conv_id → (traceid, 递增计数)：同会话复用 b3 traceid
        self._trace_ids: dict[str, tuple[str, int]] = {}

    def ensure_auth(self) -> None:
        if self.mock_dir is None:
            self.client.ensure_authenticated()

    @staticmethod
    def _conversation_fingerprint(body: dict[str, Any]) -> str:
        """对请求 body 做轻量指纹：首个 system 消息前200字符 + model + tools 数量。"""
        messages = body.get("messages") or []
        system_prefix = ""
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content", "")
                system_prefix = (content if isinstance(content, str) else str(content))[:200]
                break
        tools_count = len(body.get("tools") or [])
        raw = f"{system_prefix}|{body.get('model', '')}|{tools_count}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get_conversation_id(self, body: dict[str, Any]) -> str:
        """相同指纹复用同一 conversation_id；dict 超过 1000 条时清空防内存增长。"""
        if len(self._conversations) > 1000:
            self._conversations.clear()
        key = self._conversation_fingerprint(body)
        conv_id = self._conversations.get(key)
        if not conv_id:
            conv_id = uuid.uuid4().hex
            self._conversations[key] = conv_id
        return conv_id

    def record_response_id(self, conv_id: str, response_id: str) -> None:
        """记录会话最近一次上游响应 id（仅流正常结束时调用）。"""
        if not conv_id or not response_id:
            return
        if len(self._last_response_id) > 1000:
            self._last_response_id.clear()
        self._last_response_id[conv_id] = response_id

    def peek_previous_response_id(self, conv_id: str) -> str | None:
        """读取会话上一轮响应 id（供 forward_chat 注入 previous_response_id）。"""
        return self._last_response_id.get(conv_id)

    def get_trace_id(self, conv_id: str) -> str:
        """同 conv_id 复用同一 b3 traceid（官方 IDE 同会话延续 traceid，spanid 每请求新生成）。"""
        if len(self._trace_ids) > 1000:
            self._trace_ids.clear()
        entry = self._trace_ids.get(conv_id)
        if entry:
            trace_id, count = entry
            self._trace_ids[conv_id] = (trace_id, count + 1)
            return trace_id
        trace_id = _hex32()
        self._trace_ids[conv_id] = (trace_id, 1)
        return trace_id

    def _prune_usage(self, now: float | None = None) -> float:
        """清理 10 分钟窗口外的消耗事件，返回窗口内估算消耗总和。"""
        now = time.time() if now is None else now
        cutoff = now - 600
        while self._usage_events and self._usage_events[0][0] < cutoff:
            self._usage_events.popleft()
        return sum(v for _, v in self._usage_events)

    def _burn_threshold(self) -> float:
        """消耗速率告警阈值：credits 可用时为 total_remain 的比例，否则兜底 500。"""
        if self._credits_total_remain is not None:
            return max(0.0, self._credits_total_remain) * self.credit_burn_threshold
        return 500.0

    def record_burn(self, model: str, usage: dict[str, Any] | None) -> None:
        """记录一次成功上游请求的估算消耗（prompt+completion tokens），只告警不拒绝。"""
        if not isinstance(usage, dict):
            return
        try:
            tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            return
        if tokens <= 0:
            return
        now = time.time()
        self._usage_events.append((now, float(tokens)))
        window = self._prune_usage(now)
        threshold = self._burn_threshold()
        if window > threshold:
            if not self._burn_warned:
                self._burn_warned = True
                diagnostic(
                    "credit_burn_warning",
                    model=model,
                    window_tokens=window,
                    threshold=round(threshold, 2),
                    credits_available=self._credits_total_remain is not None,
                )
        else:
            self._burn_warned = False

    def burn_window_total(self) -> float:
        """当前 10 分钟滑动窗口内的估算消耗总和。"""
        return self._prune_usage()

    def write_log(self, event: str, **kwargs) -> None:
        if self.log_file is None:
            return
        try:
            record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **kwargs}
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
    
    def write_body_log(self, event: str, body: bytes, **kwargs) -> None:
        if self.log_file is None:
            return
        try:
            text = body.decode("utf-8", errors="replace")
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "event": event,
                "body_bytes": len(body),
                "body_text": text,
                **kwargs
            }
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(title="CodeBuddy Proxy (FastAPI)", version="2.0")

# 全局状态（在 main() 中初始化）
proxy_state: ProxyState | None = None


def get_state() -> ProxyState:
    if proxy_state is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "proxy not initialized", "type": "internal_error"}})
    return proxy_state


# ============================================================================
# 辅助函数
# ============================================================================

OFFICIAL_IDE_VERSION = "4.12.0"


def _hex32() -> str:
    return uuid.uuid4().hex


def _hex16() -> str:
    return uuid.uuid4().hex[:16]


class RateLimiter:
    """异步令牌桶全局限速：持续 rate QPS、突发容量 burst。

    acquire() 排队等待而非拒绝——排队是"好公民"行为；令牌到手后再附加随机抖动。
    rate_qps <= 0 表示不限速（仅抖动）。
    """

    def __init__(self, rate_qps: float = 1.0, burst: int = 5, jitter: float = 3.0):
        self.rate_qps = float(rate_qps)
        self.burst = max(1, int(burst))
        self.jitter = max(0.0, float(jitter))
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()
        self._waiting = 0

    def _refill(self) -> None:
        now = time.monotonic()
        if self.rate_qps > 0:
            elapsed = now - self._last_refill
            self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_qps)
        self._last_refill = now

    async def acquire(self) -> float:
        """等待获取一个令牌（外加随机抖动），返回实际等待秒数。"""
        self._waiting += 1
        try:
            waited = 0.0
            while True:
                self._refill()
                if self.rate_qps <= 0:
                    break
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    break
                delay = max((1.0 - self._tokens) / self.rate_qps, 0.005)
                t0 = time.monotonic()
                await asyncio.sleep(delay)
                waited += time.monotonic() - t0
            if self.jitter > 0:
                t0 = time.monotonic()
                await asyncio.sleep(random.uniform(0.0, self.jitter))
                waited += time.monotonic() - t0
            return waited
        finally:
            self._waiting -= 1

    def status(self) -> dict[str, Any]:
        self._refill()
        return {
            "rate_qps": self.rate_qps,
            "burst": self.burst,
            "jitter_seconds": self.jitter,
            "available_tokens": round(self._tokens, 2),
            "queued_requests": self._waiting,
        }


class UpstreamCircuitBreaker:
    """上游熔断器：按 (endpoint, model) 二元组独立熔断。

    - 429：按 details 中的 reset 时间冷却（中英文文案，UTC+8 解析），解析不出默认 300s
    - 连续 5 次 5xx：熔断 120s；200 成功清零连续计数
    - 冷却到期惰性恢复（无后台线程）
    """

    DEFAULT_429_COOLDOWN = 300.0
    DEFAULT_5XX_COOLDOWN = 120.0
    MAX_5XX_STREAK = 5
    _RESET_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}:\d{2}")

    def __init__(self):
        self._trips: dict[tuple[str, str], dict[str, Any]] = {}
        self._streaks: dict[tuple[str, str], int] = {}

    @staticmethod
    def parse_reset_at(text: str | None, now: float | None = None) -> float | None:
        """解析 429 details 中的重置时间为 epoch（文案中的时间按 UTC+8 解释）。

        支持中英文两种实测文案：
          "usage will reset at 2026-09-13 10:46:15 UTC+8"
          "将在 2026-09-13 14:48:28 UTC+8 重置"
        解析不出或时间已过返回 None（调用方回退默认 300s 冷却）。
        """
        if not text:
            return None
        m = UpstreamCircuitBreaker._RESET_RE.search(text)
        if not m:
            return None
        try:
            dt = datetime.strptime(m.group(0).replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        epoch = dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
        if now is None:
            now = time.time()
        return epoch if epoch > now else None

    @staticmethod
    def _key(endpoint: str, model: str) -> tuple[str, str]:
        return (endpoint or "", model or "")

    def trip(self, endpoint: str, model: str, reset_at: float | None = None, reason: str = "") -> float:
        """记录熔断；reset_at 缺省时默认从现在起冷却 300 秒。返回冷却总时长。"""
        if reset_at is None:
            reset_at = time.time() + self.DEFAULT_429_COOLDOWN
        self._trips[self._key(endpoint, model)] = {
            "reset_at": float(reset_at),
            "reason": reason,
            "tripped_at": time.time(),
        }
        return max(0.0, float(reset_at) - time.time())

    def check(self, endpoint: str, model: str) -> tuple[bool, float]:
        """返回 (是否熔断中, 剩余秒数)；冷却到期惰性恢复并清理条目。"""
        key = self._key(endpoint, model)
        entry = self._trips.get(key)
        if not entry:
            return False, 0.0
        remaining = entry["reset_at"] - time.time()
        if remaining <= 0:
            del self._trips[key]
            return False, 0.0
        return True, remaining

    def note_success(self, endpoint: str, model: str) -> None:
        """200 成功：清零 5xx 连续计数（不清除已熔断的冷却条目）。"""
        self._streaks.pop(self._key(endpoint, model), None)

    def note_5xx(self, endpoint: str, model: str) -> tuple[bool, float]:
        """5xx 记账：连续第 5 次触发熔断 120 秒。返回 (是否触发熔断, 冷却秒数)。"""
        key = self._key(endpoint, model)
        streak = self._streaks.get(key, 0) + 1
        self._streaks[key] = streak
        if streak >= self.MAX_5XX_STREAK:
            self._streaks[key] = 0
            self.trip(endpoint, model, time.time() + self.DEFAULT_5XX_COOLDOWN,
                      reason=f"consecutive {self.MAX_5XX_STREAK} 5xx responses")
            return True, self.DEFAULT_5XX_COOLDOWN
        return False, 0.0

    def snapshot(self) -> list[dict[str, Any]]:
        """当前熔断状态（活跃冷却 + 进行中的 5xx 计数），过期条目惰性清理。"""
        now = time.time()
        out: list[dict[str, Any]] = []
        for key in list(self._trips):
            entry = self._trips[key]
            remaining = entry["reset_at"] - now
            if remaining <= 0:
                del self._trips[key]
                continue
            endpoint, model = key
            out.append({
                "endpoint": endpoint,
                "model": model,
                "tripped": True,
                "reason": entry["reason"],
                "reset_at": entry["reset_at"],
                "reset_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(entry["reset_at"])),
                "remaining_seconds": round(remaining, 1),
                "streak_5xx": self._streaks.get(key, 0),
            })
        for key, streak in self._streaks.items():
            if streak > 0 and key not in self._trips:
                endpoint, model = key
                out.append({
                    "endpoint": endpoint,
                    "model": model,
                    "tripped": False,
                    "reason": "",
                    "reset_at": None,
                    "reset_at_iso": None,
                    "remaining_seconds": 0.0,
                    "streak_5xx": streak,
                })
        return out

    def clear(self, model: str | None = None) -> int:
        """手动清除熔断：model 指定仅清该模型，None 清全部。返回清除的键数。"""
        keys = set()
        for store in (self._trips, self._streaks):
            for key in store:
                if model is None or key[1] == model:
                    keys.add(key)
        for key in keys:
            self._trips.pop(key, None)
            self._streaks.pop(key, None)
        return len(keys)


def official_ide_headers(model: str, conversation_id: str, platform: str, device_token: str = "") -> dict:
    """构造官方 IDE 指纹 headers。

    platform 为 "workbuddy-ai" 时 x-domain 用 www.workbuddy.ai，
    否则 www.codebuddy.cn。ide-name/type 保持 CodeBuddyIDE。

    x-device-token 预留接口：下阶段由提取脚本注入，本阶段为空时不发送。
    """
    domain = "www.workbuddy.ai" if platform == "workbuddy-ai" else "www.codebuddy.cn"
    headers = {
        "user-agent": f"CodeBuddyIDE/{OFFICIAL_IDE_VERSION} CodeBuddy/{OFFICIAL_IDE_VERSION}",
        "accept": "*/*",
        "accept-language": "*",
        "sec-fetch-mode": "cors",
        "x-requested-with": "XMLHttpRequest",
        "x-ide-name": "CodeBuddyIDE",
        "x-ide-type": "CodeBuddyIDE",
        "x-ide-version": OFFICIAL_IDE_VERSION,
        "x-product": "SaaS",
        "x-product-code": "codebuddy",
        "x-product-version": OFFICIAL_IDE_VERSION,
        "x-env-id": "production",
        "x-domain": domain,
        "x-agent-intent": "craft",
        "x-model-id": model,
        "x-conversation-id": conversation_id,
    }
    if device_token:
        headers["x-device-token"] = device_token
    return headers


def gzip_body_if_large(body_bytes: bytes, threshold: int = 4096) -> tuple[bytes, dict]:
    """body 超过 threshold 时 gzip 压缩，返回 (数据, {"Content-Encoding": "gzip"})"""
    if len(body_bytes) <= threshold:
        return body_bytes, {}
    return gzip.compress(body_bytes), {"Content-Encoding": "gzip"}


def _prepare_upstream_payload(body: dict[str, Any]) -> tuple[bytes, dict]:
    """序列化上游 body，超过阈值时 gzip（返回 (payload, extra_headers)）。"""
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return gzip_body_if_large(raw)


def _extract_upstream_response_id(chunk: dict[str, Any]) -> str | None:
    """从上游 SSE data JSON 中提取响应 id（cmb- 前缀，或 responses 协议的 response.id）。

    cmb-xxx 是"上一轮响应 id"（每轮都变），与会话级 conversation_id 语义不同，勿混用。
    """
    if not isinstance(chunk, dict):
        return None
    response = chunk.get("response")
    if isinstance(response, dict):
        rid = response.get("id")
        if isinstance(rid, str) and rid:
            return rid
    rid = chunk.get("id")
    if isinstance(rid, str) and rid.startswith("cmb-"):
        return rid
    return None


def resolve_device_token(cli_value: str | None = None) -> tuple[str | None, str]:
    """解析 x-device-token：CLI 参数 > 环境变量 CODEBUDDY_DEVICE_TOKEN > 自动发现。

    CLI/env 值可以是裸 token、JSON（{"token": ...}）或指向此类文件的路径。
    返回 (token, source)；找不到返回 (None, "")。
    """
    for source_name, raw in (
        ("cli:--device-token", cli_value),
        ("env:CODEBUDDY_DEVICE_TOKEN", os.getenv("CODEBUDDY_DEVICE_TOKEN")),
    ):
        token = _coerce_token_value(raw)
        if token:
            return token, source_name
    try:
        found = _find_device_token()
    except Exception:
        found = None
    if found and found.get("token"):
        return str(found["token"]), str(found.get("source") or "auto-discovery")
    return None, ""


def body_summary(body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    message_summary = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            content_length = len(content)
            content_type = "text"
        elif isinstance(content, list):
            content_length = sum(
                len(str(part.get("text", ""))) for part in content if isinstance(part, dict)
            )
            content_type = "parts"
        else:
            content_length = 0
            content_type = type(content).__name__
        message_summary.append({
            "role": item.get("role"),
            "content_type": content_type,
            "content_length": content_length,
        })
    return {
        "model": body.get("model"),
        "stream": bool(body.get("stream")),
        "message_count": len(messages),
        "messages": message_summary,
        "tool_count": len(body.get("tools") or []),
    }


# 安全词检测统一关键词（中英混合）
_SAFETY_KEYWORDS = ("sensitive", "cannot respond", "敏感内容", "无法响应", "unable to")

def is_policy_blocked(text: str) -> bool:
    """检测文本是否包含安全策略拦截标记"""
    return any(marker in text.lower() for marker in _SAFETY_KEYWORDS)


def text_summary(value: str) -> dict[str, Any]:
    return {
        "content_length": len(value),
        "content_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": is_policy_blocked(value),
    }


def diagnostic(event: str, **kwargs) -> None:
    """输出诊断日志到 logger。"""
    state = get_state()
    if state.logger:
        state.logger.info(f"{event}: {json.dumps(kwargs, ensure_ascii=False)}")


def log_client_request(method: str, path: str, body: dict[str, Any] | None) -> None:
    """Log client request with verbosity control."""
    state = get_state()
    
    if state.verbose_llm:
        state.write_log("client_request", method=method, path=path, body=body)
    else:
        if body:
            summary = body_summary(body)
            state.write_log("client_request_summary", method=method, path=path, **summary)
        else:
            state.write_log("client_request_summary", method=method, path=path)


def log_upstream_request(protocol: str, body: dict[str, Any]) -> None:
    """Log upstream request with verbosity control."""
    state = get_state()
    
    if state.verbose_llm:
        state.write_log("upstream_request", protocol=protocol,
                       method="POST", path="/v2/chat/completions", body=body)
        diagnostic("upstream_request", protocol=protocol, **body_summary(body))
    else:
        messages = body.get("messages", [])
        total_chars = sum(
            len(str(m.get("content", "")))
            for m in messages
            if isinstance(m, dict)
        )
        summary = {
            "model": body.get("model"),
            "message_count": len(messages),
            "tool_count": len(body.get("tools", [])),
            "stream": bool(body.get("stream")),
            "total_chars": total_chars
        }
        state.write_log("upstream_request_summary", protocol=protocol, **summary)
        diagnostic("upstream_request_summary", protocol=protocol, **summary)


def log_upstream_response(protocol: str, text: str, **stats) -> None:
    """Log upstream response with verbosity control."""
    state = get_state()
    
    common = {
        "protocol": protocol,
        "content_length": len(text),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "safety_message_detected": is_policy_blocked(text),
        **stats
    }
    
    if state.verbose_llm:
        common["content_preview"] = text[:200] if text else ""
    
    diagnostic("response", **common)
    state.write_log("stream_completed" if stats.get("stream") else "response",
                   **{k: v for k, v in common.items() 
                      if k not in ("content_preview",)})


# ============================================================================
# 端点：/health
# ============================================================================

@app.get("/health")
async def health():
    state = get_state()
    auth = {} if state.mock_dir is not None else (state.client.session.get("auth") or {})
    expires = int(auth.get("expiresAt") or 0)
    return {
        "status": "ok",
        "authenticated": bool(auth.get("accessToken")),
        "token_valid": not expires or expires > int(time.time() * 1000),
        "uptime_seconds": int(time.time() - state.started_at),
        "rate_limiter": state.rate_limiter.status(),
    }


# ============================================================================
# 端点：/v1/models
# ============================================================================

@app.get("/v1/models")
async def list_models():
    state = get_state()
    log_client_request("GET", "/v1/models", None)
    state.ensure_auth()
    
    endpoint = state.client.endpoint.lower()
    overseas = (
        "workbuddy" in endpoint
        or "codebuddy.ai" in endpoint
        or "workbuddy.ai" in endpoint
        or state.client.platform.lower() == "workbuddy-ai"
    )
    
    if overseas:
        # 海外 WorkBuddy AI 模型（实测可用，共 21 个）
        models = [
            {"id": "default-model", "name": "Auto", "vendor": "codebuddy"},
            {"id": "fast-model", "name": "Fast", "vendor": "codebuddy"},
            {"id": "balanced-model", "name": "Balanced", "vendor": "codebuddy"},
            {"id": "primary-model", "name": "Primary", "vendor": "codebuddy"},
            {"id": "deep-model", "name": "Deep", "vendor": "codebuddy"},
            {"id": "hy4-preview", "name": "Hy4 preview", "vendor": "tencent"},
            {"id": "hy3", "name": "Hy3", "vendor": "tencent"},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "vendor": "openai"},
            {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra", "vendor": "openai"},
            {"id": "gpt-5.6-luna", "name": "GPT-5.6-Luna", "vendor": "openai"},
            {"id": "gpt-5.5", "name": "GPT-5.5", "vendor": "openai"},
            {"id": "gpt-5.4", "name": "GPT-5.4", "vendor": "openai"},
            {"id": "gpt-5.3-codex", "name": "GPT-5.3-Codex", "vendor": "openai"},
            {"id": "gemini-3.5-flash", "name": "Gemini-3.5-Flash", "vendor": "google"},
            {"id": "glm-5.3", "name": "GLM-5.3", "vendor": "zhipu"},
            {"id": "glm-5.2", "name": "GLM-5.2", "vendor": "zhipu"},
            {"id": "kimi-k3", "name": "Kimi-K3", "vendor": "moonshot"},
            {"id": "kimi-k2.6", "name": "Kimi-K2.6", "vendor": "moonshot"},
            {"id": "kimi-k2.5", "name": "Kimi-K2.5", "vendor": "moonshot"},
            {"id": "minimax-m3", "name": "MiniMax-M3", "vendor": "minimax"},
            {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash", "vendor": "deepseek"},
        ]
    else:
        # 国内 CodeBuddy 模型（产品配置 + 实测补充，共 27 个）
        models = [
            {"id": "auto", "name": "Auto", "vendor": "codebuddy"},
            {"id": "default", "name": "Default", "vendor": "codebuddy"},
            {"id": "hy4-preview", "name": "Hy4 preview", "vendor": "tencent"},
            {"id": "hy3", "name": "Hy3", "vendor": "tencent"},
            {"id": "hunyuan-chat", "name": "Hunyuan-Turbos", "vendor": "tencent"},
            {"id": "glm-5.3", "name": "GLM-5.3", "vendor": "zhipu"},
            {"id": "glm-5.3-flash", "name": "GLM-5.3-flash", "vendor": "zhipu"},
            {"id": "glm-5.2", "name": "GLM-5.2", "vendor": "zhipu"},
            {"id": "glm-5.1", "name": "GLM-5.1", "vendor": "zhipu"},
            {"id": "glm-5v-turbo", "name": "GLM-5v-Turbo", "vendor": "zhipu"},
            {"id": "kimi-k3", "name": "Kimi-K3", "vendor": "moonshot"},
            {"id": "kimi-k3-1", "name": "Kimi-K3.1", "vendor": "moonshot"},
            {"id": "kimi-k2.7", "name": "Kimi-K2.7-Code", "vendor": "moonshot"},
            {"id": "kimi-k2.6", "name": "Kimi-K2.6", "vendor": "moonshot"},
            {"id": "minimax-m3", "name": "MiniMax-M3", "vendor": "minimax"},
            {"id": "minimax-m2.7", "name": "MiniMax-M2.7", "vendor": "minimax"},
            {"id": "deepseek-v4-pro", "name": "Deepseek-V4-Pro", "vendor": "deepseek"},
            {"id": "deepseek-v4-flash", "name": "Deepseek-V4-Flash", "vendor": "deepseek"},
            {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash", "vendor": "deepseek"},
        ]
    
    data = [
        {
            "id": m["id"],
            "object": "model",
            "created": 0,
            "owned_by": m.get("vendor", "codebuddy"),
            "name": m.get("name", m["id"]),
        }
        for m in models
    ]
    
    return {"object": "list", "data": data}


# ============================================================================
# 端点：/v1/breaker（熔断状态运维）
# ============================================================================

@app.get("/v1/breaker")
async def breaker_status():
    state = get_state()
    breakers = state.breaker.snapshot()
    return {"breakers": breakers, "count": len(breakers)}


@app.delete("/v1/breaker")
async def breaker_clear(model: str | None = None):
    state = get_state()
    cleared = state.breaker.clear(model)
    diagnostic("circuit_breaker_cleared", model=model, cleared=cleared)
    return {"cleared": cleared, "model": model}


# ============================================================================
# 端点：/v1/credits（额度监控）
# ============================================================================

def _parse_credits_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """解析 billing/meter/get-user-resource 响应 → accounts 明细 + 汇总；无 Accounts 返回 None。"""
    if not isinstance(payload, dict):
        return None
    accounts_raw = payload.get("Accounts")
    if accounts_raw is None and isinstance(payload.get("data"), dict):
        accounts_raw = payload["data"].get("Accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        return None

    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    items = []
    total_remain = 0.0
    total_size = 0.0
    for a in accounts_raw:
        if not isinstance(a, dict):
            continue
        remain = _f(a.get("CapacityRemainPrecise"))
        size = _f(a.get("CapacitySizePrecise"))
        total_remain += remain
        total_size += size
        cycle = a.get("CycleEndTime")
        items.append({
            "PackageName": a.get("PackageName"),
            "CapacityRemainPrecise": remain,
            "CapacitySizePrecise": size,
            "CycleEndTime": str(cycle) if cycle is not None else None,
        })
    usage_ratio = ((total_size - total_remain) / total_size) if total_size > 0 else 0.0
    usage_ratio = max(0.0, min(1.0, usage_ratio))
    return {
        "accounts": items,
        "total_remain": total_remain,
        "total_size": total_size,
        "usage_ratio": round(usage_ratio, 4),
    }


async def _fetch_credits(state: ProxyState) -> dict[str, Any]:
    """调用计费接口；403（国内 code 10085）或网络错误时返回 unavailable 结果。"""
    url = state.client.endpoint + "/billing/meter/get-user-resource"
    fetched_at = int(time.time())
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=30.0), trust_env=False) as client:
            resp = await client.post(
                url,
                headers={**state.client.auth_headers(), "Content-Type": "application/json"},
                json={},
            )
    except httpx.HTTPError as exc:
        return {"available": False, "status": "unavailable", "error": str(exc)[:200],
                "fetched_at": fetched_at}
    text = resp.text[:500]
    if resp.status_code != 200:
        return {"available": False, "status": "unavailable", "http_status": resp.status_code,
                "detail": text, "fetched_at": fetched_at}
    try:
        payload = resp.json()
    except ValueError:
        return {"available": False, "status": "unavailable", "http_status": resp.status_code,
                "detail": text, "fetched_at": fetched_at}
    parsed = _parse_credits_payload(payload)
    if parsed is None:
        return {"available": False, "status": "unavailable", "http_status": resp.status_code,
                "detail": text, "fetched_at": fetched_at}
    return {"available": True, "status": "ok", **parsed, "fetched_at": fetched_at}


@app.get("/v1/credits")
async def get_credits(refresh: int = 0):
    """额度查询：结果缓存 60 秒（避免频繁打计费接口触发风控），?refresh=1 强制刷新。"""
    state = get_state()
    state.ensure_auth()

    now = time.time()
    cached = state._credits_cache
    if not refresh and cached and (now - cached["fetched_at"]) < 60:
        data = dict(cached["payload"])
        from_cache = True
    else:
        data = await _fetch_credits(state)
        state._credits_cache = {"fetched_at": now, "payload": data}
        from_cache = False
        if data.get("available") and data.get("total_remain") is not None:
            state._credits_total_remain = float(data["total_remain"])

    data = dict(data)
    data["cached"] = from_cache

    # 消耗速率告警（实时计算；只告警不拒绝）
    window = state.burn_window_total()
    if data.get("available") and data.get("total_remain") is not None:
        threshold = float(data["total_remain"]) * state.credit_burn_threshold
    else:
        threshold = 500.0
    data["burn_warning"] = window > threshold

    if not data.get("available"):
        # 国内端点 403 等场景：只返回本地已知信息并标注 unavailable
        data["local"] = {
            "endpoint": state.client.endpoint,
            "platform": state.client.platform,
            "window_burn_tokens": window,
            "burn_threshold": threshold,
            "credit_burn_threshold": state.credit_burn_threshold,
            "window_seconds": 600,
        }
    return data


# ============================================================================
# 端点：/v1/chat/completions
# ============================================================================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    state = get_state()
    body = await request.json()
    
    log_client_request("POST", "/v1/chat/completions", body)
    diagnostic("request", protocol="openai", **body_summary(body))
    
    return await forward_chat(body, "openai")


# ============================================================================
# 端点：/v1/responses
# ============================================================================

@app.post("/v1/responses")
async def create_response(request: Request):
    state = get_state()
    body = await request.json()
    
    log_client_request("POST", "/v1/responses", body)
    
    # 转换 Responses → Chat
    chat_body = responses_request_to_chat(body)
    
    # 消息压缩优化（如果启用）
    if state.enable_optimize_context and HAS_PROJECTION:
        chat_body, proj_stats = project_responses_chat_body(chat_body)
        diagnostic("projection_applied", protocol="responses", **proj_stats)
    
    diagnostic("request", protocol="responses", **body_summary(chat_body))
    
    return await forward_chat(chat_body, "responses", original=body)


# ============================================================================
# 端点：/v1/messages
# ============================================================================

@app.post("/v1/messages")
async def create_message(request: Request):
    state = get_state()
    body = await request.json()
    
    log_client_request("POST", "/v1/messages", body)
    
    # 转换 Anthropic → Chat
    chat_body = anthropic_to_chat(body)
    diagnostic("request", protocol="anthropic", **body_summary(chat_body))
    
    return await forward_chat(chat_body, "anthropic", original=body)


# ============================================================================
# 核心：转发请求到上游
# ============================================================================

async def forward_chat(
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None = None
) -> StreamingResponse | JSONResponse:
    """转发 chat 请求到 CodeBuddy 上游，支持流式和非流式。"""
    state = get_state()
    state.ensure_auth()
    
    stream = bool(body.get("stream"))
    upstream_body = dict(body)
    conv_id = state.get_conversation_id(upstream_body)
    
    diagnostic("upstream_request", protocol=protocol, conv_id=conv_id, **body_summary(body))
    
    # P2 熔断：冷却期内快速失败，不打上游（429 与 5xx 熔断统一拒绝格式）
    model = body.get("model", "")
    tripped, remaining = state.breaker.check(state.client.endpoint, model)
    if tripped:
        seconds = max(1, math.ceil(remaining))
        diagnostic("circuit_breaker_reject", endpoint=state.client.endpoint, model=model,
                   remaining_seconds=round(remaining, 1))
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"熔断中，剩余 {seconds}s（上游限频），请稍后重试或切换模型",
                    "type": "circuit_breaker",
                    "retry_after": seconds,
                }
            },
            headers={"Retry-After": str(seconds)},
        )
    
    # 海外 WorkBuddy AI 要求首条消息必须是 system prompt
    endpoint = state.client.endpoint.lower()
    overseas = (
        "workbuddy" in endpoint
        or "codebuddy.ai" in endpoint
        or "workbuddy.ai" in endpoint
        or state.client.platform.lower() == "workbuddy-ai"
    )
    if overseas:
        messages = upstream_body.get("messages") or []
        if not messages or messages[0].get("role") != "system":
            upstream_body["messages"] = [
                {"role": "system", "content": "You are a helpful assistant."},
                *messages,
            ]
    
    # 限制 tools 数量防止上游拒绝 (CodeBuddy 限制约 30-50 个工具)
    original_tool_count = len(upstream_body.get("tools", []))
    MAX_TOOLS = 30
    if original_tool_count > MAX_TOOLS:
        upstream_body["tools"] = upstream_body["tools"][:MAX_TOOLS]
        diagnostic("tools_truncated", 
                  original_count=original_tool_count,
                  truncated_count=MAX_TOOLS,
                  reason="Upstream API tool limit")
    
    # 官方 IDE 默认 max_tokens（客户端未指定时）
    if not upstream_body.get("max_tokens"):
        upstream_body["max_tokens"] = 393216
    
    # 应用脱敏处理
    if state.enable_desensitize:
        upstream_body = desensitize_body(upstream_body, compact_harness=True)
    
    # 始终以流式方式请求上游（聚合或转发）
    upstream_body["stream"] = True
    upstream_body.setdefault("stream_options", {"include_usage": True})
    
    # previous_response_id 会话延续：客户端未显式指定时，链式传递上一轮上游响应 id
    prev_resp_id = state.peek_previous_response_id(conv_id)
    if prev_resp_id and not upstream_body.get("previous_response_id"):
        upstream_body["previous_response_id"] = prev_resp_id
    
    
    url = state.client.endpoint + "/v2/chat/completions"
    model = upstream_body.get("model", "")
    finger_headers = official_ide_headers(
        model, conv_id, state.client.platform, device_token=state.device_token or ""
    )
    msg_id = _hex32()
    req_id = _hex32()
    trace_id = state.get_trace_id(conv_id)
    span_id = _hex16()
    
    base_headers = {
        **state.client.auth_headers(),
        "Content-Type": "application/json",
        "user-agent": f"CodeBuddyIDE/{OFFICIAL_IDE_VERSION} CodeBuddy/{OFFICIAL_IDE_VERSION}",
        "x-ide-name": "CodeBuddyIDE",
        "x-ide-type": "CodeBuddyIDE",
        "x-ide-version": OFFICIAL_IDE_VERSION,
        "x-product": "SaaS",
        "x-product-code": "codebuddy",
        "x-product-version": OFFICIAL_IDE_VERSION,
        "x-env-id": "production",
        "x-agent-intent": "craft",
        "x-model-id": model,
        "x-conversation-id": conv_id,
        "x-conversation-message-id": msg_id,
        "x-conversation-request-id": req_id,
        "x-request-id": req_id,
        "x-request-trace-id": str(uuid.uuid4()),
        "x-b3-traceid": trace_id,
        "x-b3-spanid": span_id,
        "x-b3-sampled": "1",
        "b3": f"{trace_id}-{span_id}-1",
        "x-requested-with": "XMLHttpRequest",
        "accept": "*/*",
        "sec-fetch-mode": "cors",
        "monitor_httpsendtime": str(int(time.time() * 1000)),
    }
    # 合并指纹 headers：跳过与 base_headers 大小写冲突的键（避免 httpx 双发拼接）
    base_keys_lower = {k.lower() for k in base_headers}
    for k, v in finger_headers.items():
        if k.lower() not in base_keys_lower:
            base_headers[k] = v
    headers = base_headers
    
    # P2 全局限速 + 随机抖动：排队等待而非拒绝（发起上游请求前）
    await state.rate_limiter.acquire()
    
    if stream:
        # 流式：直接转发
        return StreamingResponse(
            stream_upstream(url, headers, upstream_body, protocol, original),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "close"}
        )
    else:
        # 非流式：聚合后返回
        collected = await collect_upstream(url, headers, upstream_body, protocol)
        return JSONResponse(content=convert_nonstream(collected, protocol, original))


# ============================================================================
# 异步流式转发（核心改进）
# ============================================================================

async def stream_upstream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None
):
    """异步流式转发上游响应到客户端。
    
    关键改进：
    1. 使用 httpx.AsyncClient 异步请求
    2. aiter_lines() 自动处理行分割和超时
    3. 记录流开始/进度/完成日志
    """
    state = get_state()
    stream_start_time = time.time()
    conv_id = (headers or {}).get("x-conversation-id") or ""
    
    # 【日志】流开始
    state.write_log("stream_started", protocol=protocol, timestamp=stream_start_time)
    diagnostic("stream_started", protocol=protocol)
    
    
    response_id = "resp_" + uuid.uuid4().hex
    anthropic_state = AnthropicStreamConverter(
        (original or {}).get("model", "default")
    ) if protocol == "anthropic" and AnthropicStreamConverter else None
    
    # Responses 协议转换器
    responses_state = ResponsesStreamConverter(
        model=upstream_body.get("model", "auto")
    ) if protocol == "responses" and ResponsesStreamConverter else None
    
    # DSML 缓冲区（用于处理可能的文本标记格式工具调用）
    dsml_buffer = DSMLStreamBuffer()
    
    emitted_response_created = False
    response_text = ""
    response_text_started = False
    chunk_count = 0
    done_seen = False
    upstream_response_id: str | None = None
    upstream_usage: dict[str, Any] | None = None
    raw_chunks: list[bytes] = []
    last_progress_log = stream_start_time
    detected_tool_calls = []
    try:
        # 异步HTTP客户端：timeout=None 依赖TCP超时
        # 使用合理的超时配置：连接超时30s，读取超时300s
        timeout_config = httpx.Timeout(30.0, read=300.0)
        payload, extra_headers = _prepare_upstream_payload(body)
        request_headers = {**headers, **extra_headers}
        async with httpx.AsyncClient(timeout=timeout_config, trust_env=False) as client:
            async with client.stream("POST", url, headers=request_headers, content=payload) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", "replace")
                    endpoint = state.client.endpoint
                    model = body.get("model", "")
                    retry_after: int | None = None
                    if resp.status_code == 429:
                        reset_at = UpstreamCircuitBreaker.parse_reset_at(error_text)
                        state.breaker.trip(endpoint, model, reset_at, reason="upstream 429 rate limit")
                        _, rem = state.breaker.check(endpoint, model)
                        retry_after = max(1, math.ceil(rem))
                    elif resp.status_code >= 500:
                        state.breaker.note_5xx(endpoint, model)
                    
                    diagnostic("upstream_error", protocol=protocol,
                        status=resp.status_code,
                        detail=error_text[:500])
                    
                    # 返回结构化错误（包含详细信息）
                    if protocol == "anthropic":
                        # Anthropic error format
                        error_event = {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": f"Upstream API error (HTTP {resp.status_code}): {error_text[:200]}"
                            }
                        }
                        if retry_after is not None:
                            error_event["error"]["retry_after"] = retry_after
                        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n".encode()
                    else:
                        # OpenAI error format
                        error_chunk = {
                            "error": {
                                "message": f"Upstream API error (HTTP {resp.status_code})",
                                "type": "upstream_error",
                                "code": resp.status_code,
                                "details": error_text[:500]
                            }
                        }
                        if retry_after is not None:
                            error_chunk["error"]["retry_after"] = retry_after
                        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
                    
                    return
                
                state.breaker.note_success(state.client.endpoint, body.get("model", ""))
                diagnostic("upstream_response", protocol=protocol, status=resp.status_code)
                
                # 异步迭代行（自动处理超时和分块）
                async for line in resp.aiter_lines():
                    # 【日志】进度记录
                    if chunk_count > 0 and chunk_count % 10 == 0:
                        now = time.time()
                        if now - last_progress_log >= 5:
                            diagnostic("stream_progress", protocol=protocol,
                                chunks=chunk_count,
                                elapsed=round(now - stream_start_time, 2))
                            last_progress_log = now
                    
                    line = line.strip()
                    raw_chunks.append(line.encode("utf-8"))
                    
                    if not line.startswith("data:"):
                        continue
                    
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_seen = True
                        break
                    
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    
                    chunk_count += 1
                    rid = _extract_upstream_response_id(chunk)
                    if rid:
                        upstream_response_id = rid
                    # P2 消耗监控：include_usage 的末尾 chunk 携带 usage，顺带提取不额外缓冲
                    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                        upstream_usage = chunk["usage"]
                    
                    # 根据协议转换事件
                    if protocol == "openai":
                        # 提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )
                        
                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content
                            
                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)
                            
                            # 修改 chunk 中的 content 为清理后的内容
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                chunk["choices"][0]["delta"]["content"] = cleaned_content
                        
                        # 如果检测到工具调用，添加 tool_calls 字段
                        if detected_tool_calls and dsml_buffer.should_emit_tool_calls():
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                chunk["choices"][0]["finish_reason"] = "tool_calls"
                                # 将检测到的工具调用转换为 OpenAI 格式
                                chunk["choices"][0]["delta"]["tool_calls"] = [
                                    {
                                        "index": idx,
                                        "id": f"call_{uuid.uuid4().hex[:24]}",
                                        "type": "function",
                                        "function": {
                                            "name": tc["name"],
                                            "arguments": json.dumps(tc["input"], ensure_ascii=False)
                                        }
                                    }
                                    for idx, tc in enumerate(detected_tool_calls)
                                ]
                        
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
                    
                    elif protocol == "responses" and responses_state:
                        # 使用 ResponsesStreamConverter 转换事件
                        events = responses_state.feed_chunk(chunk)
                        for event_name, event_data in events:
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                    elif protocol == "anthropic" and anthropic_state:
                        # 提取 content 并通过 DSML 缓冲区处理
                        chunk_content = str(
                            ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                        )
                        
                        if chunk_content:
                            # 使用 DSML 缓冲区处理（清理标记，检测工具调用）
                            cleaned_content, chunk_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 累积清理后的文本
                            if cleaned_content:
                                response_text += cleaned_content
                            
                            # 记录检测到的工具调用
                            if chunk_tool_calls:
                                detected_tool_calls.extend(chunk_tool_calls)
                            
                            # ✅ 关键修复：在传递给 AnthropicStreamConverter 之前，先修改 chunk
                            if "choices" in chunk and len(chunk["choices"]) > 0:
                                if "delta" not in chunk["choices"][0]:
                                    chunk["choices"][0]["delta"] = {}
                                # 使用清理后的内容替换原始内容
                                chunk["choices"][0]["delta"]["content"] = cleaned_content
                            
                            # 如果检测到工具调用，添加 tool_calls 字段
                            if chunk_tool_calls and dsml_buffer.should_emit_tool_calls():
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    chunk["choices"][0]["finish_reason"] = "tool_calls"
                                    chunk["choices"][0]["delta"]["tool_calls"] = [
                                        {
                                            "index": idx,
                                            "id": call.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                                            "type": "function",
                                            "function": {
                                                "name": call["function"]["name"],
                                                "arguments": call["function"]["arguments"]
                                            }
                                        }
                                        for idx, call in enumerate(chunk_tool_calls)
                                    ]
                        
                        # 转换为 Anthropic 事件（此时 chunk 已经被清理）
                        events = anthropic_state.feed_chunk(chunk)
                        for event_name, event_data in events:
                            yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                
                # 发送结束事件
                if protocol == "responses" and response_text_started:
                    for event_name, payload in [
                        ("response.output_text.done", {
                            "type": "response.output_text.done",
                            "item_id": response_id,
                            "output_index": 0,
                            "content_index": 0,
                            "text": response_text
                        }),
                        ("response.content_part.done", {
                            "type": "response.content_part.done",
                            "item_id": response_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": response_text, "annotations": []}
                        }),
                        ("response.output_item.done", {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": {
                                "id": response_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": response_text, "annotations": []}]
                            }
                        }),
                        ("response.completed", {
                            "type": "response.completed",
                            "response": {
                                "id": response_id,
                                "object": "response",
                                "status": "completed",
                                "output_text": response_text
                            }
                        }),
                    ]:
                        yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                
                elif protocol == "anthropic" and anthropic_state:
                    for event_name, event_data in anthropic_state.finish():
                        yield f"event: {event_name}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n".encode()
                
                elif protocol == "openai":
                    yield b"data: [DONE]\n\n"
    
    except httpx.TimeoutException as exc:
        # 【日志】超时
        diagnostic("stream_timeout", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_timeout", protocol=protocol, chunks=chunk_count, error=str(exc))
        error_chunk = {
            "error": {
                "message": f"stream timeout after {chunk_count} chunks",
                "type": "timeout_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
    
    except Exception as exc:
        # 【日志】其他错误
        diagnostic("stream_error", protocol=protocol, chunks=chunk_count,
            elapsed=round(time.time() - stream_start_time, 2), error=str(exc))
        state.write_log("stream_error", protocol=protocol, chunks=chunk_count, error=str(exc))
        error_chunk = {
            "error": {
                "message": f"stream error: {exc}",
                "type": "internal_error"
            }
        }
        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n".encode()
    
    finally:
        # previous_response_id 会话延续：仅流正常结束（upstream_done）时记录
        if done_seen and upstream_response_id and conv_id:
            state.record_response_id(conv_id, upstream_response_id)
        
        # P2 消耗监控：流正常结束才计入滑动窗口
        if done_seen and upstream_usage:
            state.record_burn(body.get("model", ""), upstream_usage)
        
        # 【日志】流完成
        if state.verbose_llm:
            raw_response = b"\n".join(raw_chunks)
            state.write_body_log("upstream_response", raw_response, protocol=protocol,
                                status=200, method="POST", path="/v2/chat/completions")
        
        logged_text = anthropic_state.text if anthropic_state else response_text
        stream_duration = round(time.time() - stream_start_time, 2)
        
        log_upstream_response(protocol, logged_text, stream=True,
                            chunk_count=chunk_count, duration=stream_duration,
                            upstream_done=done_seen)


# ============================================================================
# 异步聚合流式响应
# ============================================================================

async def collect_upstream(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    protocol: str
) -> dict[str, Any]:
    """聚合上游流式响应为单个 JSON 对象（非流式场景）。"""
    state = get_state()
    conv_id = (headers or {}).get("x-conversation-id") or ""
    
    usage = None
    finish_reason = None
    content = ""
    upstream_response_id: str | None = None
    tool_calls_dict: dict[int, dict] = {}  # 使用 dict 按 index 累加
    # DSML 缓冲区
    dsml_buffer = DSMLStreamBuffer()
    
    try:
        # 使用合理的超时配置：连接超时30s，读取超时300s
        timeout_config = httpx.Timeout(30.0, read=300.0)
        payload, extra_headers = _prepare_upstream_payload(body)
        request_headers = {**headers, **extra_headers}
        async with httpx.AsyncClient(timeout=timeout_config, trust_env=False) as client:
            async with client.stream("POST", url, headers=request_headers, content=payload) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", "replace")
                    endpoint = state.client.endpoint
                    model = body.get("model", "")
                    if resp.status_code == 429:
                        reset_at = UpstreamCircuitBreaker.parse_reset_at(error_text)
                        state.breaker.trip(endpoint, model, reset_at, reason="upstream 429 rate limit")
                        _, rem = state.breaker.check(endpoint, model)
                        raise HTTPException(status_code=429, detail={"error": {
                            "message": error_text[:500] or "upstream rate limited (429)",
                            "type": "upstream_error",
                            "code": 429,
                            "retry_after": max(1, math.ceil(rem)),
                        }})
                    if resp.status_code >= 500:
                        state.breaker.note_5xx(endpoint, model)
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail={"error": {"message": error_text[:500], "type": "upstream_error"}}
                    )
                
                state.breaker.note_success(state.client.endpoint, body.get("model", ""))
                
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    
                    rid = _extract_upstream_response_id(chunk)
                    if rid:
                        upstream_response_id = rid
                    
                    usage = chunk.get("usage") or usage
                    
                    for choice in chunk.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        
                        # 处理 content（可能包含 DSML）
                        if delta.get("content"):
                            chunk_content = delta["content"]
                            
                            # 使用 DSML 缓冲区处理
                            cleaned_content, detected_tool_calls = dsml_buffer.add_chunk(chunk_content)
                            
                            # 调试日志：记录 DSML 解析结果
                            if detected_tool_calls:
                                diagnostic("dsml_detected", 
                                          tool_count=len(detected_tool_calls),
                                          tools=[tc["function"]["name"] for tc in detected_tool_calls])
                            
                            # 累积清理后的 content
                            if cleaned_content:
                                content += cleaned_content
                            
                            # 如果检测到 tool_calls，添加到 dict 中
                            if detected_tool_calls:
                                for detected_call in detected_tool_calls:
                                    # 找到下一个可用的 index
                                    next_idx = len(tool_calls_dict)
                                    tool_calls_dict[next_idx] = detected_call
                        
                        # 处理原生 tool_calls（使用 dict 累加，避免预填充）
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_dict:
                                    tool_calls_dict[idx] = {
                                        "id": "", 
                                        "type": "function", 
                                        "function": {"name": "", "arguments": ""}
                                    }
                                
                                if tc.get("id"):
                                    tool_calls_dict[idx]["id"] = tc["id"]
                                if tc.get("function", {}).get("name"):
                                    tool_calls_dict[idx]["function"]["name"] = tc["function"]["name"]
                                if tc.get("function", {}).get("arguments"):
                                    tool_calls_dict[idx]["function"]["arguments"] += tc["function"]["arguments"]
    
    except httpx.HTTPError as exc:
        diagnostic("upstream_error", protocol=protocol, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {exc}", "type": "upstream_error"}})
    
    # 转换为 list 并过滤掉无效的 tool_calls（name 为空的）
    tool_calls = [
        v for k, v in sorted(tool_calls_dict.items()) 
        if v["function"]["name"]
    ]
    
    # 如果检测到 DSML tool_calls，修改 finish_reason
    if tool_calls and dsml_buffer.should_emit_tool_calls():
        finish_reason = "tool_calls"
    
    
    # previous_response_id 会话延续：流正常聚合完成（无上游异常）时记录
    if upstream_response_id and conv_id:
        state.record_response_id(conv_id, upstream_response_id)
    
    # P2 消耗监控：成功聚合后计入滑动窗口
    if usage:
        state.record_burn(body.get("model", ""), usage)
    
    # 【日志】收集完成
    if state.verbose_llm:
        # collect_upstream 没有保存原始响应，只记录聚合后的内容
        pass
    
    log_upstream_response(protocol, content, stream=False)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": now_s(),
        "model": body.get("model", "auto"),
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls if tool_calls else None
            },
            "finish_reason": finish_reason or "stop"
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    }


# ============================================================================
# 协议转换
# ============================================================================

def convert_nonstream(data: dict[str, Any], protocol: str, original: dict[str, Any] | None) -> dict[str, Any]:
    """将聚合的 OpenAI 格式转换为目标协议格式。"""
    if protocol == "openai":
        return data
    
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content", "")
    
    if protocol == "anthropic":
        content_blocks = []
        if content:
            content_blocks.append({"type": "text", "text": content})
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = fn.get("arguments", "")
            content_blocks.append({
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "input": arguments
            })
        return {
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "role": "assistant",
            "model": (original or {}).get("model", data.get("model", "default")),
            "content": content_blocks,
            "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": (data.get("usage") or {}).get("prompt_tokens", 0),
                "output_tokens": (data.get("usage") or {}).get("completion_tokens", 0)
            }
        }
    
    elif protocol == "responses":
        # 构建 content 数组（文本 + 工具调用）
        content_parts = []
        if content:
            content_parts.append({"type": "output_text", "text": content})
        
        # 处理工具调用
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = fn.get("arguments", "")
            
            content_parts.append({
                "type": "function_call",
                "id": call.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": arguments
            })
        
        return {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": now_s(),
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": content_parts
            }]
        }
    
    return data


# ============================================================================
# 启动
# ============================================================================

def main():
    global proxy_state
    
    parser = argparse.ArgumentParser(description="CodeBuddy local API proxy")
    parser.add_argument("--host", default=os.getenv("CODEBUDDY_PROXY_HOST", "127.0.0.1"),
                        help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.getenv("CODEBUDDY_PROXY_PORT", "8787")),
                        help="监听端口")
    parser.add_argument("--endpoint", default=os.getenv("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"),
                        help="CodeBuddy/WorkBuddy 后端地址")
    parser.add_argument("--platform", default=os.getenv("CODEBUDDY_PLATFORM", "VSCode"),
                        help="认证 platform 标识（国内 CodeBuddy 用 VSCode，海外 WorkBuddy AI 用 workbuddy-ai）")
    parser.add_argument("--session-file", type=pathlib.Path,
                        help="会话文件路径")
    parser.add_argument("--mock-dir", type=pathlib.Path,
                        help="只使用指定目录中的真实响应 fixture，不访问 CodeBuddy 后端")
    parser.add_argument("--log-file", type=pathlib.Path,
                        default=pathlib.Path(os.getenv("CODEBUDDY_PROXY_LOG_FILE", "logs/codebuddy-proxy.jsonl")),
                        help="记录完整请求/响应的 JSONL 文件（默认 logs/codebuddy-proxy.jsonl）")
    parser.add_argument("--desensitize", action="store_true",
                        help="启用脱敏处理，对 system 消息中的敏感词插入零宽空格（缓解审核误拦）")
    parser.add_argument("--optimize-context", action="store_true",
                        help="启用消息压缩优化（仅 /v1/responses），大幅减少 token 使用（适用于 Codex CLI 等长上下文场景）")
    parser.add_argument("--login", action="store_true",
                        help="启动时执行浏览器登录/账户查询")
    parser.add_argument("--no-browser", action="store_true",
                        help="登录时不自动打开浏览器")
    parser.add_argument("--verbose-llm", action="store_true",
                        help="log full LLM request/response content (default: summary only, saves 98%% space)")
    parser.add_argument("--device-token", default=None,
                        help="x-device-token：裸 token 值、JSON 文件（extract_device_token.py --output）"
                             "或其路径；也可用 CODEBUDDY_DEVICE_TOKEN 环境变量。"
                             "缺省时尝试自动发现（官方 IDE 下大概率找不到，见 extract 脚本注释）")
    parser.add_argument("--rate-qps", type=float, default=1.0,
                        help="全局限速：持续 QPS（令牌桶补充速率，默认 1.0，0 表示不限速）")
    parser.add_argument("--rate-burst", type=int, default=5,
                        help="全局限速：突发容量（默认 5，超出部分排队等待）")
    parser.add_argument("--rate-jitter", type=float, default=3.0,
                        help="每请求随机抖动上限（秒，默认 3.0，0 关闭）")
    parser.add_argument("--credit-burn-threshold", type=float, default=0.3,
                        help="消耗速率告警阈值：10 分钟窗口消耗占剩余额度比例（默认 0.3）")
    args = parser.parse_args()
    
    # 设置日志
    log_dir = args.log_file.parent if args.log_file else pathlib.Path("logs")
    logger = setup_logging(log_dir)
    
    # 解析 x-device-token：CLI > 环境变量 > 自动发现
    device_token, token_source = resolve_device_token(args.device_token)
    if device_token:
        logger.info(f"x-device-token loaded from {token_source} (len={len(device_token)})")
        print(f"x-device-token loaded from {token_source}")
    else:
        logger.warning(
            "x-device-token not found; X-Device-Token header will be omitted. "
            "Runtime-generated by @tencent/turing-shield-sdk (see server/extract_device_token.py). "
            "Capture via mitmproxy then: export CODEBUDDY_DEVICE_TOKEN=... or --device-token <file>"
        )
        print("WARNING: x-device-token not found; header omitted (see logs / extract_device_token.py)")
    
    # 初始化客户端
    client = CodeBuddyClient(args.endpoint, platform=args.platform, session_file=args.session_file)
    
    # 处理登录
    if args.login:
        client.login(open_browser=not args.no_browser)
    
    # 创建全局状态
    proxy_state = ProxyState(
        client=client,
        mock_dir=args.mock_dir,
        log_file=args.log_file,
        enable_desensitize=args.desensitize,
        enable_optimize_context=args.optimize_context,
        verbose_llm=args.verbose_llm,
        logger=logger,
        device_token=device_token,
        rate_qps=args.rate_qps,
        rate_burst=args.rate_burst,
        rate_jitter=args.rate_jitter,
        credit_burn_threshold=args.credit_burn_threshold,
    )
    
    # 启动信息输出到 stdout
    print(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    print("Endpoints: /v1/models /v1/chat/completions /v1/responses /v1/messages /v1/credits /v1/breaker /health")
    
    # 同时记录到日志
    logger.info(f"CodeBuddy proxy listening on http://{args.host}:{args.port}")
    logger.info("Endpoints: /v1/models /v1/chat/completions /v1/responses /v1/messages /v1/credits /v1/breaker /health")
    
    # 启动 uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")

if __name__ == "__main__":
    main()
