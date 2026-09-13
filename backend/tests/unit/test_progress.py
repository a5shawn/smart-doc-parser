"""进度推送工具的单元测试。"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from app.ai.base import StreamKind
from app.services.progress import (
    EXTRACT_PROGRESS_END,
    EXTRACT_PROGRESS_START,
    ChunkBus,
    ChunkKind,
    ProgressThrottle,
    format_heartbeat,
    format_sse,
    progress_from_stream,
)


class TestFormatSse:
    def test_basic_shape(self) -> None:
        payload = format_sse("progress", {"a": 1})
        assert payload == 'event: progress\ndata: {"a": 1}\n\n'

    def test_ends_with_blank_line(self) -> None:
        """SSE 协议要求报文之间以空行分隔，少了它客户端会一直等下一个字段。"""
        assert format_sse("x", {}).endswith("\n\n")

    def test_event_id_is_emitted_when_given(self) -> None:
        payload = format_sse("x", {}, event_id="7")
        assert "id: 7\n" in payload

    def test_chinese_is_not_escaped(self) -> None:
        """中文原样传输，省带宽也便于用 curl 直接调试。"""
        payload = format_sse("x", {"msg": "抽取中"})
        assert "抽取中" in payload
        assert "\\u" not in payload

    def test_newlines_in_data_are_escaped(self) -> None:
        """载荷里的换行必须被转义，否则一条 data 会被拆成多行、破坏协议。"""
        payload = format_sse("x", {"text": "第一行\n第二行"})

        data_lines = [line for line in payload.splitlines() if line.startswith("data: ")]
        assert len(data_lines) == 1

        # 转义后仍能还原
        decoded = json.loads(data_lines[0][6:])
        assert decoded["text"] == "第一行\n第二行"

    def test_uuid_values_are_serialized(self) -> None:
        """任务 ID 是 UUID 对象，不能被 json 直接序列化，需要 default=str 兜底。"""
        value = uuid4()
        payload = format_sse("x", {"task_id": value})
        assert str(value) in payload

    def test_heartbeat_is_a_comment(self) -> None:
        """以冒号开头是 SSE 注释，客户端会忽略但代理能据此判断连接活跃。"""
        assert format_heartbeat().startswith(":")
        assert format_heartbeat().endswith("\n\n")


class TestProgressThrottle:
    def test_first_report_is_always_emitted(self) -> None:
        throttle = ProgressThrottle()
        assert throttle.should_emit(10) is True

    def test_small_jump_is_suppressed(self) -> None:
        """模型分片有上百个，每个都写库会产生上百次 UPDATE。"""
        throttle = ProgressThrottle(min_delta=5, min_interval=0.0)
        throttle.should_emit(10)
        assert throttle.should_emit(12) is False

    def test_large_jump_is_emitted(self) -> None:
        throttle = ProgressThrottle(min_delta=5, min_interval=0.0)
        throttle.should_emit(10)
        assert throttle.should_emit(16) is True

    def test_interval_suppresses_rapid_writes(self) -> None:
        """进度值跳变很大但时间间隔太短时也要压住，防止瞬时写入风暴。"""
        throttle = ProgressThrottle(min_delta=1, min_interval=60.0)
        throttle.should_emit(10)
        assert throttle.should_emit(50) is False

    def test_force_always_emits(self) -> None:
        """状态切换这类关键节点必须落库，否则前端看不到阶段变化。"""
        throttle = ProgressThrottle(min_delta=100, min_interval=60.0)
        throttle.should_emit(10)
        assert throttle.should_emit(11, force=True) is True

    def test_force_updates_baseline(self) -> None:
        throttle = ProgressThrottle(min_delta=5, min_interval=0.0)
        throttle.should_emit(50, force=True)
        assert throttle.should_emit(52) is False


class TestProgressFromStream:
    def test_zero_received_is_at_start(self) -> None:
        assert progress_from_stream(0, 1000) == EXTRACT_PROGRESS_START

    def test_half_received_is_in_the_middle(self) -> None:
        progress = progress_from_stream(500, 1000)
        assert EXTRACT_PROGRESS_START < progress < EXTRACT_PROGRESS_END

    def test_never_exceeds_the_end(self) -> None:
        """估超了也不能越过区间上限——那会让进度条先冲到 100 再回落。"""
        assert progress_from_stream(99999, 1000) == EXTRACT_PROGRESS_END

    def test_zero_expectation_falls_back_to_start(self) -> None:
        assert progress_from_stream(100, 0) == EXTRACT_PROGRESS_START


class TestChunkBus:
    async def test_publish_reaches_subscriber(self) -> None:
        bus = ChunkBus()
        queue = bus.subscribe()
        task_id = uuid4()

        bus.publish(task_id, StreamKind.CONTENT, "你好")

        received_id, kind, text = queue.get_nowait()
        assert received_id == task_id
        # AI 层的 StreamKind 会被归一化成传输层的 ChunkKind：
        # 后者多了 RESET 这个类型，是 SSE 协议层面的概念
        assert kind is ChunkKind.CONTENT
        assert text == "你好"

    async def test_stream_kind_is_normalized_to_chunk_kind(self) -> None:
        bus = ChunkBus()
        queue = bus.subscribe()

        bus.publish(uuid4(), StreamKind.REASONING, "思考")

        _, kind, _ = queue.get_nowait()
        assert kind is ChunkKind.REASONING
        assert kind.value == "reasoning"

    async def test_reset_marker_can_be_published(self) -> None:
        """输出被截断重跑时用它通知前端丢弃已累积的文本。"""
        bus = ChunkBus()
        queue = bus.subscribe()

        bus.publish(uuid4(), ChunkKind.RESET, "")

        _, kind, text = queue.get_nowait()
        assert kind is ChunkKind.RESET
        assert text == ""

    async def test_publish_without_subscribers_is_a_noop(self) -> None:
        """没有订阅者时不能报错——后台任务照常跑完。"""
        ChunkBus().publish(uuid4(), StreamKind.CONTENT, "x")

    async def test_unsubscribe_stops_delivery(self) -> None:
        bus = ChunkBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)

        bus.publish(uuid4(), StreamKind.CONTENT, "x")
        assert queue.empty()

    async def test_all_subscribers_receive(self) -> None:
        """全局流是单连接的，但测试或调试时可能有多个订阅者。"""
        bus = ChunkBus()
        first, second = bus.subscribe(), bus.subscribe()

        bus.publish(uuid4(), StreamKind.CONTENT, "x")

        assert not first.empty()
        assert not second.empty()

    async def test_slow_subscriber_drops_oldest_not_newest(self) -> None:
        """慢客户端不能把模型输出堆在服务端内存里。

        丢最旧的而不是最新的：用户更关心"现在写到哪了"。
        """
        bus = ChunkBus()
        queue = bus.subscribe()

        for index in range(600):  # 超过队列上限 512
            bus.publish(uuid4(), StreamKind.CONTENT, str(index))

        assert bus.dropped_count > 0
        assert queue.full()
        # 最后一条一定还在
        latest = None
        while not queue.empty():
            latest = queue.get_nowait()
        assert latest is not None and latest[2] == "599"

    async def test_publish_never_blocks(self) -> None:
        """发布者正在跑模型调用，绝不能被推送拖住。"""
        bus = ChunkBus()
        bus.subscribe()

        async def flood() -> None:
            for _ in range(2000):
                bus.publish(uuid4(), StreamKind.CONTENT, "x")

        await asyncio.wait_for(flood(), timeout=1.0)

    async def test_subscriber_count(self) -> None:
        bus = ChunkBus()
        assert bus.subscriber_count == 0
        queue = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.unsubscribe(queue)
        assert bus.subscriber_count == 0
