"""抽取模板的单元测试。"""

from __future__ import annotations

import pytest

from app.ai.schema_utils import normalize_schema, validate_data
from app.ai.templates import (
    ExtractionTemplate,
    TemplateField,
    builtin_template_keys,
    get_builtin_template,
    load_builtin_templates,
)
from app.core.errors import TemplateNotFoundError


class TestBuiltinTemplates:
    def test_three_templates_are_loaded(self) -> None:
        assert builtin_template_keys() == {"contract", "resume", "invoice"}

    def test_loading_is_cached(self) -> None:
        assert load_builtin_templates() is load_builtin_templates()

    @pytest.mark.parametrize("key", ["contract", "resume", "invoice"])
    def test_every_template_has_labeled_fields(self, key: str) -> None:
        """没有中文标签，前端就只能显示 party_a 这种字段名，可读性很差。"""
        template = get_builtin_template(key)
        assert template.fields
        for field in template.fields:
            assert field.label and field.label != field.name

    @pytest.mark.parametrize("key", ["contract", "resume", "invoice"])
    def test_every_template_schema_is_valid(self, key: str) -> None:
        normalize_schema(get_builtin_template(key).schema)

    @pytest.mark.parametrize("key", ["contract", "resume", "invoice"])
    def test_every_template_has_business_hint(self, key: str) -> None:
        """业务规则是抽取质量的关键——日期格式、金额单位都靠它约束。"""
        assert get_builtin_template(key).prompt_hint

    def test_unknown_key_raises_with_available_list(self) -> None:
        with pytest.raises(TemplateNotFoundError) as exc_info:
            get_builtin_template("nonexistent")
        assert "contract" in exc_info.value.details["available"]


class TestContractTemplate:
    def test_required_fields(self) -> None:
        template = get_builtin_template("contract")
        required = {field.name for field in template.fields if field.required}
        assert required == {"contract_name", "party_a", "party_b"}

    def test_amount_is_numeric(self) -> None:
        """金额必须是数字类型，否则没法做金额比对与统计。"""
        schema = get_builtin_template("contract").schema
        assert "number" in schema["properties"]["amount"]["type"]

    def test_all_fields_allow_null(self) -> None:
        """回归测试：prompt 要求模型"没抽到就填 null"，schema 就必须允许 null。

        早期版本把字段声明成 ``{"type": "string"}``，模型老老实实返回 null
        反而被校验判成类型错误——用户看到满屏"类型不匹配"，实际是系统在报假警。
        """
        schema = get_builtin_template("contract").schema
        for name, node in schema["properties"].items():
            assert "null" in node["type"], f"字段 {name} 不允许 null"

    def test_currency_has_enum(self) -> None:
        schema = get_builtin_template("contract").schema
        assert "CNY" in schema["properties"]["currency"]["enum"]

    def test_field_labels_mapping(self) -> None:
        labels = get_builtin_template("contract").field_labels()
        assert labels["party_a"] == "甲方"
        assert labels["amount"] == "合同金额"


class TestResumeTemplate:
    def test_work_experience_is_array_of_objects(self) -> None:
        schema = get_builtin_template("resume").schema
        node = schema["properties"]["work_experience"]
        assert "array" in node["type"]
        assert node["items"]["type"] == "object"
        assert "company" in node["items"]["properties"]

    def test_nested_item_schema_is_closed(self) -> None:
        """数组元素的 object 也要 additionalProperties: false，否则模型会在里面编字段。"""
        schema = normalize_schema(get_builtin_template("resume").schema)
        item_schema = schema["properties"]["work_experience"]["items"]
        assert item_schema["additionalProperties"] is False

    def test_nested_required_is_propagated(self) -> None:
        schema = get_builtin_template("resume").schema
        assert schema["properties"]["work_experience"]["items"]["required"] == ["company"]

    def test_skills_is_array_of_strings(self) -> None:
        schema = get_builtin_template("resume").schema
        assert schema["properties"]["skills"]["items"] == {"type": "string"}

    def test_nested_field_labels_use_bracket_path(self) -> None:
        """前端要用这个路径去嵌套数组里取标签。"""
        labels = get_builtin_template("resume").field_labels()
        assert labels["work_experience[].company"] == "公司"
        assert labels["work_experience[].position"] == "职位"

    def test_validates_realistic_data(self) -> None:
        schema = normalize_schema(get_builtin_template("resume").schema)
        data = {
            "name": "王小明",
            "skills": ["Vue 3", "FastAPI"],
            "work_experience": [{"company": "某某公司", "position": "工程师"}],
        }
        assert validate_data(data, schema) == []


class TestInvoiceTemplate:
    def test_total_amount_is_required(self) -> None:
        template = get_builtin_template("invoice")
        required = {field.name for field in template.fields if field.required}
        assert "total_amount" in required
        assert "invoice_number" in required

    def test_items_array_has_nested_fields(self) -> None:
        schema = get_builtin_template("invoice").schema
        item_schema = schema["properties"]["items"]["items"]
        assert set(item_schema["properties"]) >= {"name", "quantity", "unit_price", "amount"}


