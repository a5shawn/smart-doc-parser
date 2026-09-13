"""抽取模板：字段定义 → JSON Schema → prompt。

**Schema 驱动**是这套设计的核心：模板只声明一次字段（名称、中文标签、类型、
描述、是否必填），下游三件事全部由它推导出来——

1. 写进 prompt 的 JSON Schema
2. 服务端校验模型输出用的 JSON Schema
3. 前端渲染结果卡片用的字段标签

三者共用一份定义，就不会出现"前端展示的字段和后端校验的字段对不上"
这类改一处忘一处的问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.errors import TemplateNotFoundError

BUILTIN_DIR = Path(__file__).parent / "builtin"

#: 支持的字段类型。刻意保持精简——类型越多，前端渲染与校验的分支就越多，
#: 而抽取场景里 95% 的需求用这几种就够。
SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean"})


@dataclass(frozen=True, slots=True)
class TemplateField:
    """模板中的一个字段。"""

    name: str
    #: 中文标签。前端卡片标题、prompt 说明都用它
    label: str
    type: str
    description: str = ""
    required: bool = False
    #: 限定取值范围。给出枚举能明显提升模型输出的稳定性
    enum: tuple[str, ...] = ()
    #: type == "array" 时元素类型：string 或 object
    item_type: str | None = None
    #: type == "array" 且 item_type == "object" 时的子字段
    item_fields: tuple[TemplateField, ...] = ()

    def to_schema(self) -> dict[str, Any]:
        # 所有字段都允许 null。
        #
        # 这不是放松要求，而是**必须**：prompt 里明确要求模型"没抽到的字段填 null"，
        # 如果 schema 写的是 {"type": "string"}，模型老老实实返回 null 反而会被校验
        # 判成类型错误——用户看到一堆"类型不匹配"的告警，实际是系统自己在报假警。
        #
        # 真正"必填却没抽到"的判断交给 validate_data 里针对 null 的显式检查，
        # 而不是指望 JSON Schema 的 required（required 只检查键是否存在，
        # 而模型返回的是 {"effective_date": null}，键是存在的）。
        node: dict[str, Any] = {"type": [self.type, "null"]}

        if self.description:
            node["description"] = self.description
        # title 会被前端当作字段标签，也帮助模型理解字段含义
        node["title"] = self.label

        if self.enum:
            node["enum"] = list(self.enum)

        if self.type == "array":
            if self.item_type == "object":
                properties = {item.name: item.to_schema() for item in self.item_fields}
                required = [item.name for item in self.item_fields if item.required]
                item_node: dict[str, Any] = {
                    "type": "object",
                    "properties": properties,
                    "additionalProperties": False,
                }
                if required:
                    item_node["required"] = required
            else:
                item_node = {"type": self.item_type or "string"}
            node["items"] = item_node

        return node

    @classmethod
    def from_schema_node(
        cls, name: str, node: dict[str, Any], *, required: bool = False
    ) -> TemplateField:
        """从 JSON Schema 的单个属性反推字段定义（用于自定义模板）。"""
        node_type = node.get("type", "string")
        if isinstance(node_type, list):
            node_type = next((item for item in node_type if item != "null"), "string")

        item_fields: tuple[TemplateField, ...] = ()
        item_type: str | None = None

        if node_type == "array":
            items = node.get("items", {})
            item_type = items.get("type", "string")
            if item_type == "object" and items.get("properties"):
                item_required = set(items.get("required", []))
                item_fields = tuple(
                    cls.from_schema_node(key, value, required=key in item_required)
                    for key, value in items["properties"].items()
                )

        return cls(
            name=name,
            # 自定义 schema 里没有中文标签，退化成字段名
            label=node.get("title") or name,
            type=node_type,
            description=node.get("description", ""),
            required=required,
            enum=tuple(node.get("enum", [])),
            item_type=item_type,
            item_fields=item_fields,
        )


@dataclass(frozen=True, slots=True)
class ExtractionTemplate:
    """一套完整的抽取模板。"""

    key: str
    name: str
    description: str
    #: 适用的文档类型，用于在前端给出提示
    doc_types: tuple[str, ...] = ()
    #: 附加给模型的业务规则，例如金额单位、日期格式等
    prompt_hint: str = ""
    fields: tuple[TemplateField, ...] = ()
    #: 内置模板不可删除
    builtin: bool = True
    #: 自定义模板在数据库中的 id
    template_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def schema(self) -> dict[str, Any]:
        """本模板对应的 JSON Schema。"""
        return {
            "type": "object",
            "properties": {item.name: item.to_schema() for item in self.fields},
            "required": [item.name for item in self.fields if item.required],
            "additionalProperties": False,
        }

    def field_labels(self) -> dict[str, str]:
        """字段名 → 中文标签。

        前端渲染结果卡片时用它把 ``party_a`` 显示成「甲方」，
        遇到子字段用 ``work_experience[].company`` 这种带方括号的路径表示。
        """
        labels: dict[str, str] = {}
        for item in self.fields:
            labels[item.name] = item.label
            for child in item.item_fields:
                labels[f"{item.name}[].{child.name}"] = child.label
        return labels

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExtractionTemplate:
        fields = tuple(
            TemplateField(
                name=item["name"],
                label=item.get("label") or item["name"],
                type=item.get("type", "string"),
                description=item.get("description", ""),
                required=bool(item.get("required", False)),
                enum=tuple(item.get("enum", [])),
                item_type=item.get("item_type"),
                item_fields=tuple(
                    TemplateField(
                        name=child["name"],
                        label=child.get("label") or child["name"],
                        type=child.get("type", "string"),
                        description=child.get("description", ""),
                        required=bool(child.get("required", False)),
                        enum=tuple(child.get("enum", [])),
                    )
                    for child in item.get("item_fields", [])
                ),
            )
            for item in payload.get("fields", [])
        )

        return cls(
            key=payload["key"],
            name=payload["name"],
            description=payload.get("description", ""),
            doc_types=tuple(payload.get("doc_types", [])),
            prompt_hint=payload.get("prompt_hint", ""),
            fields=fields,
            builtin=bool(payload.get("builtin", True)),
        )

    @classmethod
    def from_custom_schema(
        cls,
        *,
        key: str,
        name: str,
        schema: dict[str, Any],
        description: str = "",
        prompt_hint: str = "",
        template_id: str | None = None,
    ) -> ExtractionTemplate:
        """由用户提供的 JSON Schema 构造模板。"""
        properties: dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))

        fields = tuple(
            TemplateField.from_schema_node(field_name, node, required=field_name in required)
            for field_name, node in properties.items()
        )

        return cls(
            key=key,
            name=name,
            description=description,
            prompt_hint=prompt_hint,
            fields=fields,
            builtin=False,
            template_id=template_id,
        )


@lru_cache(maxsize=1)
def load_builtin_templates() -> dict[str, ExtractionTemplate]:
    """加载内置模板。结果缓存，文件在进程运行期间不会变。"""
    if not BUILTIN_DIR.is_dir():  # pragma: no cover - 打包异常
        raise RuntimeError(f"内置模板目录不存在：{BUILTIN_DIR}")

    templates: dict[str, ExtractionTemplate] = {}
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        template = ExtractionTemplate.from_dict(payload)
        templates[template.key] = template

    return templates


def get_builtin_template(key: str) -> ExtractionTemplate:
    template = load_builtin_templates().get(key)
    if template is None:
        raise TemplateNotFoundError(
            f"内置模板 {key} 不存在",
            details={"available": sorted(load_builtin_templates())},
        )
    return template


def builtin_template_keys() -> frozenset[str]:
    return frozenset(load_builtin_templates())
