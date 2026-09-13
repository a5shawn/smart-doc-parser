"""大模型客户端的抽象接口。

业务代码只依赖这里的 :class:`LLMClient` 协议，不直接依赖 openai SDK。
好处有二：

1. 测试可以注入 :class:`~app.ai.fake.FakeLLMClient`，**不打真实 API**，
   测试快且不花钱、不依赖网络；
2. 将来换模型供应商（或加一层网关）时，改动局限在一个实现类里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class StreamKind(StrEnum):
    """流式分片的类型。

    deepseek-flash 是推理模型，输出分两个阶段：
    先吐 ``reasoning_content``（思考过程），再吐 ``content``（最终答案）。
    两者要分开处理——进度条据此显示"思考中"还是"生成结果"，
    界面也可以只展示 content 而不把思考过程混进结果里。
    """

    REASONING = "reasoning"
    CONTENT = "content"


#: 流式回调。每收到一个分片调用一次，参数是分片类型与**文本增量**。
#: 实现方必须自己保证异常不外泄——回调里抛错不应该中断模型调用。
ChunkCallback = Callable[[StreamKind, str], Awaitable[None]]

#: 重新开始的回调。输出撞上 max_tokens 上限时，客户端会放大预算重跑一次，
#: 此时已推送的分片作废。实现方收到这个信号后应当清空已展示的流式内容，
#: 否则界面会把两次输出的文本拼在一起，显示出一段看起来合法、实则错乱的 JSON。
RestartCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """token 用量。

    注意 ``completion_tokens`` **已经包含** ``reasoning_tokens``——
    计费时不要重复相加，否则成本会算高一倍。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次模型调用的结果。"""

    #: 模型返回的正文（不含思考过程）
    content: str
    model: str
    usage: LLMUsage
    latency_ms: int
    #: ``stop`` 表示正常结束；``length`` 表示撞上了 max_tokens 上限
    finish_reason: str | None = None
    #: 思考过程的全文。仅用于调试与展示，**绝不能当作答案解析**
    reasoning: str = ""
    #: 因为撞上 max_tokens 而放大预算重试过几次
    truncation_retries: int = 0
    #: 实际生效的 max_tokens
    effective_max_tokens: int = 0
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def was_truncated(self) -> bool:
        return self.finish_reason == "length"


@runtime_checkable
class LLMClient(Protocol):
    """大模型客户端协议。"""

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_restart: RestartCallback | None = None,
    ) -> LLMResponse:
        """请求模型输出一个 JSON 对象。

        :param on_chunk: 流式回调。为 ``None`` 时退化为一次性返回。
        :param on_restart: 因输出被截断而重跑前触发，通知调用方丢弃已推送的分片。
        :raises LLMTimeoutError: 调用超时
        :raises LLMRateLimitError: 被限流
        :raises LLMUpstreamError: 上游返回错误
        :raises LLMOutputTruncatedError: 放大预算重试后仍被截断
        """
        ...

    async def aclose(self) -> None:
        """释放底层连接。"""
        ...