class TestCustomSchema:
    def test_builds_fields_from_schema(self) -> None:
        template = ExtractionTemplate.from_custom_schema(
            key="purchase_order",
            name="采购单",
            schema={
                "type": "object",
                "properties": {
                    "order_no": {"type": "string", "title": "订单号"},
                    "total": {"type": "number", "title": "总金额"},
                },
                "required": ["order_no"],
            },
        )
        assert {field.name for field in template.fields} == {"order_no", "total"}
        assert template.builtin is False

    def test_falls_back_to_field_name_when_title_missing(self) -> None:
        """自定义 schema 常常没写 title，此时用字段名兜底而不是显示空白。"""
        template = ExtractionTemplate.from_custom_schema(
            key="c",
            name="C",
            schema={"type": "object", "properties": {"field_x": {"type": "string"}}},
        )
        assert template.fields[0].label == "field_x"

    def test_required_is_read_from_schema(self) -> None:
        template = ExtractionTemplate.from_custom_schema(
            key="c",
            name="C",
            schema={
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a"],
            },
        )
        required = {field.name for field in template.fields if field.required}
        assert required == {"a"}

    def test_enum_is_preserved(self) -> None:
        template = ExtractionTemplate.from_custom_schema(
            key="c",
            name="C",
            schema={
                "type": "object",
                "properties": {"level": {"type": "string", "enum": ["高", "中", "低"]}},
            },
        )
        assert template.fields[0].enum == ("高", "中", "低")

    def test_nested_array_of_objects_is_reconstructed(self) -> None:
        template = ExtractionTemplate.from_custom_schema(
            key="c",
            name="C",
            schema={
                "type": "object",
                "properties": {
                    "lines": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"sku": {"type": "string", "title": "货号"}},
                            "required": ["sku"],
                        },
                    }
                },
            },
        )
        field = template.fields[0]
        assert field.item_type == "object"
        assert field.item_fields[0].name == "sku"
        assert field.item_fields[0].required is True
        # 子字段的 title 也要被用作标签
        assert template.field_labels()["lines[].sku"] == "货号"

    def test_nullable_type_takes_non_null_branch(self) -> None:
        """形如 ["string", "null"] 的联合类型要取 string，而不是当成未知类型。"""
        template = ExtractionTemplate.from_custom_schema(
            key="c",
            name="C",
            schema={
                "type": "object",
                "properties": {"a": {"type": ["string", "null"]}},
            },
        )
        assert template.fields[0].type == "string"

    def test_generated_schema_round_trips(self) -> None:
        """由自定义 schema 重建的 schema 应当与原意一致。"""
        template = ExtractionTemplate.from_custom_schema(
            key="c",
            name="C",
            schema={
                "type": "object",
                "properties": {"a": {"type": "string", "title": "甲", "description": "说明"}},
                "required": ["a"],
            },
        )
        assert template.schema["required"] == ["a"]
        assert template.schema["properties"]["a"]["title"] == "甲"
        assert template.schema["properties"]["a"]["description"] == "说明"
        assert template.schema["additionalProperties"] is False


class TestFieldSchema:
    def test_scalar_field(self) -> None:
        node = TemplateField(name="a", label="甲", type="string", description="说明").to_schema()
        assert node == {
            "type": ["string", "null"],
            "description": "说明",
            "title": "甲",
        }

    def test_enum_field(self) -> None:
        node = TemplateField(name="a", label="甲", type="string", enum=("X", "Y")).to_schema()
        assert node["enum"] == ["X", "Y"]

    def test_array_of_string_field(self) -> None:
        node = TemplateField(name="a", label="甲", type="array", item_type="string").to_schema()
        assert node["items"] == {"type": "string"}

    def test_array_of_object_field(self) -> None:
        node = TemplateField(
            name="rows",
            label="明细",
            type="array",
            item_type="object",
            item_fields=(TemplateField(name="sku", label="货号", type="string", required=True),),
        ).to_schema()
        assert "string" in node["items"]["properties"]["sku"]["type"]
        assert node["items"]["required"] == ["sku"]
        assert node["items"]["additionalProperties"] is False


class TestFromDict:
    def test_builtin_payload_flag_defaults_to_true(self) -> None:
        template = ExtractionTemplate.from_dict(
            {"key": "k", "name": "N", "fields": [{"name": "a", "label": "甲"}]}
        )
        assert template.builtin is True
        assert template.fields[0].type == "string"

    def test_reads_item_fields(self) -> None:
        template = ExtractionTemplate.from_dict(
            {
                "key": "k",
                "name": "N",
                "fields": [
                    {
                        "name": "rows",
                        "label": "明细",
                        "type": "array",
                        "item_type": "object",
                        "item_fields": [{"name": "sku", "label": "货号", "required": True}],
                    }
                ],
            }
        )
        assert template.fields[0].item_fields[0].required is True
