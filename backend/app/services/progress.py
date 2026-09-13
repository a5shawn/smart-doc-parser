"""进度推送：SSE 事件格式与写库节流。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.ai.base import StreamKind

#: SSE 事件类型。前端按这几种分别处理。
EVENT_SNAPSHOT = "snapshot"  # 连接建立时的全量快照
EVENT_PROGRESS = "progress"  # 状态或进度变化
EVENT_CHUNK = "chunk"  # 模型流式输出的分片
EVENT_DONE = "done"  # 任务进入终态
EVENT_ERROR = "error"  # 推送层面出错
EVENT_PING = "ping"  # 心跳


def format_sse(event: str, data: dict[str, Any], *, event_id: str | None = None) -> str:
    """把事件序列化成 SSE 报文。

    ``json.dumps`` 会把内容里的换行转义成 ``\\n``，因此不必担心一条 data
    被拆成多行——这是选用 JSON 而不是裸文本作为载荷的主要原因。
    ``ensure_ascii=False`` 让中文原样传输，省带宽也便于用 curl 调试。
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, default=str)}")
    # SSE 报文以空行结束
    return "\n".join(lines) + "\n\n"


def format_heartbeat() -> str:
    """心跳。

    以冒号开头的行是 SSE 注释，客户端会忽略，但足以让中间的代理/网关
    认为连接仍然活跃，不至于因为空闲而把它掐断。
    """
    return ": ping\n\n"


@dataclass
class ProgressThrottle:
    """进度写库的节流器。

    模型流式输出时会推送几十到上百个分片，如果每个分片都写一次数据库，
    单个任务就能产生上百次 UPDATE——连接池、WAL、磁盘 IO 全被无意义地消耗。

    这里只在「进度前进足够多」或「距上次写入足够久」时才真正落库，
    保证界面上进度条是平滑推进的，同时把写库次数压到个位数。
    """

    #: 进度至少前进多少个百分点才写
    min_delta: int = 3
    #: 距上次写入至少多少秒才写
    min_interval: float = 1.0

    _last_progress: int = field(default=-1000, init=False)
    _last_at: float = field(default=0.0, init=False)

    def should_emit(self, progress: int, *, force: bool = False) -> bool:
        """是否需要写库。``force=True`` 用于状态切换等必须落库的时刻。"""
        if force:
            self._last_progress = progress
            self._last_at = time.monotonic()
            return True

        now = time.monotonic()
        if progress - self._last_progress < self.min_delta:
            return False
        if now - self._last_at < self.min_interval:
            return False

        self._last_progress = progress
        self._last_at = now
        return True


#: 抽取阶段的进度区间。解析占 10-30，抽取占 30-85，校验占 85-95。
EXTRACT_PROGRESS_START = 30
EXTRACT_PROGRESS_END = 85


def progress_from_stream(received_chars: int, expected_chars: int) -> int:
    """由已接收字符数推算抽取阶段的进度。

    ``expected_chars`` 是预期输出长度的估计值。估不准也没关系——
    这里只影响进度条推进的快慢，最终一定会在抽取结束时被 force 到 85。
    用 ``min`` 兜底，避免超出区间。
    """
    if expected_chars <= 0:
        return EXTRACT_PROGRESS_START

    ratio = min(received_chars / expected_chars, 1.0)
    span = EXTRACT_PROGRESS_END - EXTRACT_PROGRESS_START
    return EXTRACT_PROGRESS_START + int(span * ratio)


# ============================================================================
# 流式分片广播
# ============================================================================
class ChunkKind(StrEnum):
    """推送给前端的流式分片类型。

    在模型的两种输出类型之外多了一个 ``RESET``：输出撞上 max_tokens 时
    客户端会放大预算重跑，此时已推送的分片全部作废。

    **必须显式通知前端清空**，否则界面会把两次输出的文本拼在一起，
    显示出一段看起来合法、实则错乱的 JSON——这比直接报错更难排查，
    因为用户会以为那是模型抽出来的真实结果。

    ``RESET`` 是传输层概念而非模型输出类型，因此定义在这里而不是 AI 层。
    """

    REASONING = "reasoning"
    CONTENT = "content"
    RESET = "reset"


#: 队列上限。慢客户端不能把模型输出堆在内存里——超限时丢弃最旧的分片，
#: 前端表现为"少显示了几个字"，而不是服务端内存持续增长。
_MAX_QUEUE_SIZE = 512


class ChunkBus:
    """进程内的模型输出广播。

    为什么需要它，而状态更新却走数据库轮询：

    - **状态与进度走数据库**：可靠，跨 worker、跨重连、跨进程重启都对。
      代价是最多一个轮询周期（默认 1 秒）的延迟——对进度条完全够用。
    - **逐字输出走进程内广播**：前端要看到模型"正在写字"。这个信息量太大
      （单个任务就有上百个分片），每个分片写一次数据库是不可接受的。

    因此这里是一条**尽力而为**的通道：跨 worker 时订阅者收不到别的 worker
    上跑的模型分片（进度依旧正常）。这是刻意的取舍，写进了 docs/architecture.md。
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[tuple[UUID, ChunkKind, str]]] = set()
        self._dropped = 0

    def subscribe(self) -> asyncio.Queue[tuple[UUID, ChunkKind, str]]:
        queue: asyncio.Queue[tuple[UUID, ChunkKind, str]] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[UUID, ChunkKind, str]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped_count(self) -> int:
        return self._dropped

    def publish(self, task_id: UUID, kind: ChunkKind | StreamKind, text: str) -> None:
        """广播一个分片。**绝不阻塞**——发布者正在跑模型调用，不能被推送拖住。"""
        if not self._subscribers:
            return

        # 归一化：调用方可能传 AI 层的 StreamKind，统一成传输层的 ChunkKind
        normalized = ChunkKind(kind.value) if isinstance(kind, StreamKind) else kind

        for queue in self._subscribers:
            try:
                queue.put_nowait((task_id, normalized, text))
            except asyncio.QueueFull:
                # 客户端消费不过来。丢最旧的，保证最新内容能进去——
                # 用户更关心"现在写到哪了"而不是"十个字之前写了什么"。
                self._dropped += 1
                try:
                    queue.get_nowait()
                    queue.put_nowait((task_id, normalized, text))
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass


#: 进程内单例。lifespan 不需要特别管理它的生命周期——订阅者断开时会自行注销。
chunk_bus = ChunkBus()


def reset_chunk_bus() -> None:
    """清空订阅者。测试之间调用，避免上一个用例的连接影响下一个。"""
    chunk_bus._subscribers.clear()
