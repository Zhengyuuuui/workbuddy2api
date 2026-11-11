"""模型上下文档位测试。

背景：AI 编码编辑器带硬编码模型元数据表，表里没有我们的模型时 fallback 到保守
默认值（opencode 是 128000），然后在发请求前就把上下文截断——bridge 支持 100 万
也收不到。所以 /v1/models 必须给出准确档位，且未知时给高估值。

查找优先级：上游 /v3/config 声明 > 内置表 > 默认 1M。
"""

import pytest

from codebuddy_proxy import (
    DEFAULT_CONTEXT_TOKENS,
    MODEL_CONTEXT_TABLE,
    _context_tag,
    _model_limits_payload,
    _normalize_model_id,
    _resolve_context_limits,
)


def test_known_models_from_table():
    """内置表里的模型必须返回准确档位。"""
    assert _resolve_context_limits("deepseek-v4.1-flash") == (1_000_000, 393_216)
    assert _resolve_context_limits("glm-5.3") == (1_000_000, 131_072)
    assert _resolve_context_limits("kimi-k3-1") == (1_000_000, 1_048_576)
    assert _resolve_context_limits("hy3") == (192_000, 64_000)
    assert _resolve_context_limits("auto") == (256_000, 32_000)


def test_unknown_model_defaults_to_1m():
    """未知模型必须给 1M——高估安全，低估会让客户端提前截断上下文。"""
    ctx, out = _resolve_context_limits("some-unknown-model-9000")
    assert ctx == DEFAULT_CONTEXT_TOKENS == 1_000_000
    # output 未知时省略，避免客户端按错误值限制生成长度
    assert out is None


def test_remote_declaration_takes_precedence():
    """上游声明的档位优先于内置表。"""
    ctx, out = _resolve_context_limits("glm-5.3", (512_000, 99_999))
    assert ctx == 512_000
    assert out == 99_999


def test_remote_zero_falls_back_to_table():
    """上游给 0/None 时回退到内置表。"""
    ctx, out = _resolve_context_limits("glm-5.3", (0, None))
    assert ctx == 1_000_000
    assert out == 131_072


def test_payload_emits_all_field_aliases():
    """同一值必须发多个别名——不同客户端读的键名不一。"""
    p = _model_limits_payload("deepseek-v4.1-flash")
    assert p["context_length"] == 1_000_000
    assert p["max_input_tokens"] == 1_000_000
    assert p["context_window"] == 1_000_000
    assert p["top_provider"]["context_length"] == 1_000_000
    assert p["max_output_tokens"] == 393_216
    assert p["max_completion_tokens"] == 393_216
    assert p["input_modalities"] == ["text"]


def test_payload_omits_output_when_unknown():
    """output 未知时不能硬塞一个值。"""
    p = _model_limits_payload("unknown-model")
    assert "max_output_tokens" not in p
    assert "max_completion_tokens" not in p
    assert p["context_length"] == 1_000_000


def test_payload_uses_remote_when_present():
    p = _model_limits_payload("deepseek-v4.1-flash", (700_000, 120_000))
    assert p["context_length"] == 700_000
    assert p["max_output_tokens"] == 120_000


def test_context_tag_formatting():
    assert _context_tag(1_000_000) == "1M"
    assert _context_tag(192_000) == "192k"
    assert _context_tag(256_000) == "256k"
    assert _context_tag(500) == "500"
    assert _context_tag(0) == ""


def test_normalize_strips_context_tag():
    """客户端可能把带 [1M] 标记的 id 原样回传，转发前必须剥掉。"""
    assert _normalize_model_id("hy4-preview-f[1M]") == "hy4-preview-f"
    assert _normalize_model_id("fast-model (x0.34)[1M]") == "fast-model"
    assert _normalize_model_id("fast-model (x0.34)") == "fast-model"
    assert _normalize_model_id("glm-5.3") == "glm-5.3"


def test_table_values_are_sane():
    """表里不能出现 0 或负值。"""
    for mid, (ctx, out) in MODEL_CONTEXT_TABLE.items():
        assert ctx > 0, f"{mid} context 非法"
        assert out is None or out > 0, f"{mid} output 非法"


@pytest.mark.parametrize("mid", list(MODEL_CONTEXT_TABLE)[:5])
def test_table_models_resolve(mid):
    ctx, out = _resolve_context_limits(mid)
    assert ctx > 0
    assert ctx == MODEL_CONTEXT_TABLE[mid][0]
