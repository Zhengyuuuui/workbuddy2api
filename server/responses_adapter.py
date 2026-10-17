"""
responses_adapter.py — OpenAI Responses API ↔ Chat Completions API 适配层。

Codex CLI 使用 Responses API（POST /v1/responses），而 CodeBuddy 后端只支持
Chat Completions 协议。本模块做双向转换：
  请求：Responses input/instructions/tools → Chat messages/tools
  响应：Chat SSE delta → Responses 语义事件流（response.created / output_text.delta / …）
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Iterator


def _rand_id(prefix: str = "resp_") -> str:
    """生成随机ID"""
    return prefix + os.urandom(12).hex()


def _now_s() -> int:
    """当前时间戳（秒）"""
    return int(time.time())


def _extract_text(value: Any) -> str:
    """从各种content格式中提取纯文本"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text"):
                    parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if value is None else str(value)


# 上游 chat/completions 只认 {"type":"function","function":{...}} 这一种工具定义。
# Codex 客户端（wire_api=responses）还会发 namespace / web_search / local_shell 等
# Responses 专有类型，原样转发会被上游以 code 11129
# "invalid function call parameters" 整体拒绝（详见 _convert_tools_for_chat）。
_CHAT_TOOL_TYPES = {"function"}


def _chat_tool_parameters(params: Any) -> dict | None:
    """校验并归一化工具的 JSON Schema 参数，不合法返回 None（调用方丢弃该工具）。

    上游对 parameters 要求：
      - 必须是 object
      - type 必须为 "object"（有的是 "$schema"/"properties" 而无 type）
      - properties 非空（namespace 类工具天然没有，会被丢）
    空 schema 的 function 工具同样会被上游拒，故一并视为不合法。
    """
    if not isinstance(params, dict):
        return None
    if params.get("type") != "object":
        return None
    props = params.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    return params


def _convert_tools_for_chat(tools: list[dict]) -> list[dict]:
    """将Responses工具格式转换为Chat格式

    Responses: {name, description, parameters, strict}
    Chat: {type: "function", "function": {name, description, parameters, strict}}

    只保留上游能接受的 function 工具：
      - Responses 专有类型（namespace / web_search / local_shell / custom_tool_call …）
        一律丢弃——Codex 的 namespace 工具没有 parameters，转成空 schema 会让上游
        以 11129 invalid function call parameters 拒绝整个请求。
      - parameters 不是合法 JSON Schema 的同样丢弃。
    若过滤后一个都不剩，返回 []（调用方据此不发送 tools 字段）。
    """
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")
        if ttype is not None and ttype not in _CHAT_TOOL_TYPES:
            continue
        # Chat 格式原样带进来的：仍要校验 parameters
        if ttype == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            params = _chat_tool_parameters(fn.get("parameters"))
            if params is None:
                continue
            out: dict[str, Any] = {"type": "function", "function": {
                "name": fn.get("name") or "",
                "description": fn.get("description", ""),
                "parameters": params,
            }}
            if "strict" in fn:
                out["function"]["strict"] = fn["strict"]
            result.append(out)
            continue
        # Responses 格式 → Chat 格式
        name = tool.get("name")
        if not name:
            continue
        params = _chat_tool_parameters(tool.get("parameters"))
        if params is None:
            continue
        out = {"type": "function", "function": {
            "name": name,
            "description": tool.get("description", ""),
            "parameters": params,
        }}
        if "strict" in tool:
            out["function"]["strict"] = tool["strict"]
        result.append(out)
    return result


def _convert_input_items(items: list) -> list[dict]:
    """将Responses API的input数组转换为Chat messages
    
    处理：
      - {"role": "user/system/developer", "content": ...}
      - {"type": "message", ...}
      - {"type": "function_call", ...} → 合并到前面的assistant消息
      - {"type": "function_call_output", ...} → tool角色
    """
    messages: list[dict] = []
    # 临时缓存：合并相邻的assistant message和function_call
    pending_assistant_content: str | None = None
    pending_tool_calls: list[dict] = []

    def _flush_assistant():
        nonlocal pending_assistant_content, pending_tool_calls
        if pending_assistant_content is not None or pending_tool_calls:
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": pending_assistant_content or "",
            }
            if pending_tool_calls:
                msg["tool_calls"] = pending_tool_calls[:]
            messages.append(msg)
            pending_assistant_content = None
            pending_tool_calls.clear()

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        role = item.get("role", "")

        # 普通消息（无type标记）
        if item_type is None and role in ("user", "system", "developer"):
            _flush_assistant()
            mapped_role = "system" if role == "developer" else role
            content = _extract_text(item.get("content", ""))
            messages.append({"role": mapped_role, "content": content})
            continue

        # typed message
        if item_type == "message":
            if role == "assistant":
                _flush_assistant()
                content = _extract_text(item.get("content", ""))
                pending_assistant_content = content
            elif role in ("user", "system", "developer"):
                _flush_assistant()
                mapped_role = "system" if role == "developer" else role
                content = _extract_text(item.get("content", ""))
                messages.append({"role": mapped_role, "content": content})
            continue

        # function_call — 合并到前面的assistant消息
        if item_type == "function_call":
            if pending_assistant_content is None:
                pending_assistant_content = ""
            pending_tool_calls.append({
                "id": item.get("call_id", item.get("id", _rand_id("call_"))),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": json.dumps(item.get("arguments", {})) if isinstance(item.get("arguments"), dict) else str(item.get("arguments", "")),
                },
            })
            continue

        # function_call_output → tool消息
        if item_type == "function_call_output":
            _flush_assistant()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": _extract_text(item.get("output", "")),
            })
            continue

        # 其他未知类型，如果有role就当普通消息处理
        if role:
            _flush_assistant()
            content = _extract_text(item.get("content", ""))
            messages.append({"role": role, "content": content})

    # 刷新最后一条assistant消息
    _flush_assistant()
    return messages


