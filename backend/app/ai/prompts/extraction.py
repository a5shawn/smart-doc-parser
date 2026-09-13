"""抽取任务的 prompt 构造。"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.prompts.base import (
    BASE_SYSTEM_PROMPT,
    DOCUMENT_CLOSE,
    DOCUMENT_OPEN,
    assert_json_keyword,
)
from app.ai.schema_utils import compact_schema
from app.ai.templates import ExtractionTemplate, TemplateField


@dataclass(frozen=True, slots=True)
class PromptPair:
    system: str
    user: str

    def contains_json_keyword(self) -> bool:
        return "json" in f"{self.system} {self.user}".lower()


def _describe_field(field: TemplateField, indent: str = "- ") -> list[str]:
    """把一个字段渲染成给模型看的说明行。"""
    suffix = "（必填）" if field.required else ""
    type_label = {
        "string": "文本",
        "number": "数字",
        "integer": "整数",
        "boolean": "布尔值",
        "array": "数组",
    }.get(field.type, field.type)

    line = f"{indent}{field.name}（{field.label}，{type_label}{suffix}）"
    if field.description:
        line += f"：{field.description}"
    if field.enum:
        line += f" 取值范围：{'、'.join(field.enum)}"

    lines = [line]

    for item in field.item_fields:
        # 递归返回的是一个列表（子字段自己也可能再有子字段），必须用 extend
        lines.extend(_describe_field(item, indent="    - "))

    if field.type == "array" and field.item_type == "string":
        lines.append("    （数组元素为文本）")

    return lines


def build_extraction_prompt(
    *,
    template: ExtractionTemplate,
    document_text: str,
    truncated: bool = False,
) -> PromptPair:
    """构造抽取用的 system / user prompt。

    :param truncated: 文档是否因为超长被截断。必须显式告知模型——
        否则它会认为"没找到的信息就是不存在"，而实际上可能在被省略的部分里。
    """
    field_lines: list[str] = []
    for field in template.fields:
        field_lines.extend(_describe_field(field))

    sections: list[str] = [
        "请从下面的文档文本中抽取信息，输出一个符合给定 JSON Schema 的 json 对象。",
        "",
        f"任务类型：{template.name}",
    ]

    if template.description:
        sections.append(f"任务说明：{template.description}")

    sections.extend(
        [
            "",
            "【字段说明】",
            "\n".join(field_lines),
            "",
            "【JSON Schema】",
            "输出必须满足以下 Schema（字段名、类型、取值范围都要符合）：",
            "```json",
            _schema_text(template),
            "```",
        ]
    )

    if template.prompt_hint:
        sections.extend(["", "【业务规则】", template.prompt_hint])

    if truncated:
        sections.extend(
            [
                "",
                "【重要提示】",
                "文档过长，中间部分内容已被省略（省略处有明确标记）。"
                "如果某个字段可能位于被省略的部分，请填 null，不要根据上下文猜测。",
            ]
        )

    sections.extend(
        [
            "",
            "【文档文本】",
            DOCUMENT_OPEN,
            document_text,
            DOCUMENT_CLOSE,
            "",
            "现在请输出 json 对象。",
        ]
    )

    user_prompt = "\n".join(sections)
    assert_json_keyword(BASE_SYSTEM_PROMPT, user_prompt)

    return PromptPair(system=BASE_SYSTEM_PROMPT, user=user_prompt)


def _schema_text(template: ExtractionTemplate) -> str:
    """渲染写进 prompt 的 schema。

    刻意用紧凑格式（无缩进）：字段多时能省下可观的输入 token，
    而模型对 JSON 的空白完全不敏感。这个函数的输出会被逐字塞进 prompt，
    因此任何格式美化都是纯粹的成本。
    """
    import json

    return json.dumps(
        compact_schema(template.schema),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_repair_prompt(*, previous_output: str, error_hint: str) -> str:
    """构造「上次输出不是合法 JSON」的纠正 prompt。

    措辞要点：明确说"只输出 json"，并附上错误位置。
    不要重发一遍文档——把上一次的输出和错误提示一起发过去就够了，
    这样重试的输入 token 远小于重跑一遍完整 prompt。
    """
    prompt = (
        "你上一次的输出不是合法的 json 对象，解析失败了。\n"
        f"失败原因：{error_hint}\n\n"
        "请重新输出一次。这一次必须满足：\n"
        "1. 只输出 json 对象本身，不要有任何解释文字；\n"
        "2. 不要用 Markdown 代码块包裹；\n"
        "3. 确保所有引号、括号成对闭合；\n"
        "4. 对象与数组的最后一个元素后面不要有逗号。\n\n"
        "你上一次的输出是：\n"
        f"{previous_output[:4000]}"
    )
    assert_json_keyword(prompt)
    return prompt
