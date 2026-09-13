"""DeepSeek 客户端。

这个文件集中处理了实测出来的四个 API 行为，每一条都对应一类线上疑难问题：

1. **推理模型的截断陷阱**：``deepseek-flash`` 会先输出思考内容再输出答案，
   两者**共享 max_tokens 预算**。预算给小了的表现是"调用成功但 content 为空"，
   日志里没有任何异常——这是最难排查的一类故障。因此这里显式检查
   ``finish_reason == "length"`` 并放大预算重试一次。

2. **JSON mode 要求 prompt 含 "json"**：否则直接 400。已在 prompt 构造层拦截，
   这里再断言一次作为兜底。

3. **不支持 json_schema 严格模式**：实测返回
   ``This response_format type is unavailable now``。因此结构约束只能靠
   prompt 描述 + 服务端校验，不能指望模型端。

4. **``reasoning_content`` 是非标准字段**：必须用 ``getattr`` 取，
   直接下标访问会 KeyError。而且流式时思考阶段的 ``delta.content`` 是 ``None``
   而不是空串，遍历时不能假定它有值。
"""

from __future__ import annotations

import time
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from app.ai.base import (
    ChunkCallback,
    LLMResponse,
    LLMUsage,
    RestartCallback,
    StreamKind,
)
from app.ai.prompts import assert_json_keyword
from app.ai.retry import with_retry
from app.core.config import settings
from app.core.errors import (
    LLMOutputTruncatedError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: max_tokens 的硬上限。翻倍重试不会超过它，避免一次调用把预算撑到不可控。
MAX_TOKENS_HARD_CAP = 32_768

#: 一次网络请求内部的重试次数由这里统一控制。
#: ``AsyncOpenAI`` 自身的重试必须关掉（``max_retries=0``），否则两层重试相乘，
#: 最坏情况会打出 9 次请求——既拖长任务时间，也会把上游限流雪上加霜。
_RETRYABLE_EXCEPTIONS = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    # 5xx 之外的 APIStatusError（如 400 参数错误）重试没有意义，只会重复失败
    return isinstance(exc, APIStatusError) and exc.status_code >= 500


def _map_exception(exc: Exception) -> Exception:
    """把 SDK 异常翻译成领域异常，让上层不必依赖 openai 的类型。"""
    if isinstance(exc, APITimeoutError):
        return LLMTimeoutError(
            f"调用大模型超时（{settings.LLM_TIMEOUT_SECONDS} 秒）。"
            "文档较长或上游繁忙时可以适当调大 LLM_TIMEOUT_SECONDS。",
            details={"timeout_seconds": settings.LLM_TIMEOUT_SECONDS},
        )
    if isinstance(exc, RateLimitError):
        return LLMRateLimitError(
            "大模型接口限流，请稍后重试",
            details={"upstream_message": str(exc)[:300]},
        )
    if isinstance(exc, APIStatusError):
        return LLMUpstreamError(
            f"大模型服务返回错误（HTTP {exc.status_code}）",
            details={
                "status_code": exc.status_code,
                "upstream_message": str(exc)[:300],
            },
        )
    if isinstance(exc, APIConnectionError):
        return LLMUpstreamError(
            "无法连接到大模型服务，请检查网络与 DEEPSEEK_BASE_URL 配置",
            details={"base_url": settings.DEEPSEEK_BASE_URL},
        )
    return LLMUpstreamError(
        f"调用大模型时发生未预期的错误：{type(exc).__name__}",
        details={"reason": str(exc)[:300]},
    )


def _extract_reasoning(message: Any) -> str:
    """取思考内容。

    ``reasoning_content`` 不是 OpenAI 标准字段，用 ``getattr`` 兜底；
    某些 SDK 版本会把它放进 ``model_extra``，因此再查一层。
    """
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        return str(reasoning)

    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        return str(extra.get("reasoning_content") or "")
    return ""


def _extract_usage(usage: Any) -> LLMUsage:
    """解析用量。字段缺失一律按 0 处理——统计信息不该让一次成功的调用失败。"""
    if usage is None:
        return LLMUsage()

    reasoning_tokens = 0
    completion_details = getattr(usage, "completion_tokens_details", None)
    if completion_details is not None:
        reasoning_tokens = getattr(completion_details, "reasoning_tokens", 0) or 0

    # 优先用 DeepSeek 特有的字段；缺失时退回 OpenAI 标准结构
    cached_tokens = getattr(usage, "prompt_cache_hit_tokens", None)
    if cached_tokens is None:
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = getattr(prompt_details, "cached_tokens", 0) if prompt_details else 0

    return LLMUsage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens or 0,
    )


