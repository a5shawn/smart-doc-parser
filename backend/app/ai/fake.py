"""测试用的假大模型客户端。

测试**绝不打真实 API**：既慢又花钱，还会因为上游抖动造成随机失败，
让人分不清是代码坏了还是网络抖了。

用它来构造各种"模型不听话"的场景——返回带围栏的 JSON、截断的 JSON、
空内容、超时异常——这些才是真正需要覆盖的分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.base import (
    ChunkCallback,
    LLMResponse,
    LLMUsage,
    RestartCallback,
    StreamKind,
)


@dataclass
class ScriptedResponse:
    """一次预设的模型返回。"""

    content: str = "{}"
    reasoning: str = ""
    finish_reason: str = "stop"
    model: str = "fake-model"
    usage: LLMUsage = field(
        default_factory=lambda: LLMUsage(
            prompt_tokens=100, completion_tokens=50, reasoning_tokens=20
        )
    )
    latency_ms: int = 123
    #: 非 None 时这次调用直接抛出该异常，用于测试重试与错误映射
    error: Exception | None = None


class FakeLLMClient:
    """按脚本返回结果的假客户端。

    ``script`` 里的条目按顺序消费；用完后回落到 ``default_response``。
    调用记录保存在 :attr:`calls` 里，测试可以断言"共调用了几次""第二次用的
    是哪个 prompt"。
    """

    def __init__(
        self,
        script: list[ScriptedResponse] | None = None,
        *,
        default_response: ScriptedResponse | None = None,
    ) -> None:
        self._script = list(script or [])
        self._default = default_response or ScriptedResponse()
        self.calls: list[dict[str, object]] = []
        self.closed = False

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def last_prompt(self) -> str:
        assert self.calls, "还没有任何调用"
        return str(self.calls[-1]["user_prompt"])

    def last_system_prompt(self) -> str:
        assert self.calls, "还没有任何调用"
        return str(self.calls[-1]["system_prompt"])

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        # 签名必须与协议一致，但假客户端不做截断处理（那是真实客户端的职责），
        # 因此这里刻意不使用它。参数名不能改，否则按关键字调用会失败。
        on_restart: RestartCallback | None = None,  # noqa: ARG002
    ) -> LLMResponse:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
            }
        )

        scripted = self._script.pop(0) if self._script else self._default
        if scripted.error is not None:
            raise scripted.error

        # 模拟流式：思考内容先推，正文后推——与真实推理模型的分片顺序一致
        if on_chunk is not None:
            for index in range(0, len(scripted.reasoning), 8):
                await on_chunk(StreamKind.REASONING, scripted.reasoning[index : index + 8])
            for index in range(0, len(scripted.content), 8):
                await on_chunk(StreamKind.CONTENT, scripted.content[index : index + 8])

        return LLMResponse(
            content=scripted.content,
            reasoning=scripted.reasoning,
            model=scripted.model,
            usage=scripted.usage,
            latency_ms=scripted.latency_ms,
            finish_reason=scripted.finish_reason,
            effective_max_tokens=max_tokens or 0,
        )

    async def aclose(self) -> None:
        self.closed = True
