"""抽取编排：prompt → 调用 → 解析 → 矫正 → 校验。

这是「一次抽取」的完整业务逻辑，与大模型供应商无关（只依赖
:class:`~app.ai.base.LLMClient` 协议），也与 HTTP / 数据库无关，
因此可以脱离服务单独测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.base import ChunkCallback, LLMClient, LLMUsage, RestartCallback
from app.ai.json_repair import parse_json_lenient
from app.ai.prompts import build_extraction_prompt, build_repair_prompt
from app.ai.schema_utils import (
    coerce_data,
    dedupe_issues,
    normalize_schema,
    validate_data,
)
from app.ai.templates import ExtractionTemplate
from app.core.errors import LLMInvalidJSONError
from app.core.logging import get_logger
from app.services.parsing.normalize import truncate_for_llm

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """一次抽取的完整产出。"""

    data: dict[str, Any]
    #: 模型返回的原始文本，原样保留
    raw_output: str
    #: 非致命问题（字段缺失、类型被矫正、文档被截断等）
    warnings: list[dict[str, str]] = field(default_factory=list)
    #: 输入文档是否因为超长被截断
    truncated: bool = False
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    model: str = ""
    #: 实际调用大模型的次数（含因 JSON 解析失败而重试的那次）
    attempts: int = 1
    #: 应用过的 JSON 修复动作，便于观察模型输出质量的长期趋势
    repairs: list[str] = field(default_factory=list)


class Extractor:
    """把一份文档文本抽取成结构化数据。"""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def extract(
        self,
        *,
        template: ExtractionTemplate,
        document_text: str,
        max_input_chars: int,
        on_chunk: ChunkCallback | None = None,
        on_restart: RestartCallback | None = None,
    ) -> ExtractionOutcome:
        """执行抽取。

        :raises LLMInvalidJSONError: 两次调用返回的内容都无法解析为 JSON
        :raises LLMError: 模型调用失败（超时、限流等）
        """
        # ---- 1. 超长文档截断 ----
        text, truncated = truncate_for_llm(document_text, max_input_chars)

        # ---- 2. 构造 prompt ----
        prompt = build_extraction_prompt(template=template, document_text=text, truncated=truncated)

        # ---- 3. 调用模型 ----
        response = await self._client.complete_json(
            system_prompt=prompt.system,
            user_prompt=prompt.user,
            on_chunk=on_chunk,
            on_restart=on_restart,
        )
        attempts = 1

        # ---- 4. 解析 JSON，失败则纠正后重试一次 ----
        try:
            parsed, repairs = parse_json_lenient(response.content)
        except LLMInvalidJSONError as first_error:
            logger.warning(
                "llm_invalid_json_retrying",
                model=response.model,
                raw_length=len(response.content),
                reason=first_error.message,
            )
            attempts = 2
            response = await self._client.complete_json(
                system_prompt=prompt.system,
                user_prompt=build_repair_prompt(
                    previous_output=response.content,
                    error_hint=first_error.message,
                ),
                on_chunk=on_chunk,
                on_restart=on_restart,
            )
            parsed, repairs = parse_json_lenient(response.content)

        if not isinstance(parsed, dict):
            raise LLMInvalidJSONError(
                "模型返回的 JSON 顶层不是对象，无法作为字段集合使用",
                details={"actual_type": type(parsed).__name__},
            )

        # ---- 5. 类型矫正 + 结构校验 ----
        schema = normalize_schema(template.schema)
        coerced, coercion_issues = coerce_data(parsed, schema)
        validation_issues = validate_data(coerced, schema)

        issues = dedupe_issues([*coercion_issues, *validation_issues])
        warnings = [issue.to_dict() for issue in issues]

        if truncated:
            warnings.insert(
                0,
                {
                    "path": "$",
                    "kind": "input_truncated",
                    "message": f"文档超过 {max_input_chars} 字上限，中间部分内容已省略，"
                    "部分字段可能因此未被抽到",
                },
            )

        if warnings:
            logger.info(
                "extraction_completed_with_warnings",
                warning_count=len(warnings),
                kinds=sorted({item["kind"] for item in warnings}),
            )

        return ExtractionOutcome(
            data=coerced if isinstance(coerced, dict) else parsed,
            raw_output=response.content,
            warnings=warnings,
            truncated=truncated,
            usage=response.usage,
            latency_ms=response.latency_ms,
            model=response.model,
            attempts=attempts,
            repairs=repairs,
        )