class DeepSeekClient:
    """基于 OpenAI 兼容接口的 DeepSeek 客户端。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
        default_max_tokens: int | None = None,
        temperature: float | None = None,
        streaming: bool | None = None,
    ) -> None:
        self._api_key = api_key or settings.DEEPSEEK_API_KEY
        self._model = model or settings.DEEPSEEK_MODEL
        self._max_retries = max_retries if max_retries is not None else settings.LLM_MAX_RETRIES
        self._default_max_tokens = default_max_tokens or settings.LLM_MAX_TOKENS
        self._temperature = temperature if temperature is not None else settings.LLM_TEMPERATURE
        self._streaming = streaming if streaming is not None else settings.LLM_STREAMING

        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=base_url or settings.DEEPSEEK_BASE_URL,
            timeout=timeout_seconds or settings.LLM_TIMEOUT_SECONDS,
            # 关键：关掉 SDK 自带重试，重试策略统一由 app.ai.retry 管理
            max_retries=0,
        )

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_restart: RestartCallback | None = None,
    ) -> LLMResponse:
        # JSON mode 的硬性要求，在发请求前就失败，省一次网络往返
        assert_json_keyword(system_prompt, user_prompt)

        budget = max_tokens or self._default_max_tokens
        use_stream = self._streaming and on_chunk is not None
        truncation_retries = 0

        while True:
            response = await self._request_with_retry(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=budget,
                on_chunk=on_chunk if use_stream else None,
            )

            if not response.was_truncated:
                logger.info(
                    "llm_call_succeeded",
                    model=response.model,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    reasoning_tokens=response.usage.reasoning_tokens,
                    cached_tokens=response.usage.cached_tokens,
                    latency_ms=response.latency_ms,
                    max_tokens=budget,
                    streaming=use_stream,
                )
                return LLMResponse(
                    content=response.content,
                    model=response.model,
                    usage=response.usage,
                    latency_ms=response.latency_ms,
                    finish_reason=response.finish_reason,
                    reasoning=response.reasoning,
                    truncation_retries=truncation_retries,
                    effective_max_tokens=budget,
                )

            # 撞上 max_tokens：deepseek-flash 的思考 token 与正文共享这个预算，
            # 文档复杂时思考会吃掉大部分额度，表现为正文被截断甚至为空。
            new_budget = min(budget * 2, MAX_TOKENS_HARD_CAP)
            if truncation_retries >= 1 or new_budget <= budget:
                raise LLMOutputTruncatedError(
                    f"大模型输出被 max_tokens（{budget}）截断，即使放大到 {budget} 仍然如此。"
                    "请调大环境变量 LLM_MAX_TOKENS。",
                    details={
                        "max_tokens": budget,
                        "reasoning_tokens": response.usage.reasoning_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "content_preview": response.content[:300],
                    },
                )

            truncation_retries += 1
            logger.warning(
                "llm_output_truncated_retrying",
                model=self._model,
                previous_max_tokens=budget,
                new_max_tokens=new_budget,
                reasoning_tokens=response.usage.reasoning_tokens,
                content_length=len(response.content),
            )
            budget = new_budget

            # 通知调用方丢弃已推送的分片，否则界面会把两次输出拼在一起
            if on_restart is not None:
                await self._safe_callback(on_restart)

    # ------------------------------------------------------------------
    # 实际请求
    # ------------------------------------------------------------------
    async def _request_with_retry(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        on_chunk: ChunkCallback | None,
    ) -> LLMResponse:
        try:
            return await with_retry(
                lambda: self._do_request(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=max_tokens,
                    on_chunk=on_chunk,
                ),
                max_attempts=self._max_retries + 1,
                is_retryable=_is_retryable,
                operation_name="deepseek_complete_json",
            )
        except _RETRYABLE_EXCEPTIONS as exc:
            # 重试次数耗尽，最后一次的原始异常走到这里
            raise _map_exception(exc) from exc
        except APIStatusError as exc:
            raise _map_exception(exc) from exc

    def _build_request(
        self, *, system_prompt: str, user_prompt: str, max_tokens: int
    ) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # 只支持 json_object，不支持 json_schema（实测报 "unavailable now"）。
            # 结构正确性由 app.ai.schema_utils 的服务端校验兜底。
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "temperature": self._temperature,
        }

    async def _do_request(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        on_chunk: ChunkCallback | None,
    ) -> LLMResponse:
        request = self._build_request(
            system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=max_tokens
        )
        started = time.perf_counter()

        if on_chunk is None:
            completion = await self._client.chat.completions.create(**request, stream=False)
            latency_ms = int((time.perf_counter() - started) * 1000)
            choice = completion.choices[0]
            return LLMResponse(
                content=choice.message.content or "",
                reasoning=_extract_reasoning(choice.message),
                model=completion.model or self._model,
                usage=_extract_usage(completion.usage),
                latency_ms=latency_ms,
                finish_reason=choice.finish_reason,
                effective_max_tokens=max_tokens,
            )

        return await self._do_streaming_request(request, on_chunk=on_chunk, started=started)

    async def _do_streaming_request(
        self,
        request: dict[str, Any],
        *,
        on_chunk: ChunkCallback,
        started: float,
    ) -> LLMResponse:
        """流式请求：边收边回调。

        ``include_usage=True`` 让最后一个分片带上完整的 usage——
        没有它就只能靠猜 token 数，成本统计会失真。
        """
        stream = await self._client.chat.completions.create(
            **request, stream=True, stream_options={"include_usage": True}
        )

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: LLMUsage = LLMUsage()
        finish_reason: str | None = None
        model = self._model

        try:
            async for chunk in stream:
                # 带 usage 的那个分片没有 choices，必须先判空再取
                if getattr(chunk, "usage", None) is not None:
                    usage = _extract_usage(chunk.usage)
                if getattr(chunk, "model", None):
                    model = chunk.model

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

                delta = choice.delta

                reasoning_delta = _extract_reasoning(delta)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    await self._safe_chunk(on_chunk, StreamKind.REASONING, reasoning_delta)

                # 思考阶段 delta.content 是 None 而不是空串，必须判空
                if delta.content:
                    content_parts.append(delta.content)
                    await self._safe_chunk(on_chunk, StreamKind.CONTENT, delta.content)
        finally:
            # **必须显式关闭流**。只让 `async for` 自然结束不够：
            # 底层 HTTP 响应流没有被释放，之后关闭 HTTP 客户端时 httpx 会在
            # 异步生成器的清理阶段抛 "generator didn't stop after athrow()"。
            # 这个异常发生在收尾路径上，不影响抽取结果，但会污染日志、
            # 让排查真正的问题时被误导。
            await stream.close()

        latency_ms = int((time.perf_counter() - started) * 1000)

        return LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            model=model,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            effective_max_tokens=request["max_tokens"],
        )

    # ------------------------------------------------------------------
    # 回调保护
    # ------------------------------------------------------------------
    @staticmethod
    async def _safe_callback(callback: RestartCallback) -> None:
        """回调里抛错不该中断模型调用——推送进度失败是次要问题，
        让整个抽取任务失败是主要问题。"""
        try:
            await callback()
        except Exception:
            logger.warning("stream_restart_callback_failed", exc_info=True)

    @staticmethod
    async def _safe_chunk(callback: ChunkCallback, kind: StreamKind, delta: str) -> None:
        try:
            await callback(kind, delta)
        except Exception:
            logger.warning("stream_chunk_callback_failed", kind=kind.value, exc_info=True)


def create_llm_client() -> DeepSeekClient:
    """按当前配置构造客户端。"""
    return DeepSeekClient()
