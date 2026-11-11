"""DSML 流式缓冲区的回归测试。

这三个 bug 都会造成「长任务输出被截断」，且与客户端无关——所有接入的编辑器
都会中招，因为长输出几乎必然含 '<'（代码泛型、HTML、比较运算符、markdown）。

1. 只要 buffer 里有 '<' 就扣住全部内容，不再输出
2. 工具调用块之后的 suffix 被整个丢弃
3. 流结束没有 flush，滞留内容永久丢失
"""

import pytest

from dsml_parser import MAX_PENDING_TAG_BYTES, DSMLStreamBuffer

LT = chr(60)
GT = chr(62)
Q = chr(34)


def _invoke(name="bash", arg="cmd", value="ls"):
    return (LT + "invoke name=" + Q + name + Q + GT
            + LT + "parameter name=" + Q + arg + Q + GT + value
            + LT + "/parameter" + GT + LT + "/invoke" + GT)


def _drain(chunks, flush=True):
    """模拟流式消费，返回拼接后的输出。"""
    buf = DSMLStreamBuffer()
    out = ""
    for c in chunks:
        text, _ = buf.add_chunk(c)
        out += text
    if flush:
        out += buf.flush()
    return out


# ---------------------------------------------------------------------------
# bug 1：含 '<' 的普通文本不能被无限扣住
# ---------------------------------------------------------------------------

def test_generic_code_not_withheld():
    """代码泛型 Foo<T> 不能导致后续内容被扣住。"""
    out = _drain(["public ", "class ", "Foo" + LT + "T" + GT + " ",
                  "implements ", "Bar" + LT + "T" + GT + " {", " }"])
    assert out == "public class Foo" + LT + "T" + GT + " implements Bar" + LT + "T" + GT + " { }"


def test_comparison_operator_not_withheld():
    """比较运算符 a < b 不能扣住内容。"""
    out = _drain(["如果 a ", LT, " b 则 ", "返回 True"])
    assert "a " in out and "b 则" in out and "返回 True" in out


def test_html_in_markdown_not_withheld():
    """markdown 里的 HTML 片段不能扣住后续内容。"""
    out = _drain(["请看示例：", LT + "div class=" + Q + "x" + Q + GT,
                  "内容", LT + "/div" + GT, " 结束"])
    assert "请看示例：" in out
    assert "内容" in out
    assert "结束" in out


def test_pending_tag_is_held_briefly():
    """真正未闭合的标签仍应短暂扣住（等工具调用闭标签）。"""
    buf = DSMLStreamBuffer()
    text, calls = buf.add_chunk("前缀" + LT + "invoke name=" + Q + "bash" + Q)
    assert text == "前缀"
    assert calls is None
    assert buf.buffer != ""  # 确实在等闭标签


def test_pending_tag_overflow_released():
    """防呆：未闭合尾巴超过阈值必须放行，否则 buffer 无限膨胀。"""
    buf = DSMLStreamBuffer()
    buf.add_chunk("x" * 100)
    # 灌一个超长的 '<' 开头但始终不闭合的尾巴
    text, _ = buf.add_chunk(LT + "y" * (MAX_PENDING_TAG_BYTES + 100))
    assert LT in text  # 超阈值后当作普通文本放行
    assert buf.buffer == ""


# ---------------------------------------------------------------------------
# bug 2：工具调用之后的 suffix 不能丢
# ---------------------------------------------------------------------------

def test_content_after_tool_call_preserved():
    """工具调用块之后的正文必须保留。"""
    buf = DSMLStreamBuffer()
    out = ""
    text, calls = buf.add_chunk("前缀文本")
    out += text
    text, calls = buf.add_chunk(_invoke() + "后面的正文内容")
    out += text
    assert calls is not None
    text, _ = buf.add_chunk("更多正文")
    out += text
    out += buf.flush()
    assert "前缀文本" in out
    assert "后面的正文内容" in out
    assert "更多正文" in out


def test_multiple_tool_calls_with_text_between():
    """两个工具调用之间夹着正文，都不能丢。"""
    buf = DSMLStreamBuffer()
    out = ""
    for chunk in ["第一段", _invoke("a", "p", "1"), "中间段",
                  _invoke("b", "p", "2"), "最后段"]:
        text, _ = buf.add_chunk(chunk)
        out += text
    out += buf.flush()
    for expected in ("第一段", "中间段", "最后段"):
        assert expected in out, f"丢失了 {expected}"
    assert len(buf.detected_calls) >= 1


# ---------------------------------------------------------------------------
# bug 3：流结束必须 flush
# ---------------------------------------------------------------------------

def test_flush_returns_remaining():
    """flush() 必须吐出 buffer 剩余内容。"""
    buf = DSMLStreamBuffer()
    buf.add_chunk("第一段。")
    text, _ = buf.add_chunk("第二段带 " + LT + " 尖括号")
    remaining = buf.flush()
    assert "尖括号" in remaining
    assert buf.buffer == ""
    assert buf.flush() == ""  # 幂等


def test_flush_after_complete_stream():
    """正常完整流（含 [DONE] 语义）flush 不应重复输出。"""
    out = _drain(["完整的一句话"], flush=True)
    assert out == "完整的一句话"


def test_long_output_nothing_lost():
    """长输出（含大量 '<' 与工具调用）不能丢任何一段。"""
    parts = []
    for i in range(50):
        parts.append(f"第{i}段说明文字。")
        parts.append("代码 " + LT + "T" + GT + " 泛型。")
    parts.append(_invoke())
    for i in range(50):
        parts.append(f"工具调用后第{i}段。")
    out = _drain(parts)
    for i in range(50):
        assert f"第{i}段说明文字。" in out, f"丢失第{i}段"
        assert f"工具调用后第{i}段。" in out, f"丢失工具调用后第{i}段"
    assert out.count("泛型。") == 50
