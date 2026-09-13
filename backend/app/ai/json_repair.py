"""从模型输出里解析出 JSON。

即使开了 JSON mode，模型仍可能：

- 把 JSON 包在 Markdown 代码块里（训练数据里的习惯太强）
- 在 JSON 前后加一句"好的，以下是抽取结果："
- 输出结尾多一个逗号
- 因为撞上 max_tokens 而截断

这些都属于**可预期**的输出噪声，应当靠修复阶梯消化掉，而不是直接判定失败让用户重跑——
重跑一次既要花钱也要多等几秒。修复阶梯逐级降级：

1. 直接解析
2. 剥掉 Markdown 围栏
3. 用 JSON 解码器扫描出第一个完整可解析的对象
4. 去掉对象/数组里多余的尾逗号
5. 补全被截断的括号（按嵌套栈逆序，尽力而为）
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.core.errors import LLMInvalidJSONError

#: ```json ... ``` 或 ``` ... ```
_FENCE_PATTERN = re.compile(r"```(?:json|JSON)?\s*(.*?)\s*```", re.DOTALL)
#: 对象/数组里最后一个元素后面多余的逗号
_TRAILING_COMMA_PATTERN = re.compile(r",\s*([}\]])")

#: 扫描候选对象时的最大尝试次数。
#: 正常输出里 ``{`` 不会很多；设上限是为了防止一份超长文档被原样回显时
#: 扫描退化成 O(n²)。
_MAX_SCAN_ATTEMPTS = 200


def _try_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _strip_fence(text: str) -> str | None:
    match = _FENCE_PATTERN.search(text)
    return match.group(1) if match else None


def _scan_json_objects(text: str) -> list[str]:
    """扫描出所有能独立解析成功的 JSON 对象子串。

    比"取第一个 ``{`` 到最后一个 ``}``"健壮得多：模型经常在 JSON 前面提到 schema，
    例如 ``根据{schema}的要求，结果如下：{"a":1}``。朴素的切片会把 ``{schema}``
    也圈进来，导致一个本来完全正常的输出被判为解析失败。

    ``JSONDecoder.raw_decode`` 会从指定位置尝试解析一个完整的 JSON 值并返回结束位置，
    正好用来做这件事。
    """
    decoder = json.JSONDecoder()
    found: list[str] = []
    attempts = 0

    for index, char in enumerate(text):
        if char != "{":
            continue

        attempts += 1
        if attempts > _MAX_SCAN_ATTEMPTS:
            break

        try:
            value, end = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            continue

        if isinstance(value, dict):
            found.append(text[index:end])

    return found


def _remove_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_PATTERN.sub(r"\1", text)


def _close_unbalanced(text: str) -> str | None:
    """为被截断的 JSON 补上缺失的右括号。

    必须按**嵌套栈逆序**补：``{"items": [{"name": "x"`` 缺的是 ``}]}``
    而不是 ``]}}``——后者看上去像补全了，实际上仍然是非法 JSON，
    而且报错位置会指向别处，极难定位。

    仅作为最后手段：补出来的结构通常是残缺的，调用方应结合
    ``finish_reason == "length"`` 判断这其实是"输出被截断"而非"格式错误"。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    #: 「安全截断点」——从文本开头切到这里，剩下的部分一定是完整的键值对序列。
    #: 只在**逗号之后**或**开括号之后**更新：切在冒号后面会留下一个没有值的键
    #: （``{"party_b"``），看似补全了实则仍然非法。
    safe_cut_end = 0

    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if char in "{[":
            stack.append(char)
            safe_cut_end = index + 1
        elif char in "}]":
            if not stack:
                # 右括号比左括号还多，说明这不是"被截断"而是输出本身就坏了
                return None
            stack.pop()
            safe_cut_end = index + 1
        elif char == ",":
            # 切在逗号之前，丢掉这个逗号本身
            safe_cut_end = index

    if not stack:
        return None

    truncated = text
    if in_string:
        # 截断处卡在一个没写完的字符串里，退回到最后一个安全截断点
        truncated = text[:safe_cut_end].rstrip()
        if not truncated:
            return None

    # 按嵌套栈**逆序**闭合：先关最内层。
    # 顺序写反（例如统一先补 ] 再补 }）得到的是 `]}}` 这种看着像补全、
    # 实际仍然非法的结果，而且报错位置会指向别处，极难定位。
    closing = "".join("}" if opener == "{" else "]" for opener in reversed(stack))

    # 截断点后可能残留一个多余逗号（例如切在数组中间时的 ``[1,``），顺手清掉
    return _remove_trailing_commas(truncated + closing)


def parse_json_lenient(raw: str) -> tuple[Any, list[str]]:
    """尽最大努力从模型输出中解析出 JSON。

    :returns: ``(解析结果, 应用过的修复动作说明)``
    :raises LLMInvalidJSONError: 所有修复手段都失败
    """
    if not raw or not raw.strip():
        # deepseek-flash 是推理模型，max_tokens 过小时思考会吃光全部预算，
        # 表现为"调用成功但 content 是空的"——这是最容易误判成模型故障的现象。
        raise LLMInvalidJSONError(
            "模型返回了空内容。若使用推理模型，通常是因为 max_tokens 过小，"
            "思考过程耗尽了全部输出预算。请调大 LLM_MAX_TOKENS。",
            details={"raw_length": len(raw or "")},
        )

    text = raw.strip()

    # ---- 第一轮：直接解析 ----
    direct = _try_loads(text)
    if direct is not None:
        return direct, []

    # ---- 构造候选文本，逐级降级 ----
    # 每个候选记录 (标签, 文本)，标签会作为"修复动作"返回，便于观察模型输出质量的趋势
    candidates: list[tuple[str, str]] = [("original", text)]

    fenced = _strip_fence(text)
    if fenced is not None:
        candidates.append(("stripped_markdown_fence", fenced))

    # 对原文与去围栏后的文本都做一次对象扫描，找出被前言/后记包裹的 JSON
    for label, source in (("original", text), ("stripped_markdown_fence", fenced or "")):
        if not source:
            continue
        for scanned in _scan_json_objects(source):
            tag = "scanned_object" if label == "original" else f"{label}+scanned_object"
            candidates.append((tag, scanned))

    # 对已有候选统一做两级再加工。分成两轮而不是一轮，
    # 是为了让"去尾逗号"和"补括号"能叠加生效（两种问题经常同时出现）。
    for label, candidate in list(candidates):
        cleaned = _remove_trailing_commas(candidate)
        if cleaned != candidate:
            candidates.append((f"{label}+removed_trailing_commas", cleaned))

    for label, candidate in list(candidates):
        closed = _close_unbalanced(candidate)
        if closed is not None and closed != candidate:
            candidates.append((f"{label}+closed_brackets", closed))

    for label, candidate in candidates:
        parsed = _try_loads(candidate)
        if parsed is not None:
            return parsed, [label]

    raise LLMInvalidJSONError(
        "模型返回的内容无法解析为 JSON",
        details={
            "raw_preview": text[:500],
            "raw_length": len(text),
            "attempted": [label for label, _ in candidates],
        },
    )
