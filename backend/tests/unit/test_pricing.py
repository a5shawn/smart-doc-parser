"""成本换算的单元测试。

成本算错不会让功能崩溃，但会让"这个功能一个月花多少钱"的答案一直是错的，
所以口径必须锁死在测试里。
"""

from __future__ import annotations

import pytest

from app.ai.base import LLMUsage
from app.ai.pricing import estimate_cost_usd, format_cost, usd_to_cny
from app.core.config import settings


class TestEstimateCost:
    def test_zero_usage_costs_nothing(self) -> None:
        assert estimate_cost_usd(LLMUsage()) == 0.0

    def test_input_only(self) -> None:
        usage = LLMUsage(prompt_tokens=1_000_000)
        assert estimate_cost_usd(usage) == pytest.approx(
            settings.LLM_PRICE_INPUT_PER_M_USD, rel=1e-6
        )

    def test_output_only(self) -> None:
        usage = LLMUsage(completion_tokens=1_000_000)
        assert estimate_cost_usd(usage) == pytest.approx(
            settings.LLM_PRICE_OUTPUT_PER_M_USD, rel=1e-6
        )

    def test_cached_input_is_cheaper(self) -> None:
        """前缀缓存命中部分的单价远低于未命中部分，拆开算才准确。"""
        cached = estimate_cost_usd(LLMUsage(prompt_tokens=1_000_000, cached_tokens=1_000_000))
        uncached = estimate_cost_usd(LLMUsage(prompt_tokens=1_000_000, cached_tokens=0))
        assert cached < uncached

    def test_reasoning_tokens_are_not_double_counted(self) -> None:
        """completion_tokens 已经包含 reasoning_tokens，再加一遍会让成本虚高。"""
        without_reasoning = estimate_cost_usd(LLMUsage(completion_tokens=1000, reasoning_tokens=0))
        with_reasoning = estimate_cost_usd(LLMUsage(completion_tokens=1000, reasoning_tokens=800))
        assert without_reasoning == with_reasoning

    def test_cached_tokens_cannot_exceed_prompt_tokens(self) -> None:
        """数据异常时（缓存数大于总数）不能算出负成本。"""
        usage = LLMUsage(prompt_tokens=100, cached_tokens=999)
        assert estimate_cost_usd(usage) >= 0

    def test_realistic_single_document_cost(self) -> None:
        """一份合同的典型用量：约 2000 输入 + 500 输出，成本应是几厘钱量级。"""
        usage = LLMUsage(prompt_tokens=2000, completion_tokens=500, reasoning_tokens=300)
        cost = estimate_cost_usd(usage)
        assert 0 < cost < 0.01


class TestFormatting:
    def test_usd_to_cny_uses_configured_rate(self) -> None:
        assert usd_to_cny(1.0) == round(settings.USD_TO_CNY, 6)

    def test_tiny_amounts_keep_precision(self) -> None:
        """单份文档的成本极小，两位小数会显示成 ¥0.00，失去意义。"""
        assert format_cost(0.0001) != "¥0.00"
        assert format_cost(0.0001).startswith("¥")

    def test_larger_amounts_use_two_decimals(self) -> None:
        assert format_cost(1.0) == f"¥{settings.USD_TO_CNY:.2f}"

    def test_zero_is_representable(self) -> None:
        assert format_cost(0.0).startswith("¥")
