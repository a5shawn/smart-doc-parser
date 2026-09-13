"""JSON Schema 工具与类型矫正的单元测试。"""

from __future__ import annotations

import pytest

from app.ai.schema_utils import (
    SchemaIssue,
    coerce_data,
    compact_schema,
    dedupe_issues,
    normalize_schema,
    schema_to_prompt_text,
    validate_data,
)
from app.core.errors import InvalidSchemaError

CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "party_a": {"type": "string", "title": "甲方"},
        "amount": {"type": "number", "title": "金额"},
        "currency": {"type": "string", "enum": ["CNY", "USD"], "title": "币种"},
        "signed": {"type": "boolean", "title": "已签署"},
        "tags": {"type": "array", "items": {"type": "string"}, "title": "标签"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "qty": {"type": "integer"}},
            },
            "title": "明细",
        },
    },
    "required": ["party_a"],
}


class TestNormalizeSchema:
    def test_adds_additional_properties_false(self) -> None:
        """这是抑制模型编造字段最有效的一招。"""
        schema = normalize_schema(CONTRACT_SCHEMA)
        assert schema["additionalProperties"] is False

    def test_applies_recursively(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        item_schema = schema["properties"]["items"]["items"]
        assert item_schema["additionalProperties"] is False

    def test_infers_object_type_from_properties(self) -> None:
        schema = normalize_schema({"properties": {"a": {"type": "string"}}})
        assert schema["type"] == "object"

    def test_does_not_mutate_input(self) -> None:
        original = {"type": "object", "properties": {"a": {"type": "string"}}}
        normalize_schema(original)
        assert "additionalProperties" not in original

    @pytest.mark.parametrize("root_type", ["array", "string", "integer"])
    def test_rejects_non_object_root(self, root_type: str) -> None:
        """抽取结果必须是一个字段集合，根节点是数组或标量没法用。"""
        with pytest.raises(InvalidSchemaError, match="object"):
            normalize_schema({"type": root_type})

    def test_rejects_invalid_schema(self) -> None:
        with pytest.raises(InvalidSchemaError):
            normalize_schema({"type": "object", "properties": {"a": {"type": "不存在的类型"}}})

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(InvalidSchemaError):
            normalize_schema(["not", "a", "schema"])  # type: ignore[arg-type]

    def test_preserves_enum_and_required(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        assert schema["properties"]["currency"]["enum"] == ["CNY", "USD"]
        assert schema["required"] == ["party_a"]


class TestCompactSchema:
    def test_strips_documentation_only_keys(self) -> None:
        schema = compact_schema(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://example.com/s.json",
                "examples": [{"a": 1}],
                "default": {},
                "type": "object",
                "properties": {"a": {"type": "string", "title": "甲"}},
            }
        )
        assert "$schema" not in schema
        assert "$id" not in schema
        assert "examples" not in schema
        assert "default" not in schema

    def test_keeps_title_and_description(self) -> None:
        """title 是字段的中文标签，description 是给模型的说明，都不能删。"""
        schema = compact_schema(
            {
                "type": "object",
                "properties": {"a": {"type": "string", "title": "甲", "description": "说明"}},
            }
        )
        assert schema["properties"]["a"]["title"] == "甲"
        assert schema["properties"]["a"]["description"] == "说明"

    def test_strips_nested_keys(self) -> None:
        schema = compact_schema(
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "examples": [1], "properties": {}},
                    }
                },
            }
        )
        assert "examples" not in schema["properties"]["items"]["items"]

    def test_prompt_text_is_compact(self) -> None:
        """紧凑格式能省下可观的输入 token，而模型对 JSON 空白不敏感。"""
        text = schema_to_prompt_text(CONTRACT_SCHEMA)
        assert "\n" not in text
        assert ", " not in text
        assert "甲方" in text


