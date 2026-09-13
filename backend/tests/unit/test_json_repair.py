"""JSON 修复阶梯的单元测试。

这些用例覆盖的是模型"不配合"的真实形态——即使开了 JSON mode，
它仍然会加代码块、加前言、多写逗号。每一条都对应线上真实出现过的输出。
"""

from __future__ import annotations

import pytest

from app.ai.json_repair import parse_json_lenient
from app.core.errors import LLMInvalidJSONError


class TestCleanOutput:
    def test_plain_json(self) -> None:
        data, repairs = parse_json_lenient('{"party_a": "北京星辰科技有限公司"}')
        assert data == {"party_a": "北京星辰科技有限公司"}
        assert repairs == []

    def test_nested_structures(self) -> None:
        raw = '{"items": [{"name": "服务费", "amount": 100}], "total": 100}'
        data, repairs = parse_json_lenient(raw)
        assert data["items"][0]["name"] == "服务费"
        assert repairs == []

    def test_json_with_surrounding_whitespace(self) -> None:
        data, _ = parse_json_lenient('  \n {"a": 1} \n  ')
        assert data == {"a": 1}

    def test_null_values_are_preserved(self) -> None:
        """null 是有意义的信息（表示"没抽到"），不能被当成解析失败。"""
        data, _ = parse_json_lenient('{"party_a": null, "amount": null}')
        assert data == {"party_a": None, "amount": None}


class TestMarkdownFence:
    def test_strips_json_fence(self) -> None:
        raw = '```json\n{"a": 1}\n```'
        data, repairs = parse_json_lenient(raw)
        assert data == {"a": 1}
        assert "stripped_markdown_fence" in repairs[0]

    def test_strips_bare_fence(self) -> None:
        raw = '```\n{"a": 1}\n```'
        data, _ = parse_json_lenient(raw)
        assert data == {"a": 1}

    def test_strips_uppercase_fence(self) -> None:
        data, _ = parse_json_lenient('```JSON\n{"a": 1}\n```')
        assert data == {"a": 1}


class TestPreamble:
    def test_extracts_json_after_explanation(self) -> None:
        """模型经常会先说一句"好的，以下是抽取结果"，说教是没用的，直接扫描出对象。"""
        raw = '好的，以下是抽取结果：\n{"party_a": "某某公司"}\n希望对您有帮助。'
        data, repairs = parse_json_lenient(raw)
        assert data == {"party_a": "某某公司"}
        assert any("scanned_object" in item for item in repairs)

    def test_extracts_json_between_prose_with_braces(self) -> None:
        raw = '根据{schema}的要求，结果如下：{"a": {"b": 1}} 完毕'
        data, _ = parse_json_lenient(raw)
        assert data == {"a": {"b": 1}}


class TestTrailingComma:
    def test_removes_trailing_comma_in_object(self) -> None:
        data, repairs = parse_json_lenient('{"a": 1, "b": 2,}')
        assert data == {"a": 1, "b": 2}
        assert any("removed_trailing_commas" in item for item in repairs)

    def test_removes_trailing_comma_in_array(self) -> None:
        data, _ = parse_json_lenient('{"items": [1, 2, 3,]}')
        assert data == {"items": [1, 2, 3]}

    def test_keeps_commas_inside_strings(self) -> None:
        """字符串里的逗号不能被误删。"""
        data, _ = parse_json_lenient('{"name": "北京,上海,广州"}')
        assert data["name"] == "北京,上海,广州"


class TestTruncatedOutput:
    def test_closes_unbalanced_braces(self) -> None:
        """输出撞上 max_tokens 时 JSON 会在中间断掉，补全总比直接失败好。"""
        raw = '{"party_a": "北京星辰科技有限公司", "party_b": "上海云图'
        data, repairs = parse_json_lenient(raw)
        assert data["party_a"] == "北京星辰科技有限公司"
        assert any("closed_brackets" in item for item in repairs)

    def test_closes_nested_structures(self) -> None:
        raw = '{"items": [{"name": "服务费", "amount": 100'
        data, _ = parse_json_lenient(raw)
        assert data["items"][0]["name"] == "服务费"

    def test_braces_inside_strings_do_not_confuse_counter(self) -> None:
        """JSON 字符串里出现的花括号不能被算进嵌套深度。"""
        raw = '{"note": "金额为 {待确认}", "amount": null'
        data, _ = parse_json_lenient(raw)
        assert data["note"] == "金额为 {待确认}"


class TestFailures:
    def test_empty_content_gives_actionable_message(self) -> None:
        """推理模型的 max_tokens 陷阱：调用成功但 content 为空。

        这个报错必须点出 max_tokens，否则排查方向会完全跑偏。
        """
        with pytest.raises(LLMInvalidJSONError) as exc_info:
            parse_json_lenient("")

        assert "max_tokens" in exc_info.value.message
        assert "推理模型" in exc_info.value.message

    def test_whitespace_only(self) -> None:
        with pytest.raises(LLMInvalidJSONError):
            parse_json_lenient("   \n\t  ")

    def test_plain_prose(self) -> None:
        with pytest.raises(LLMInvalidJSONError) as exc_info:
            parse_json_lenient("抱歉，我无法从这份文档中抽取信息。")

        assert exc_info.value.code == "LLM_INVALID_JSON"
        # 报错要带上原文片段，否则线上只能看到"解析失败"四个字
        assert "抱歉" in exc_info.value.details["raw_preview"]

    def test_malformed_structure(self) -> None:
        with pytest.raises(LLMInvalidJSONError):
            parse_json_lenient('{"a": }')

    def test_error_lists_attempted_strategies(self) -> None:
        with pytest.raises(LLMInvalidJSONError) as exc_info:
            parse_json_lenient("完全不是 JSON")
        assert exc_info.value.details["attempted"]


class TestRepairEscalation:
    def test_multiple_repairs_can_stack(self) -> None:
        """代码块 + 尾逗号同时出现时要能一路修下来。"""
        raw = '```json\n{"a": 1,}\n```'
        data, repairs = parse_json_lenient(raw)
        assert data == {"a": 1}
        # 至少要记录下修复动作，便于观察模型输出质量的长期变化
        assert repairs

    def test_clean_input_records_no_repairs(self) -> None:
        _, repairs = parse_json_lenient('{"a": 1}')
        assert repairs == []
