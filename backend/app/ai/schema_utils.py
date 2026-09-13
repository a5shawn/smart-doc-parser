"""JSON Schema 的规范化、压缩、校验与类型矫正。

核心立场：**不指望模型端约束结构**。
DeepSeek 不支持 ``response_format: json_schema`` 严格模式（实测返回
``This response_format type is unavailable now``），schema 只能以文本形式写进 prompt。
所以结构的正确性完全靠服务端这四步兜底：

规范化 → 压缩 → 类型矫正 → 校验
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.core.errors import InvalidSchemaError

#: 压缩时可以直接丢弃的键——它们只影响文档化，对模型理解字段没有帮助，
#: 但会实实在在占用 prompt token（每次调用都要付一遍）
_STRIPPABLE_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "examples",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
        "additionalItems",
        "unevaluatedProperties",
    }
)

#: 模型用来表达"没抽到"的各种写法。注意不含中文的"无"/"未知"——
#: 那些在合同里可能是有意义的字面内容，不能当成空值抹掉。
_NULLISH_STRINGS = frozenset({"null", "none", "n/a", "na", "nil", "undefined", ""})

#: 从数字字符串里剥掉的常见装饰
_NUMERIC_NOISE = str.maketrans("", "", "¥$€£, 　元人民币")


@dataclass(frozen=True, slots=True)
class SchemaIssue:
    """一条校验发现的问题。"""

    path: str
    kind: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "message": self.message}


# ============================================================================
# 规范化与压缩
# ============================================================================
def normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """补齐让 schema 更严格、对模型更明确的缺省项。

    - 所有 object 加 ``additionalProperties: false``：显著减少模型编造字段
      （形如 ``other_info``、``notes`` 这类 schema 里根本没有的键）
    - 根节点若不是 object 则拒绝：后续所有逻辑都假设抽取结果是一个对象

    :raises InvalidSchemaError: schema 本身不合法
    """
    if not isinstance(schema, dict):
        raise InvalidSchemaError("JSON Schema 必须是一个对象")

    normalized = copy.deepcopy(schema)

    if "type" not in normalized and "properties" in normalized:
        normalized["type"] = "object"

    if normalized.get("type") != "object":
        raise InvalidSchemaError(
            "根节点的 type 必须是 object：抽取结果需要是一个字段集合，不能是数组或标量"
        )

    _close_objects(normalized)
    _check_valid(normalized)
    return normalized


def _close_objects(node: Any) -> None:
    """递归给所有 object 补上 additionalProperties: false。"""
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            node.setdefault("additionalProperties", False)
        for value in node.values():
            _close_objects(value)
    elif isinstance(node, list):
        for item in node:
            _close_objects(item)


def _check_valid(schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidSchemaError(
            f"JSON Schema 格式有误：{exc.message}",
            details={"path": list(exc.path)},
        ) from exc


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """压掉对模型无用的键，返回紧凑的 JSON 字符串所需的 dict。"""
    compacted = copy.deepcopy(schema)
    _strip_keys(compacted)
    return compacted


def _strip_keys(node: Any) -> None:
    if isinstance(node, dict):
        for key in _STRIPPABLE_KEYS:
            node.pop(key, None)
        for value in node.values():
            _strip_keys(value)
    elif isinstance(node, list):
        for item in node:
            _strip_keys(item)


def schema_to_prompt_text(schema: dict[str, Any], *, indent: int | None = None) -> str:
    """把 schema 渲染成写进 prompt 的文本。

    用紧凑分隔符（``separators``）而不是默认的带空格格式：字段多时能省下
    可观的 token，而模型对 JSON 的空白并不敏感。
    """
    return json.dumps(
        compact_schema(schema),
        ensure_ascii=False,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )


# ============================================================================
# 类型矫正
# ============================================================================
def _expected_type(schema_node: dict[str, Any]) -> str | None:
    node_type = schema_node.get("type")
    if isinstance(node_type, list):
        # 形如 ["string", "null"]：取第一个非 null 的类型作为主类型
        non_null = [item for item in node_type if item != "null"]
        return non_null[0] if non_null else None
    return node_type if isinstance(node_type, str) else None


def _coerce_scalar(value: Any, expected: str | None) -> tuple[Any, bool]:
    """尝试把标量转成期望类型。返回 ``(值, 是否发生了转换)``。"""
    if expected in {"number", "integer"} and isinstance(value, str):
        cleaned = value.strip().translate(_NUMERIC_NOISE)
        try:
            number = float(cleaned)
        except ValueError:
            return value, False
        if expected == "integer" and number.is_integer():
            return int(number), True
        return (int(number) if expected == "integer" else number), True

    if expected == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "是"}:
            return True, True
        if lowered in {"false", "no", "否"}:
            return False, True

    if expected == "string" and isinstance(value, (int, float)) and not isinstance(value, bool):
        # 单号、电话号码之类模型偶尔会返回数字，转回字符串
        return str(value), True

    return value, False


def coerce_data(data: Any, schema: dict[str, Any]) -> tuple[Any, list[SchemaIssue]]:
    """按 schema 递归矫正类型。

    典型需要矫正的情况：金额返回成 ``"¥1,280,000 元"``、没抽到的字段返回成字符串
    ``"null"``、数组字段只返回了一个值而不是列表。

    :returns: ``(矫正后的数据, 矫正记录)``。矫正记录会作为 warnings 展示给用户，
    因为它意味着模型输出不够规范，用户看到的结果可能值得复核。
    """
    issues: list[SchemaIssue] = []
    coerced = _coerce_node(data, schema, path="", issues=issues)
    return coerced, issues


def _coerce_node(value: Any, node: dict[str, Any], *, path: str, issues: list[SchemaIssue]) -> Any:
    # 先做 null 归一化。
    #
    # **对所有类型都生效，包括字符串**：模型在 JSON mode 下经常用字面量
    # "null" / "N/A" / 空串来表示"没抽到"。如果只对非字符串类型处理，
    # 字符串字段就会留下一个内容为 "null" 的字符串——它在界面上看着像有值，
    # 实际是噪声，而且会让"是否抽到"的判断全部失真。
    #
    # 代价是一个内容确实为 "null" 的真实字符串会被抹成空值。这种情况极其罕见，
    # 且会被记录成 warning 展示给用户，属于可接受的取舍。
    if isinstance(value, str) and value.strip().lower() in _NULLISH_STRINGS:
        issues.append(
            SchemaIssue(
                path=path or "$",
                kind="nullish_string",
                message=f"字段被填成了 {value!r}，已归一化为 null",
            )
        )
        return None

    expected = _expected_type(node)

    if expected == "object" or "properties" in node:
        if not isinstance(value, dict):
            issues.append(
                SchemaIssue(
                    path=path or "$",
                    kind="type_mismatch",
                    message=f"期望对象，实际是 {type(value).__name__}",
                )
            )
            return value

        properties: dict[str, Any] = node.get("properties", {})
        result: dict[str, Any] = {}
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else key
            child_schema = properties.get(key)
            if child_schema is None:
                # schema 里没定义的字段：保留原值但记一条，多半是模型编的
                issues.append(
                    SchemaIssue(
                        path=child_path,
                        kind="unexpected_field",
                        message="该字段不在模板定义中，可能是模型自行添加的",
                    )
                )
                result[key] = item
                continue
            result[key] = _coerce_node(item, child_schema, path=child_path, issues=issues)
        return result

    if expected == "array":
        if value is None:
            return []
        if not isinstance(value, list):
            issues.append(
                SchemaIssue(
                    path=path or "$",
                    kind="wrapped_scalar_as_array",
                    message="期望数组但只返回了单个值，已包装成长度为 1 的数组",
                )
            )
            value = [value]
        item_schema = node.get("items", {})
        return [
            _coerce_node(item, item_schema, path=f"{path}[{index}]", issues=issues)
            for index, item in enumerate(value)
        ]

    coerced, changed = _coerce_scalar(value, expected)
    if changed:
        issues.append(
            SchemaIssue(
                path=path or "$",
                kind="type_coerced",
                message=f"已从 {value!r} 转换为 {expected}",
            )
        )
    return coerced


# ============================================================================
# 校验
# ============================================================================
def _check_required_nulls(
    data: Any, schema: dict[str, Any], *, path: str = ""
) -> list[SchemaIssue]:
    """检查必填字段是否被填成了 null。

    JSON Schema 的 ``required`` **只检查键是否存在**，而模型的标准做法是
    ``{"effective_date": null}``——键在，值是空。仅靠 ``required`` 完全检查不出来，
    用户会拿到一份"必填项是空的"却没有任何提示的结果。

    因此这里对 null 做显式检查。会递归进数组元素，覆盖简历、发票这类嵌套结构。
    """
    issues: list[SchemaIssue] = []

    if isinstance(data, list):
        item_schema = schema.get("items", {})
        for index, item in enumerate(data):
            issues.extend(_check_required_nulls(item, item_schema, path=f"{path}[{index}]"))
        return issues

    if not isinstance(data, dict):
        return issues

    for name in schema.get("required", []):
        if data.get(name) is None:
            issues.append(
                SchemaIssue(
                    path=f"{path}.{name}" if path else name,
                    kind="missing_required",
                    message=f"必填字段「{name}」没有抽到",
                )
            )

    for name, node in schema.get("properties", {}).items():
        if name in data and isinstance(node, dict):
            issues.extend(
                _check_required_nulls(data[name], node, path=f"{path}.{name}" if path else name)
            )

    return issues


def validate_data(data: Any, schema: dict[str, Any]) -> list[SchemaIssue]:
    """用 JSON Schema 校验矫正后的数据。

    校验失败**不抛异常**：模型偶尔漏一个字段是常态，为此让整个任务失败、
    让用户白等一次调用是更糟的选择。这些发现作为 ``warnings`` 随结果一起返回，
    由用户判断是否可接受。
    """
    validator = Draft202012Validator(schema)
    issues: list[SchemaIssue] = [
        # 必填但为 null 的情况 JSON Schema 查不出来，单独补一轮
        *_check_required_nulls(data, schema),
    ]

    for error in sorted(validator.iter_errors(data), key=lambda item: list(item.path)):
        path = ".".join(str(part) for part in error.path) or "$"

        # 跳过 JSON Schema 的 required 错误：上面的 _check_required_nulls 是它的超集
        # （同时覆盖"键不存在"和"键存在但值为 null"），而且给出的路径直接指向字段本身。
        # 不跳过的话，一个缺失的必填字段会报两条一模一样的 warning。
        if error.validator == "required":
            continue

        if error.validator == "additionalProperties":
            issues.append(
                SchemaIssue(
                    path=path,
                    kind="unexpected_field",
                    message=error.message,
                )
            )
        elif error.validator == "type":
            issues.append(SchemaIssue(path=path, kind="type_mismatch", message=error.message))
        elif error.validator == "enum":
            issues.append(SchemaIssue(path=path, kind="enum_mismatch", message=error.message))
        else:
            issues.append(
                SchemaIssue(
                    path=path,
                    kind=str(error.validator),
                    message=error.message,
                )
            )

    return dedupe_issues(issues)


def dedupe_issues(issues: list[SchemaIssue]) -> list[SchemaIssue]:
    """同一路径同一类型的问题只保留第一条。

    模型把整数字段写成字符串时，矫正和校验会各报一次，对用户是重复噪声。
    """
    seen: set[tuple[str, str]] = set()
    result: list[SchemaIssue] = []
    for issue in issues:
        key = (issue.path, issue.kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
