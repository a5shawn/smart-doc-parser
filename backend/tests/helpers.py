"""测试用的公共辅助函数。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import UUID

from httpx import AsyncClient

TERMINAL_STATUSES = frozenset({"completed", "failed"})


async def wait_for_task(
    client: AsyncClient, task_id: UUID | str, *, wait_seconds: float = 10.0
) -> dict[str, Any]:
    """轮询直到任务进入终态，返回任务详情。

    后台任务是真正的 asyncio 任务，测试无法直接 await 它，
    因此用轮询等待——比 sleep 固定时长要快得多，也不会在慢机器上偶发失败。

    参数名刻意不叫 ``timeout``：ruff 的 ASYNC109 会把带 ``timeout`` 参数的
    异步函数标出来（它认为超时该由调用方用 ``asyncio.timeout()`` 管），
    与其压制规则不如换个同样清楚的名字。
    """
    deadline = time.monotonic() + wait_seconds
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/tasks/{task_id}")
        last = response.json()
        if last.get("status") in TERMINAL_STATUSES:
            return last
        await asyncio.sleep(0.05)

    raise AssertionError(f"任务 {task_id} 未在 {wait_seconds}s 内进入终态，最后状态：{last}")


async def upload(client: AsyncClient, filename: str, data: bytes) -> dict[str, Any]:
    """上传一个文件并返回它对应的文档对象。"""
    response = await client.post(
        "/api/v1/documents", files=[("files", (filename, data, "application/octet-stream"))]
    )
    body = response.json()
    assert not body["rejected"], f"上传被拒绝：{body['rejected']}"
    return (body["accepted"] or body["duplicate"])[0]


async def create_task(
    client: AsyncClient, document_id: str, template_key: str = "contract"
) -> dict[str, Any]:
    """为一个文档创建抽取任务，返回任务对象。"""
    response = await client.post(
        "/api/v1/tasks", json={"document_ids": [document_id], "template_key": template_key}
    )
    assert response.status_code == 201, response.text
    return response.json()["tasks"][0]


def parse_sse(raw: str) -> tuple[str | None, dict[str, Any]]:
    """把一段 SSE 报文解析成 ``(事件名, 数据)``。"""
    event: str | None = None
    data: dict[str, Any] = {}

    for line in raw.splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))

    return event, data


class FakeRequest:
    """SSE 生成器需要的最小请求对象。

    为什么要伪造而不是用真实的 HTTP 请求：**httpx 的 ASGITransport 会把整个响应体
    缓冲完才返回**（``ASGIResponseStream`` 里是 ``yield b"".join(self._body)``），
    而 SSE 是永不结束的流，走 HTTP 客户端会直接挂死。
    直接驱动异步生成器既能测到全部逻辑，又是确定性的、毫秒级的。
    """

    def __init__(self, *, disconnect_after: int | None = None) -> None:
        self.calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self._disconnect_after is not None and self.calls > self._disconnect_after


async def read_sse_raw(generator, *, limit: int, wait_seconds: float = 5.0) -> list[str]:
    """读取原始 SSE 帧。

    心跳是注释行，会被 :func:`read_sse_events` 跳过，
    验证心跳必须看原始内容。
    """
    frames: list[str] = []
    try:
        while len(frames) < limit:
            frames.append(await asyncio.wait_for(generator.__anext__(), timeout=wait_seconds))
    except (StopAsyncIteration, TimeoutError):
        pass
    finally:
        await generator.aclose()
    return frames


async def read_sse_events(
    generator, *, limit: int, wait_seconds: float = 5.0
) -> list[tuple[str | None, dict[str, Any]]]:
    """从 SSE 异步生成器里读事件。

    读完 ``limit`` 个事件后主动 ``aclose()``，让生成器的 ``finally`` 分支
    （注销订阅者）得到执行——和真实客户端断开连接的效果一致。
    """
    frames = await read_sse_raw(generator, limit=limit, wait_seconds=wait_seconds)
    return [parse_sse(frame) for frame in frames]
