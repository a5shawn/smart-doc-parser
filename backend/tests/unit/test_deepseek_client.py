"""DeepSeek 客户端的行为测试。

这里**不打真实 API**：通过替换 ``_do_request`` 精确构造各种返回，
覆盖那些用真实调用很难稳定复现的分支（截断、字段缺失等）。
"""

from __future__ import annotations

import pytest

from app.ai.base import LLMResponse, LLMUsage, StreamKind
from app.ai.deepseek_client import (
    MAX_TOKENS_HARD_CAP,
    DeepSeekClient,
    _extract_reasoning,
    _extract_usage,
)
from app.core.errors import LLMOutputTruncatedError


def _client(**kwargs) -> DeepSeekClient:
    defaults = {
        "api_key": "sk-test",
        "base_url": "https://api.deepseek.com",
        "default_max_tokens": 1000,
    }
    return DeepSeekClient(**{**defaults, **kwargs})


def _response(
    content: str = '{"a": 1}',
    *,
    finish_reason: str = "stop",
    reasoning: str = "",
    max_tokens: int = 1000,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        reasoning=reasoning,
        model="deepseek-flash",
        usage=LLMUsage(prompt_tokens=100, completion_tokens=50, reasoning_tokens=40),
        latency_ms=500,
        finish_reason=finish_reason,
        effective_max_tokens=max_tokens,
    )


class TestPromptGuard:
    async def test_rejects_prompt_without_json_keyword(self) -> None:
        """JSON mode 要求 prompt 含 json，在本地就拦下来，省一次网络往返和一次 400。"""
        client = _client()
        with pytest.raises(ValueError, match="json"):
            await client.complete_json(system_prompt="你是一个抽取助手", user_prompt="请抽取字段")

    async def test_accepts_prompt_with_keyword(self, monkeypatch) -> None:
        client = _client()

        async def fake_request(**_kwargs):
            return _response()

        monkeypatch.setattr(client, "_do_request", fake_request)
        result = await client.complete_json(system_prompt="输出 JSON", user_prompt="请处理")
        assert result.content == '{"a": 1}'


class TestTruncationHandling:
    """deepseek-flash 是推理模型，思考 token 与正文共享 max_tokens 预算。

    预算被思考吃光时 ``finish_reason`` 会是 ``length``，正文可能为空。
    这类故障在日志里没有任何异常，是最难排查的一类。
    """

    async def test_retries_with_doubled_budget(self, monkeypatch) -> None:
        client = _client(default_max_tokens=1000)
        budgets: list[int] = []

        async def fake_request(*, max_tokens, **_kwargs):
            budgets.append(max_tokens)
            if len(budgets) == 1:
                return _response(content="", finish_reason="length")
            return _response(content='{"a": 1}')

        monkeypatch.setattr(client, "_do_request", fake_request)

        result = await client.complete_json(system_prompt="输出 JSON", user_prompt="请处理")

        assert budgets == [1000, 2000]
        assert result.content == '{"a": 1}'
        assert result.truncation_retries == 1
        assert result.effective_max_tokens == 2000

    async def test_raises_when_still_truncated_after_retry(self, monkeypatch) -> None:
        client = _client(default_max_tokens=1000)
        budgets: list[int] = []

        async def fake_request(*, max_tokens, **_kwargs):
            budgets.append(max_tokens)
            return _response(content="", finish_reason="length")

        monkeypatch.setattr(client, "_do_request", fake_request)

        with pytest.raises(LLMOutputTruncatedError) as exc_info:
            await client.complete_json(system_prompt="输出 JSON", user_prompt="请处理")

        # 只放大一次，不会无限翻倍
        assert budgets == [1000, 2000]
        assert "LLM_MAX_TOKENS" in exc_info.value.message
        assert exc_info.value.details["reasoning_tokens"] == 40

    async def test_budget_never_exceeds_hard_cap(self, monkeypatch) -> None:
        client = _client(default_max_tokens=MAX_TOKENS_HARD_CAP)
        budgets: list[int] = []

        async def fake_request(*, max_tokens, **_kwargs):
            budgets.append(max_tokens)
            return _response(content="", finish_reason="length")

        monkeypatch.setattr(client, "_do_request", fake_request)

        with pytest.raises(LLMOutputTruncatedError):
            await client.complete_json(system_prompt="输出 JSON", user_prompt="请处理")

        assert all(budget <= MAX_TOKENS_HARD_CAP for budget in budgets)

    async def test_notifies_caller_to_discard_streamed_chunks(self, monkeypatch) -> None:
        """重跑前必须清空已推送的分片，否则界面会把两次输出拼在一起。"""
        client = _client(default_max_tokens=1000)
        restarts: list[bool] = []

        async def fake_request(*, max_tokens, **_kwargs):
            if max_tokens == 1000:
                return _response(content="", finish_reason="length")
            return _response(content='{"a": 1}')

        async def on_restart() -> None:
            restarts.append(True)

        monkeypatch.setattr(client, "_do_request", fake_request)
        await client.complete_json(
            system_prompt="输出 JSON", user_prompt="请处理", on_restart=on_restart
        )

        assert restarts == [True]

    async def test_normal_response_does_not_trigger_restart(self, monkeypatch) -> None:
        client = _client()
        restarts: list[bool] = []

        async def fake_request(**_kwargs):
            return _response()

        async def on_restart() -> None:
            restarts.append(True)

        monkeypatch.setattr(client, "_do_request", fake_request)
        await client.complete_json(
            system_prompt="输出 JSON", user_prompt="请处理", on_restart=on_restart
        )
        assert restarts == []

    async def test_failing_restart_callback_is_swallowed(self, monkeypatch) -> None:
        """回调抛错不该中断已经成功的重试。"""
        client = _client(default_max_tokens=1000)

        async def fake_request(*, max_tokens, **_kwargs):
            if max_tokens == 1000:
                return _response(content="", finish_reason="length")
            return _response(content='{"a": 1}')

        async def broken() -> None:
            raise RuntimeError("推送失败")

        monkeypatch.setattr(client, "_do_request", fake_request)
        result = await client.complete_json(
            system_prompt="输出 JSON", user_prompt="请处理", on_restart=broken
        )
        assert result.content == '{"a": 1}'


