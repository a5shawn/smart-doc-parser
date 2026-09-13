"""任务进度推送（SSE）。

架构：**一条全局流**，而不是每个任务一条
--------------------------------------------
批量上传 20 份文档，如果每个任务开一条 SSE，会直接撞上浏览器
「同源并发连接数约 6 个」的限制（HTTP/1.1），整页连普通请求都发不出去。
全局单流在事件里带上 ``task_id``，由前端分发，从架构上规避了这个问题。

两条通道各司其职
----------------
- **状态与进度**：服务端轮询数据库后推送。跨 worker、跨重连、跨进程重启都正确，
  代价是最多一个轮询周期的延迟。
- **模型逐字输出**：走进程内广播（:data:`app.services.progress.chunk_bus`）。
  数据量太大不适合落库，因此是**尽力而为**的通道。

:warning: **这个路由必须注册在 ``/tasks/{task_id}`` 之前**，
否则 ``stream`` 会被当成 UUID 解析，直接返回 422。见 ``router.py`` 里的顺序说明。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select

from app.ai.base import StreamKind
from app.core.config import settings
from app.core.logging import get_logger
from app.db.enums import TaskStatus
from app.db.models import Document, ExtractionTask
from app.db.session import session_scope
from app.schemas.task import TaskOut
from app.services.progress import (
    EVENT_CHUNK,
    EVENT_DONE,
    EVENT_PROGRESS,
    EVENT_SNAPSHOT,
    chunk_bus,
    format_heartbeat,
    format_sse,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/tasks", tags=["任务"])

#: 全局流里最多跟踪多少个任务，防止一次性把整张表拉进内存
_MAX_TRACKED_TASKS = 200

#: 已完成的任务在多长时间内仍然出现在全局流里。
#: 太短的话前端可能错过完成事件（进度条停在 95% 不动），太长则每连接都要多查一堆。
_RECENTLY_FINISHED_WINDOW = timedelta(minutes=5)

#: 等待分片时的最大阻塞时间。同时保证"有分片就立即转发、没有就按节奏轮询"。
_MAX_CHUNK_WAIT_SECONDS = 0.5


def _fingerprint(task: ExtractionTask) -> tuple[str, int, str | None]:
    """用于判断任务状态是否发生变化。"""
    return (task.status.value, task.progress, task.error_code)


async def _load_tracked_tasks(
    *, task_ids: list[UUID] | None, batch_id: UUID | None
) -> list[tuple[ExtractionTask, str]]:
    """取出本次连接需要跟踪的任务。"""
    query = (
        select(ExtractionTask, Document.filename)
        .join(Document, Document.id == ExtractionTask.document_id)
        .order_by(ExtractionTask.created_at.desc())
        .limit(_MAX_TRACKED_TASKS)
    )

    if task_ids:
        query = query.where(ExtractionTask.id.in_(task_ids))
    elif batch_id is not None:
        query = query.where(ExtractionTask.batch_id == batch_id)
    else:
        # 全局流：进行中的全部 + 刚结束的一小段时间内的。
        # 只跟踪"刚结束"的是为了让前端能收到完成事件，同时避免把历史任务全查出来。
        cutoff = datetime.now(UTC) - _RECENTLY_FINISHED_WINDOW
        query = query.where(
            or_(
                ExtractionTask.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                ExtractionTask.finished_at >= cutoff,
            )
        )

    async with session_scope() as db:
        rows = (await db.execute(query)).all()

    return [(row[0], row[1]) for row in rows]


async def _event_stream(
    request: Request,
    *,
    task_ids: list[UUID] | None,
    batch_id: UUID | None,
) -> AsyncIterator[str]:
    """SSE 事件生成器。

    每轮循环做两件事，用一个循环同时服务两条通道：

    1. 到点了就轮询数据库，把变化过的任务作为 ``progress`` 推出去
    2. 有模型分片就立即转发

    ``asyncio.wait_for`` 的超时时间取"距下次轮询还剩多久"，
    因此分片活跃时会被立刻转发，空闲时不会空转。
    """
    #: 显式指定任务时，全部进入终态后主动收尾并关闭连接。
    #: 全局流与批次流不自动关闭——前端可能还要继续看到新任务。
    close_when_done = bool(task_ids)

    queue = chunk_bus.subscribe()
    seen: dict[UUID, tuple[str, int, str | None]] = {}
    last_heartbeat = time.monotonic()
    next_poll_at = 0.0

    try:
        # ---- 首次快照：晚订阅的客户端也能立刻看到当前状态，而不是从 0 开始 ----
        tracked = await _load_tracked_tasks(task_ids=task_ids, batch_id=batch_id)
        for task, _filename in tracked:
            seen[task.id] = _fingerprint(task)
        yield format_sse(
            EVENT_SNAPSHOT,
            {
                "tasks": [
                    TaskOut.from_row(task, filename).model_dump(mode="json")
                    for task, filename in tracked
                ]
            },
        )

        while True:
            if await request.is_disconnected():
                break

            now = time.monotonic()

            # ---- 通道一：数据库轮询 ----
            if now >= next_poll_at:
                next_poll_at = now + settings.SSE_POLL_INTERVAL_SECONDS

                tracked = await _load_tracked_tasks(task_ids=task_ids, batch_id=batch_id)

                for task, filename in tracked:
                    fingerprint = _fingerprint(task)
                    if seen.get(task.id) == fingerprint:
                        continue
                    seen[task.id] = fingerprint

                    payload = TaskOut.from_row(task, filename).model_dump(mode="json")
                    yield format_sse(EVENT_PROGRESS, payload)

                    if task.status.is_terminal:
                        yield format_sse(EVENT_DONE, payload)

                if (
                    close_when_done
                    and tracked
                    and all(task.status.is_terminal for task, _ in tracked)
                ):
                    break

                if now - last_heartbeat >= settings.SSE_HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    yield format_heartbeat()
                continue

            # ---- 通道二：模型流式分片 ----
            remaining = max(0.05, min(next_poll_at - time.monotonic(), _MAX_CHUNK_WAIT_SECONDS))
            try:
                task_id, kind, text = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                # 没有分片，回到循环顶部去轮询数据库
                continue
            except asyncio.CancelledError:
                raise

            # 指定了跟踪范围时，服务端就把无关任务的分片丢掉，
            # 而不是让每个客户端都收全量再自己过滤——批量跑 20 个任务时，
            # 不过滤意味着每个连接都要传输 20 倍的无关内容。
            if (task_ids or batch_id is not None) and task_id not in seen:
                continue

            yield format_sse(
                EVENT_CHUNK,
                {
                    "task_id": str(task_id),
                    "kind": kind.value if isinstance(kind, StreamKind) else str(kind),
                    "text": text,
                },
            )

    except asyncio.CancelledError:
        # 客户端断开时 Starlette 会取消这个生成器。**必须重新抛出**，
        # 否则连接不会被真正释放，每次刷新页面都会泄漏一个协程。
        logger.debug("sse_client_cancelled")
        raise
    except Exception:
        logger.error("sse_stream_failed", exc_info=True)
        raise
    finally:
        chunk_bus.unsubscribe(queue)
        logger.debug("sse_connection_closed", tracked=len(seen))


@router.get(
    "/stream",
    summary="任务进度流（SSE）",
    description=(
        "服务端推送任务状态、进度与模型的流式输出。\n\n"
        "**全局单流**：不传参数时跟踪所有进行中的任务，事件里带 `task_id` 由前端分发。\n"
        "传 `batch_id` 只看某一批；传 `task_ids` 只看指定任务，且全部结束后连接会自动关闭。\n\n"
        "事件类型：`snapshot`（连接时的全量快照）/ `progress` / `chunk`（模型输出分片）"
        "/ `done`（进入终态）。以 `:` 开头的行是心跳注释。"
    ),
    response_class=StreamingResponse,
)
async def stream_tasks(
    request: Request,
    task_ids: str | None = Query(default=None, description="逗号分隔的任务 ID，只看这些任务"),
    batch_id: UUID | None = Query(default=None, description="只看某个批次"),
) -> StreamingResponse:
    parsed_ids: list[UUID] | None = None
    if task_ids:
        try:
            parsed_ids = [UUID(item.strip()) for item in task_ids.split(",") if item.strip()]
        except ValueError:
            parsed_ids = None

    return StreamingResponse(
        _event_stream(request, task_ids=parsed_ids, batch_id=batch_id),
        media_type="text/event-stream",
        headers={
            # 禁止任何中间层缓存或转换这个响应
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 与 nginx 的 proxy_buffering off 是双重保险：
            # 任一侧生效，事件都能逐条吐出而不是攒成一坨
            "X-Accel-Buffering": "no",
        },
    )