class TestCoerceData:
    def test_nullish_strings_become_none(self) -> None:
        data, issues = coerce_data({"party_a": "null", "amount": "N/A"}, CONTRACT_SCHEMA)
        assert data["party_a"] is None
        assert data["amount"] is None
        assert any(issue.kind == "nullish_string" for issue in issues)

    def test_empty_string_becomes_none(self) -> None:
        data, _ = coerce_data({"party_a": ""}, CONTRACT_SCHEMA)
        assert data["party_a"] is None

    def test_currency_decorated_string_becomes_number(self) -> None:
        """模型很爱返回 "¥1,280,000 元"，直接拿去做数据比对会全错。"""
        data, issues = coerce_data({"amount": "¥1,280,000 元"}, CONTRACT_SCHEMA)
        assert data["amount"] == 1280000.0
        assert any(issue.kind == "type_coerced" for issue in issues)

    def test_plain_numeric_string_becomes_number(self) -> None:
        data, _ = coerce_data({"amount": "1280000"}, CONTRACT_SCHEMA)
        assert data["amount"] == 1280000.0

    def test_non_numeric_string_left_alone(self) -> None:
        """转不了就原样保留，交给校验阶段报 warning，而不是悄悄丢掉数据。"""
        data, issues = coerce_data({"amount": "以合同约定为准"}, CONTRACT_SCHEMA)
        assert data["amount"] == "以合同约定为准"
        assert not any(issue.kind == "type_coerced" for issue in issues)

    def test_scalar_wrapped_into_array(self) -> None:
        data, issues = coerce_data({"tags": "重要"}, CONTRACT_SCHEMA)
        assert data["tags"] == ["重要"]
        assert any(issue.kind == "wrapped_scalar_as_array" for issue in issues)

    def test_nested_array_of_objects_coerced(self) -> None:
        data, _ = coerce_data({"items": [{"qty": "12"}]}, CONTRACT_SCHEMA)
        assert data["items"][0]["qty"] == 12

    def test_unexpected_field_is_flagged_but_kept(self) -> None:
        """模型自创的字段要记下来，但不能直接删——万一它承载了有用信息。"""
        data, issues = coerce_data({"party_a": "甲公司", "notes": "口头约定"}, CONTRACT_SCHEMA)
        assert data["notes"] == "口头约定"
        assert any(issue.kind == "unexpected_field" for issue in issues)

    def test_missing_fields_are_not_invented(self) -> None:
        data, _ = coerce_data({"party_a": "甲公司"}, CONTRACT_SCHEMA)
        assert "amount" not in data

    def test_boolean_strings(self) -> None:
        schema = {"type": "object", "properties": {"signed": {"type": "boolean"}}}
        assert coerce_data({"signed": "true"}, schema)[0]["signed"] is True
        assert coerce_data({"signed": "否"}, schema)[0]["signed"] is False

    def test_number_returned_for_string_field(self) -> None:
        schema = {"type": "object", "properties": {"phone": {"type": "string"}}}
        data, _ = coerce_data({"phone": 13800000000}, schema)
        assert data["phone"] == "13800000000"

    def test_issue_path_records_field_location(self) -> None:
        _, issues = coerce_data({"items": [{"qty": "abc"}]}, CONTRACT_SCHEMA)
        assert all(issue.path for issue in issues)


class TestValidateData:
    def test_valid_data_produces_no_issues(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        assert validate_data({"party_a": "甲公司", "amount": 100}, schema) == []

    def test_missing_required_field(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        issues = validate_data({"amount": 100}, schema)

        missing = [issue for issue in issues if issue.kind == "missing_required"]
        assert len(missing) == 1
        # 路径要精确到字段名，前端才能高亮到具体那一项
        assert missing[0].path == "party_a"

    def test_unexpected_field(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        issues = validate_data({"party_a": "甲", "bogus": 1}, schema)
        assert any(issue.kind == "unexpected_field" for issue in issues)

    def test_type_mismatch(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        issues = validate_data({"party_a": "甲", "amount": "不是数字"}, schema)
        assert any(issue.kind == "type_mismatch" for issue in issues)

    def test_enum_mismatch(self) -> None:
        schema = normalize_schema(CONTRACT_SCHEMA)
        issues = validate_data({"party_a": "甲", "currency": "日元"}, schema)
        assert any(issue.kind == "enum_mismatch" for issue in issues)

    def test_nested_required_field(self) -> None:
        schema = normalize_schema(
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    }
                },
            }
        )
        issues = validate_data({"items": [{"qty": 1}]}, schema)
        missing = [issue for issue in issues if issue.kind == "missing_required"]
        assert missing and "name" in missing[0].path

    def test_validation_never_raises(self) -> None:
        """校验失败不能让任务失败——模型偶尔漏字段是常态。"""
        schema = normalize_schema(CONTRACT_SCHEMA)
        issues = validate_data({"completely": "wrong", "shape": 1}, schema)
        assert isinstance(issues, list)


NULLABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "party_a": {"type": ["string", "null"], "title": "甲方"},
        "amount": {"type": ["number", "null"], "title": "金额"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": ["string", "null"]}},
                "required": ["name"],
            },
        },
    },
    "required": ["party_a"],
}