class TestUsageExtraction:
    def test_missing_usage_returns_zeros(self) -> None:
        """用量字段缺失不该让一次成功的调用失败。"""
        assert _extract_usage(None) == LLMUsage()

    def test_reads_deepseek_specific_cache_field(self) -> None:
        class Usage:
            prompt_tokens = 100
            completion_tokens = 50
            prompt_cache_hit_tokens = 80
            completion_tokens_details = None

        usage = _extract_usage(Usage())
        assert usage.cached_tokens == 80
        assert usage.prompt_tokens == 100

    def test_falls_back_to_openai_cache_field(self) -> None:
        """DeepSeek 字段缺失时退回 OpenAI 标准结构。"""

        class PromptDetails:
            cached_tokens = 30

        class CompletionDetails:
            reasoning_tokens = 12

        class Usage:
            prompt_tokens = 100
            completion_tokens = 50
            prompt_tokens_details = PromptDetails()
            completion_tokens_details = CompletionDetails()

        usage = _extract_usage(Usage())
        assert usage.cached_tokens == 30
        assert usage.reasoning_tokens == 12

    def test_none_valued_fields_become_zero(self) -> None:
        class Usage:
            prompt_tokens = None
            completion_tokens = None
            prompt_cache_hit_tokens = None
            completion_tokens_details = None

        usage = _extract_usage(Usage())
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0


class TestReasoningExtraction:
    def test_reads_non_standard_field(self) -> None:
        """reasoning_content 不是 OpenAI 标准字段，必须用 getattr 兜底。"""

        class Message:
            reasoning_content = "先分析文档结构"

        assert _extract_reasoning(Message()) == "先分析文档结构"

    def test_missing_field_does_not_raise(self) -> None:
        class Message:
            content = "{}"

        assert _extract_reasoning(Message()) == ""

    def test_reads_from_model_extra(self) -> None:
        """某些 SDK 版本会把非标准字段塞进 model_extra。"""

        class Message:
            pass

        message = Message()
        message.model_extra = {"reasoning_content": "思考内容"}  # type: ignore[attr-defined]

        assert _extract_reasoning(message) == "思考内容"

    def test_none_reasoning_returns_empty(self) -> None:
        class Message:
            reasoning_content = None
            model_extra = None

        assert _extract_reasoning(Message()) == ""


class TestStreamKind:
    def test_kinds_are_stable_strings(self) -> None:
        """这两个值会出现在 SSE 事件里，改动会破坏前端兼容性。"""
        assert StreamKind.REASONING == "reasoning"
        assert StreamKind.CONTENT == "content"