def responses_request_to_chat(body: dict) -> dict:
    """将Responses API请求体转换为Chat Completions请求体
    
    关键映射：
      input → messages
      instructions → system message（置顶）
      max_output_tokens → max_tokens
      tools格式微调
    """
    messages: list[dict] = []

    # instructions → system message
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # input → messages
    inp = body.get("input", [])
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        messages.extend(_convert_input_items(inp))

    # 构造Chat body
    chat: dict[str, Any] = {"messages": messages, "stream": True}

    # model
    if "model" in body:
        chat["model"] = body["model"]

    # tools
    tools = body.get("tools")
    if tools:
        chat["tools"] = _convert_tools_for_chat(tools)
    if "tool_choice" in body:
        chat["tool_choice"] = body["tool_choice"]

    # 透传常见参数
    for key in ("temperature", "top_p", "stop", "seed",
                "presence_penalty", "frequency_penalty",
                "response_format", "reasoning_effort"):
        if key in body:
            chat[key] = body[key]

    # max_output_tokens → max_tokens
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    elif "max_tokens" in body:
        chat["max_tokens"] = body["max_tokens"]

    return chat


# ---------------------------------------------------------------------------
# 响应转换：Chat SSE → Responses事件流
# ---------------------------------------------------------------------------

class ResponsesStreamConverter:
    """将Chat SSE流转换为Responses API事件流
    
    事件序列：
      1. response.created
      2. response.in_progress
      3. response.output_item.added (message)
      4. response.content_part.added
      5. response.output_text.delta (多次)
      6. response.output_item.added (function_call，如有)
      7. response.function_call_arguments.delta (多次)
      8. response.output_text.done
      9. response.content_part.done
      10. response.output_item.done (各项)
      11. response.completed
    """

    def __init__(self, model: str):
        self.response_id = _rand_id("resp_")
        self.model = model
        self.created_at = _now_s()

        # 状态跟踪
        self.started = False
        self.message_item_added = False
        self.content_part_added = False
        self.text = ""
        self.function_calls: dict[int, dict] = {}  # index -> {id, name, arguments}
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        # 每个事件的序号（OpenAI Responses SSE 规范强制字段，见官方 schema）：
        # Claude Code 会容忍缺失，Codex 的 Rust 解析器要求严格存在，
        # 缺字段会让 Codex 报 "stream closed before response.completed"。
        self._seq = 0

    def _next_seq(self) -> int:
        """返回下一个事件的序号（从 0 起，官方事件字段名为 sequence_number）。"""
        self._seq += 1
        return self._seq - 1

    def _response_snapshot(self, status: str, output: list | None = None,
                           usage: dict | None = None) -> dict:
        """构造 Response 对象快照。

        字段集与官方 Response schema 的必填项对齐（id / created_at / model /
        object / output / parallel_tool_calls / tool_choice / tools）——Codex 用
        Rust 结构体强类型解析，缺任一必填字段都会让整条流被判为不完整。
        注意 object 必须是 "response"：早期版本写 "realtime.response"（Realtime
        API 的取值），在 Responses API 里是非法枚举值。
        """
        snap: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "model": self.model,
            "status": status,
            "output": output or [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": {},
            "temperature": None,
            "top_p": None,
        }
        if usage is not None:
            snap["usage"] = usage
        return snap

    def feed_chunk(self, chunk: dict) -> list[tuple[str, dict]]:
        """处理一个Chat SSE chunk，返回Responses事件列表"""
        events: list[tuple[str, dict]] = []

        # 首次chunk：发出created和in_progress
        if not self.started:
            self.started = True
            events.append(("response.created", {
                "type": "response.created",
                "sequence_number": self._next_seq(),
                "response": self._response_snapshot("in_progress"),
            }))
            events.append(("response.in_progress", {
                "type": "response.in_progress",
                "sequence_number": self._next_seq(),
                "response": self._response_snapshot("in_progress"),
            }))

        # 提取usage
        if chunk.get("usage"):
            self.usage = chunk["usage"]

        # 处理choices
        for choice in chunk.get("choices", []):
            self.finish_reason = choice.get("finish_reason") or self.finish_reason
            delta = choice.get("delta", {})

            # 文本内容
            if delta.get("content"):
                text_delta = str(delta["content"])
                self.text += text_delta

                # 首次文本：发出output_item.added + content_part.added
                if not self.message_item_added:
                    self.message_item_added = True
                    events.append(("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "sequence_number": self._next_seq(),
                        "item": {
                            "id": self.response_id + "_msg",
                            "type": "message",
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    }))

                if not self.content_part_added:
                    self.content_part_added = True
                    events.append(("response.content_part.added", {
                        "type": "response.content_part.added",
                        "item_id": self.response_id + "_msg",
                        "output_index": 0,
                        "content_index": 0,
                        "sequence_number": self._next_seq(),
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    }))

                # 发出文本delta
                events.append(("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": self.response_id + "_msg",
                    "output_index": 0,
                    "content_index": 0,
                    "sequence_number": self._next_seq(),
                    "delta": text_delta,
                    "logprobs": [],
                }))

            # 工具调用
            for call in delta.get("tool_calls", []):
                index = int(call.get("index", 0))
                slot = self.function_calls.setdefault(index, {
                    "id": None,
                    "name": "",
                    "arguments": "",
                })

                call_id = call.get("id")
                if call_id:
                    slot["id"] = call_id

                fn = call.get("function", {})
                fn_name = fn.get("name")
                fn_args = fn.get("arguments")

                # 首次见到这个调用：发出output_item.added
                if fn_name and not slot["name"]:
                    slot["name"] = fn_name
                    item_id = slot["id"] or f"fc_{self.response_id}_{index}"
                    slot["id"] = item_id
                    events.append(("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": index,
                        "sequence_number": self._next_seq(),
                        "item": {
                            "id": item_id,
                            "type": "function_call",
                            "call_id": item_id,
                            "name": fn_name,
                            "arguments": "",
                            "status": "in_progress",
                        },
                    }))

                # arguments增量
                if fn_args:
                    slot["arguments"] += fn_args
                    events.append(("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": slot["id"] or f"fc_{self.response_id}_{index}",
                        "output_index": index,
                        "sequence_number": self._next_seq(),
                        "delta": fn_args,
                    }))

        return events

    def finish(self) -> list[tuple[str, dict]]:
        """流结束，发出done和completed事件"""
        events: list[tuple[str, dict]] = []

        # 关闭文本块
        if self.content_part_added:
            events.append(("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": self.response_id + "_msg",
                "output_index": 0,
                "content_index": 0,
                "sequence_number": self._next_seq(),
                "text": self.text,
                "logprobs": [],
            }))
            events.append(("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": self.response_id + "_msg",
                "output_index": 0,
                "content_index": 0,
                "sequence_number": self._next_seq(),
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            }))

        # 关闭消息item
        if self.message_item_added:
            events.append(("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": 0,
                "sequence_number": self._next_seq(),
                "item": {
                    "id": self.response_id + "_msg",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": self.text, "annotations": []}] if self.text else [],
                },
            }))

        # 关闭各个function_call item
        for index, call in sorted(self.function_calls.items()):
            events.append(("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": index,
                "sequence_number": self._next_seq(),
                "item": {
                    "id": call["id"],
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "status": "completed",
                },
            }))

        # 构造完整response对象
        output_items = []
        if self.message_item_added:
            output_items.append({
                "id": self.response_id + "_msg",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": self.text, "annotations": []}] if self.text else [],
            })
        for index, call in sorted(self.function_calls.items()):
            output_items.append({
                "id": call["id"],
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": call["arguments"],
                "status": "completed",
            })

        # 映射usage（字段集与官方 ResponseUsage 对齐，缺字段会让严格解析器拒绝整条流）
        usage_out = {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        }
        if self.usage:
            usage_out["input_tokens"] = self.usage.get("prompt_tokens", 0)
            usage_out["output_tokens"] = self.usage.get("completion_tokens", 0)
            usage_out["total_tokens"] = self.usage.get("total_tokens", 0)

        # 发出completed
        events.append(("response.completed", {
            "type": "response.completed",
            "sequence_number": self._next_seq(),
            "response": self._response_snapshot("completed", output_items, usage_out),
        }))

        return events

