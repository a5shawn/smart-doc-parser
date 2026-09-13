"""文本清洗与截断的单元测试。"""

from __future__ import annotations

import pytest

from app.services.parsing.normalize import (
    MIN_TEXT_CHARS,
    decode_bytes,
    has_text_layer,
    make_preview,
    normalize_text,
    truncate_for_llm,
)


class TestNormalizeText:
    def test_converts_crlf_and_cr_to_lf(self) -> None:
        assert normalize_text("甲\r\n乙\r丙") == "甲\n乙\n丙"

    def test_removes_zero_width_characters(self) -> None:
        # 零宽空格、零宽连字符、BOM 都是肉眼不可见但会白吃 token 的字符
        assert normalize_text("合​同﻿金‍额") == "合同金额"

    def test_collapses_excess_newlines(self) -> None:
        assert normalize_text("甲\n\n\n\n\n乙") == "甲\n\n乙"

    def test_strips_trailing_whitespace_per_line(self) -> None:
        assert normalize_text("甲   \n乙\t\n") == "甲\n乙"

    def test_removes_control_characters(self) -> None:
        assert normalize_text("甲\x00\x07乙") == "甲乙"

    def test_keeps_internal_structure(self) -> None:
        """清洗应当是保守的：不该动段落之间的单个空行。"""
        assert normalize_text("第一段\n\n第二段") == "第一段\n\n第二段"

    def test_empty_input(self) -> None:
        assert normalize_text("") == ""

    def test_whitespace_only_input(self) -> None:
        assert normalize_text("   \n\t\n  ") == ""


class TestDecodeBytes:
    def test_utf8(self) -> None:
        text, encoding = decode_bytes("合同金额".encode())
        assert text == "合同金额"
        assert encoding == "utf-8"

    def test_utf8_with_bom(self) -> None:
        text, encoding = decode_bytes("合同".encode("utf-8-sig"))
        assert text == "合同"
        assert encoding == "utf-8-sig"

    def test_gb18030(self) -> None:
        """国内老系统导出的简历/合同常见 GBK 编码。"""
        text, encoding = decode_bytes("合同金额".encode("gb18030"))
        assert text == "合同金额"
        assert encoding == "gb18030"

    def test_invalid_bytes_fall_back_without_raising(self) -> None:
        """任何字节都得能解出结果——解析流程不该因为一个怪文件整个崩掉。"""
        text, encoding = decode_bytes(b"\xff\xfe\x00\x01\x9c")
        assert isinstance(text, str)
        assert encoding in {"gb18030", "big5", "latin-1"}


class TestHasTextLayer:
    def test_real_text(self) -> None:
        assert has_text_layer("采购合同\n甲方：北京星辰科技有限公司\n乙方：上海云图") is True

    def test_empty(self) -> None:
        assert has_text_layer("") is False

    def test_only_whitespace(self) -> None:
        assert has_text_layer("   \n\n\t ") is False

    def test_below_threshold(self) -> None:
        """扫描件偶尔会提取出几个乱码字符，不能据此判定有文本层。"""
        assert has_text_layer("x" * (MIN_TEXT_CHARS - 1)) is False

    def test_at_threshold(self) -> None:
        assert has_text_layer("x" * MIN_TEXT_CHARS) is True


class TestMakePreview:
    def test_collapses_whitespace_into_single_spaces(self) -> None:
        assert make_preview("甲\n\n乙\t丙") == "甲 乙 丙"

    def test_truncates_to_length(self) -> None:
        assert len(make_preview("字" * 1000, length=100)) == 100

    def test_short_text_unchanged(self) -> None:
        assert make_preview("短文本", length=100) == "短文本"


class TestTruncateForLlm:
    def test_short_text_passes_through(self) -> None:
        text, truncated = truncate_for_llm("合同内容", max_chars=100)
        assert text == "合同内容"
        assert truncated is False

    def test_exact_length_is_not_truncated(self) -> None:
        text, truncated = truncate_for_llm("x" * 100, max_chars=100)
        assert truncated is False
        assert text == "x" * 100

    def test_keeps_head_and_tail(self) -> None:
        """合同的关键信息在开头和落款两处，只留开头会丢掉落款。"""
        body = "开头标记" + "中" * 1000 + "结尾标记"
        text, truncated = truncate_for_llm(body, max_chars=200)

        assert truncated is True
        assert text.startswith("开头标记")
        assert text.endswith("结尾标记")

    def test_marks_omitted_content(self) -> None:
        """必须显式标注省略，否则模型会把断裂的上下文当成完整信息来推断。"""
        text, _ = truncate_for_llm("字" * 1000, max_chars=100)
        assert "省略" in text

    def test_result_length_stays_within_budget(self) -> None:
        """截断后的长度允许略超上限（标记文字），但不应无节制膨胀。"""
        text, _ = truncate_for_llm("字" * 10_000, max_chars=1000)
        assert len(text) < 1000 + 100


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  甲方  ", "甲方"),
        ("甲\n\n\n乙", "甲\n\n乙"),
        ("", ""),
    ],
)
def test_normalize_idempotent(raw: str, expected: str) -> None:
    """清洗两次结果应当一致——避免下游重复清洗时文本继续变形。"""
    once = normalize_text(raw)
    assert once == expected
    assert normalize_text(once) == once
