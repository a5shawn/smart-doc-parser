"""大模型调用的重试策略。

**为什么不直接用 SDK 自带的重试**：``AsyncOpenAI`` 默认 ``max_retries=2``，
如果外面再套一层 3 次重试，最坏情况会打 9 次请求，单个任务可能跑十分钟以上，
还会把上游的限流雪上加霜。因此客户端构造时设 ``max_retries=0``，
重试策略统一收在这里，只有一处可调。

本模块**不导入任何供应商 SDK**：哪些异常可重试由调用方通过 ``is_retryable``
传进来，这样换供应商时不用动这里，测试也不需要构造真实 SDK 异常。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

from app.core.logging import get_logger

logger = get_logger(__name__)


def default_on_retry(operation_name: str) -> Callable[[RetryCallState], None]:
    """默认的重试日志钩子。把每次重试记进日志，否则线上只看得到最终失败。"""

    def _log(state: RetryCallState) -> None:
        exception = state.outcome.exception() if state.outcome else None
        logger.warning(
            "llm_call_retrying",
            operation=operation_name,
            attempt=state.attempt_number,
            next_wait_seconds=round(state.next_action.sleep, 2),
            error_type=type(exception).__name__ if exception else None,
            error=str(exception),
        )

    return _log


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    is_retryable: Callable[[BaseException], bool],
    operation_name: str = "llm_call",
    on_retry: Callable[[RetryCallState], None] | None = None,
    max_wait_seconds: float = 20.0,
) -> T:
    """执行 ``operation``，对可重试的异常做指数退避重试。

    退避带抖动（jitter）：多个任务同时失败时，固定间隔会让它们在同一时刻
    一起重试，把刚缓过来的上游再打挂一次。
    """
    if max_attempts <= 1:
        return await operation()

    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=1, max=max_wait_seconds),
        retry=retry_if_exception(is_retryable),
        before_sleep=on_retry or default_on_retry(operation_name),
        # 重试次数耗尽后把最后一次的原始异常抛出去，
        # 这样上层能看到真正的错误类型，而不是被包成 RetryError
        reraise=True,
    )

    async for attempt in retrying:
        with attempt:
            return await operation()

    raise AssertionError("tenacity 的循环要么返回结果要么抛出异常，不应到达这里")
