"""Prompt 构造的单元测试。

**这个文件里最重要的断言是"prompt 必须含 json 这个词"。**
DeepSeek 的 JSON mode 有此硬性要求，缺了它线上会直接 400。
把这条约束锁进测试，是为了防止将来改 prompt 文案时不小心删掉关键词——
那时线上会大面积失败，而错误信息指向的是请求参数，很难反向定位到文案。
"""

from __future__ import annotations

import pytest

from app.ai.prompts import (
    BASE_SYSTEM_PROMPT,
    assert_json_keyword,
    build_extraction_prompt,
    build_repair_prompt,
)
from app.ai.templates import (
    ExtractionTemplate,
    builtin_template_keys,
    get_builtin_template,
)


class TestJsonKeywordRequirement:
    @pytest.mark.parametrize("template_key", sorted(builtin_template_keys()))
    def test_builtin_templates_produce_prompt_with_json_keyword(self, template_key: str) -> None:
        """三套内置模板全部覆盖——少测一套就可能漏掉那条分支。"""
        prompt = build_extraction_prompt(
            template=get_builtin_template(template_key),
            document_text="这是一份测试文档的正文内容。",
        )
        assert prompt.contains_json_keyword()

    def test_custom_template_prompt_contains_keyword(self) -> None:
        """自定义 schema 走的是另一条构造分支，同样要覆盖。"""
        template = ExtractionTemplate.from_custom_schema(
            key="custom",
            name="采购单",
            schema={
                "type": "object",
                "properties": {"order_no": {"type": "string", "title": "订单号"}},
            },
        )
        prompt = build_extraction_prompt(template=template, document_text="订单号 A-001")
        assert prompt.contains_json_keyword()

    def test_repair_prompt_contains_keyword(self) -> None:
        """纠正 prompt 是最容易漏掉的一处——它走的是另一条构造路径。"""
        prompt = build_repair_prompt(previous_output="{bad", error_hint="第 3 行解析失败")
        assert "json" in prompt.lower()

    def test_base_system_prompt_contains_keyword(self) -> None:
        assert "json" in BASE_SYSTEM_PROMPT.lower()

    def test_assert_raises_when_keyword_missing(self) -> None:
        with pytest.raises(ValueError, match="json"):
            assert_json_keyword("你是一个抽取助手", "请抽取以下字段")

    def test_assert_passes_when_keyword_present(self) -> None:
        assert_json_keyword("输出 JSON", "请处理")


class TestExtractionPromptContent:
    @pytest.fixture
    def contract_prompt(self):
        return build_extraction_prompt(
            template=get_builtin_template("contract"),
            document_text="甲方：北京星辰科技有限公司\n乙方：上海云图信息技术有限公司",
        )

    def test_includes_chinese_field_labels(self, contract_prompt) -> None:
        """中文标签帮助模型理解字段语义，尤其对 party_a 这种抽象字段名。"""
        assert "甲方" in contract_prompt.user
        assert "乙方" in contract_prompt.user
        assert "合同金额" in contract_prompt.user

    def test_includes_field_descriptions(self, contract_prompt) -> None:
        assert "保留全称不要简称" in contract_prompt.user

    def test_includes_json_schema(self, contract_prompt) -> None:
        assert '"type":"object"' in contract_prompt.user
        assert '"party_a"' in contract_prompt.user

    def test_marks_required_fields(self, contract_prompt) -> None:
        assert "（必填）" in contract_prompt.user

    def test_shows_enum_values(self, contract_prompt) -> None:
        assert "CNY" in contract_prompt.user
        assert "取值范围" in contract_prompt.user

    def test_includes_business_hint(self, contract_prompt) -> None:
        assert "YYYY-MM-DD" in contract_prompt.user

    def test_document_is_wrapped_in_delimiters(self, contract_prompt) -> None:
        """定界符把「指令」和「待处理内容」分开，是抵御提示词注入的基础手段。"""
        assert "<<<DOCUMENT" in contract_prompt.user
        assert "DOCUMENT>>>" in contract_prompt.user
        assert contract_prompt.user.index("北京星辰科技有限公司") > contract_prompt.user.index(
            "<<<DOCUMENT"
        )

    def test_nested_item_fields_are_described(self) -> None:
        """简历的工作经历是数组套对象，子字段必须逐个说明。"""
        prompt = build_extraction_prompt(
            template=get_builtin_template("resume"), document_text="王小明 简历"
        )
        assert "work_experience" in prompt.user
        assert "company" in prompt.user
        assert "公司全称" in prompt.user

    def test_array_of_string_is_annotated(self) -> None:
        prompt = build_extraction_prompt(
            template=get_builtin_template("resume"), document_text="技能：Vue、Python"
        )
        assert "数组元素为文本" in prompt.user


class TestTruncationNotice:
    def test_truncated_document_gets_explicit_notice(self) -> None:
        """不告知截断，模型会把"没找到"当成"不存在"，导致静默的低质量抽取。"""
        prompt = build_extraction_prompt(
            template=get_builtin_template("contract"),
            document_text="正文",
            truncated=True,
        )
        assert "已被省略" in prompt.user
        assert "填 null" in prompt.user

    def test_normal_document_has_no_truncation_notice(self) -> None:
        prompt = build_extraction_prompt(
            template=get_builtin_template("contract"),
            document_text="正文",
            truncated=False,
        )
        assert "已被省略" not in prompt.user


class TestRepairPrompt:
    def test_includes_previous_output(self) -> None:
        prompt = build_repair_prompt(previous_output='{"party_a": "某某公司"', error_hint="截断")
        assert "某某公司" in prompt

    def test_includes_reason(self) -> None:
        prompt = build_repair_prompt(previous_output="{", error_hint="第 5 行缺少右括号")
        assert "第 5 行缺少右括号" in prompt

    def test_lists_concrete_requirements(self) -> None:
        prompt = build_repair_prompt(previous_output="{", error_hint="解析失败")
        assert "Markdown 代码块" in prompt
        assert "逗号" in prompt

    def test_caps_previous_output_length(self) -> None:
        """上一次的输出可能很长，全量回传会让重试的输入 token 反而超过原请求。"""
        prompt = build_repair_prompt(previous_output="x" * 10_000, error_hint="失败")
        assert len(prompt) < 5_000
