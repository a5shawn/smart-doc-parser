"""token 用量换算成成本。

**"我知道每份文档花多少钱"是生产系统与 demo 的分水岭。**
把成本算出来、记进数据库、显示在界面上，才能回答
"这个功能一个月要花多少钱""调大 max_tokens 值不值"这类真正的问题。

单价放在配置里而不是硬编码：官方价格会调整，改环境变量即可跟进，不用改代码。
"""

from __future__ import annotations

from app.ai.base import LLMUsage
from app.core.config import settings

#: 一百万
_MILLION = 1_000_000


def estimate_cost_usd(usage: LLMUsage) -> float:
    """按用量估算成本（美元）。

    计费口径（与官方一致）：

    - ``prompt_tokens`` 是输入总量，其中命中前缀缓存的部分（``cached_tokens``）
      单价更低，因此要拆开算；
    - ``completion_tokens`` **已经包含** ``reasoning_tokens``，直接按输出价算即可，
      把思考 token 再加一遍会导致成本虚高一倍。

    :returns: 美元金额。用量为 0 时返回 0.0。
    """
    cached = min(usage.cached_tokens, usage.prompt_tokens)
    uncached = max(usage.prompt_tokens - cached, 0)

    cost = (
        uncached * settings.LLM_PRICE_INPUT_PER_M_USD
        + cached * settings.LLM_PRICE_CACHED_INPUT_PER_M_USD
        + usage.completion_tokens * settings.LLM_PRICE_OUTPUT_PER_M_USD
    ) / _MILLION

    return round(cost, 8)


def usd_to_cny(amount_usd: float) -> float:
    return round(amount_usd * settings.USD_TO_CNY, 6)


def format_cost(amount_usd: float) -> str:
    """人类可读的成本字符串。金额很小时用更多小数位，避免显示成 ¥0.00。"""
    amount_cny = usd_to_cny(amount_usd)
    if amount_cny < 0.01:
        return f"¥{amount_cny:.4f}"
    return f"¥{amount_cny:.2f}"