class TestRequiredNullDetection:
    """回归测试：模型"没抽到就填 null"，系统不能把这当成类型错误。

    同时，必填字段被填成 null 必须被单独识别出来——
    JSON Schema 的 required 只检查键是否存在，而模型返回的是
    ``{"party_a": null}``，键是在的，靠 required 完全查不出来。
    """

    def test_null_for_optional_field_is_not_an_issue(self) -> None:
        schema = normalize_schema(NULLABLE_SCHEMA)
        assert validate_data({"party_a": "甲公司", "amount": None}, schema) == []

    def test_null_for_required_field_is_reported(self) -> None:
        schema = normalize_schema(NULLABLE_SCHEMA)
        issues = validate_data({"party_a": None, "amount": 100}, schema)

        missing = [issue for issue in issues if issue.kind == "missing_required"]
        assert len(missing) == 1
        assert missing[0].path == "party_a"

    def test_absent_required_field_is_reported_once(self) -> None:
        """键不存在与键为 null 是同一件事，不能报两条。"""
        schema = normalize_schema(NULLABLE_SCHEMA)
        issues = validate_data({"amount": 100}, schema)
        missing = [issue for issue in issues if issue.kind == "missing_required"]
        assert len(missing) == 1

    def test_nested_required_null_is_reported(self) -> None:
        schema = normalize_schema(NULLABLE_SCHEMA)
        issues = validate_data({"party_a": "甲公司", "rows": [{"name": None}]}, schema)

        missing = [issue for issue in issues if issue.kind == "missing_required"]
        assert len(missing) == 1
        # 路径要能定位到数组里的具体元素
        assert missing[0].path == "rows[0].name"

    def test_null_in_array_element_is_not_a_type_error(self) -> None:
        schema = normalize_schema(NULLABLE_SCHEMA)
        issues = validate_data({"party_a": "甲", "rows": [{"name": "服务费"}]}, schema)
        assert issues == []

    def test_fully_populated_data_has_no_issues(self) -> None:
        schema = normalize_schema(NULLABLE_SCHEMA)
        data = {"party_a": "甲公司", "amount": 100, "rows": [{"name": "服务费"}]}
        assert validate_data(data, schema) == []


class TestDedupeIssues:
    def test_same_path_and_kind_kept_once(self) -> None:
        issues = [
            SchemaIssue(path="amount", kind="type_coerced", message="第一次"),
            SchemaIssue(path="amount", kind="type_coerced", message="第二次"),
        ]
        assert len(dedupe_issues(issues)) == 1

    def test_different_kinds_on_same_path_are_kept(self) -> None:
        issues = [
            SchemaIssue(path="amount", kind="type_coerced", message="a"),
            SchemaIssue(path="amount", kind="enum_mismatch", message="b"),
        ]
        assert len(dedupe_issues(issues)) == 2

    def test_preserves_order(self) -> None:
        issues = [
            SchemaIssue(path="a", kind="x", message="1"),
            SchemaIssue(path="b", kind="y", message="2"),
        ]
        assert [issue.path for issue in dedupe_issues(issues)] == ["a", "b"]

    def test_issue_serializes_to_dict(self) -> None:
        issue = SchemaIssue(path="a.b", kind="type_mismatch", message="消息")
        assert issue.to_dict() == {"path": "a.b", "kind": "type_mismatch", "message": "消息"}
