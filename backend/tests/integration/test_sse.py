"""SSE 进度推送的集成测试。

为什么直接驱动异步生成器，而不走 HTTP 客户端
--------------------------------------------
**httpx 的 ``ASGITransport`` 会把整个响应体缓冲完才返回**
（``ASGIResponseStream`` 的实现是 ``yield b"".join(self._body)``），
而 SSE 是永不结束的流——用 HTTP 客户端测会直接挂死。

因此这里直接调用 ``_event_stream`` 生成器，逐帧读取。这样测到的是同一份业务逻辑，
而且确定、快速。HTTP 层的属性（响应头、媒体类型）通过直接调用路由函数来验证。
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.ai.base import StreamKind
from app.ai.fake import FakeLLMClient, ScriptedResponse
from app.api.v1.task_stream import _event_stream, stream_tasks
from app.services.progress import chunk_bus
from tests.helpers import (
    FakeRequest,
    create_task,
    parse_sse,
    read_sse_events,
    read_sse_raw,
    upload,
    wait_for_task,
)

pytestmark = pytest.mark.integration

PAYLOAD = json.dumps(
    {"contract_name": "采购合同", "party_a": "甲公司", "party_b": "乙公司"}, ensure_ascii=False
)


class SlowFakeLLMClient(FakeLLMClient):
    """在返回前先等一会儿，好在流式期间观察进度事件。"""

    def __init__(self, delay: float = 0.4) -> None:
        super().__init__([ScriptedResponse(content=PAYLOAD)])
        self._delay = delay

    async def complete_json(self, **kwargs):
        await asyncio.sleep(self._delay)
        return await super().complete_json(**kwargs)


@pytest.fixture
def llm_client() -> FakeLLMClient:
    return SlowFakeLLMClient()


class TestHttpContract:
    """HTTP 层属性。不迭代响应体，因此不会有缓冲问题。"""

    async def test_headers_are_sse_friendly(self) -> None:
        response = await stream_tasks(FakeRequest(), task_ids=None, batch_id=None)

        assert response.media_type == "text/event-stream"
        # 这三行是 SSE 能否穿过 nginx 的关键，缺一个都可能被缓冲成一坨
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["connection"] == "keep-alive"

        await response.body_iterator.aclose()

    async def test_malformed_task_ids_fall_back_to_global(self) -> None:
        """参数写错不该让连接直接 500，退化成全局流是更合理的行为。"""
        response = await stream_tasks(FakeRequest(), task_ids="not-a-uuid", batch_id=None)
        assert response.status_code == 200
        await response.body_iterator.aclose()

    async def test_task_ids_are_parsed(self) -> None:
        task_id = uuid4()
        response = await stream_tasks(FakeRequest(), task_ids=str(task_id), batch_id=None)

        events = await read_sse_events(response.body_iterator, limit=1)
        assert events[0][0] == "snapshot"
        assert events[0][1]["tasks"] == []


class TestSnapshot:
    async def test_snapshot_is_sent_on_connect(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """晚订阅的客户端必须立刻看到当前状态，而不是从 0 开始等。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])
        await wait_for_task(client, task["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None), limit=1
        )

        event_name, payload = events[0]
        assert event_name == "snapshot"
        assert len(payload["tasks"]) == 1
        assert payload["tasks"][0]["id"] == task["id"]

    async def test_snapshot_carries_full_task_shape(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """快照里的字段要和 REST 接口一致，前端才能用同一套渲染逻辑。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])
        await wait_for_task(client, task["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None), limit=1
        )

        item = events[0][1]["tasks"][0]
        for field in ("id", "status", "progress", "document_filename", "template_name"):
            assert field in item

    async def test_global_stream_includes_running_tasks(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=None, batch_id=None), limit=1
        )

        assert any(item["id"] == task["id"] for item in events[0][1]["tasks"])


class TestProgressEvents:
    async def test_progress_and_done_events_during_execution(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None),
            limit=99,
        )

        names = [name for name, _ in events]
        assert names[0] == "snapshot"
        assert "progress" in names
        assert names[-1] == "done"

    async def test_progress_never_goes_backwards(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """进度条倒退是最容易被用户当成故障的现象。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None),
            limit=99,
        )

        progresses = [payload["progress"] for name, payload in events if name == "progress"]
        assert progresses == sorted(progresses)
        assert progresses[-1] == 100

    async def test_done_event_carries_terminal_status(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None),
            limit=99,
        )

        done = [payload for name, payload in events if name == "done"]
        assert len(done) == 1
        assert done[0]["status"] == "completed"

    async def test_stream_closes_when_tracked_tasks_finish(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """显式指定任务时连接会自动收尾，客户端不用猜什么时候能断开。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])

        generator = _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None)

        # 读到生成器自然结束（StopAsyncIteration），而不是被 limit 截断
        names: list[str | None] = []
        async for raw in generator:
            names.append(parse_sse(raw)[0])

        assert names[-1] == "done"

    async def test_terminal_task_only_produces_snapshot(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """已经处于终态的任务不该被反复推送——那是纯粹的带宽浪费。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])
        await wait_for_task(client, task["id"])

        events = await read_sse_events(
            _event_stream(FakeRequest(), task_ids=[UUID(task["id"])], batch_id=None),
            limit=99,
        )

        assert [name for name, _ in events] == ["snapshot"]


class TestChunkEvents:
    async def test_model_chunks_are_forwarded(self, app: FastAPI) -> None:
        """模型逐字输出走进程内广播，前端据此显示"正在写字"。"""
        generator = _event_stream(FakeRequest(), task_ids=None, batch_id=None)
        # 先取快照，确认生成器已经在运行并完成了订阅
        await asyncio.wait_for(generator.__anext__(), timeout=5.0)

        task_id = uuid4()
        chunk_bus.publish(task_id, StreamKind.REASONING, "分析文档中")
        chunk_bus.publish(task_id, StreamKind.CONTENT, '{"party_a"')

        events = await read_sse_events(generator, limit=2)

        chunks = [payload for name, payload in events if name == "chunk"]
        assert len(chunks) == 2
        assert chunks[0]["kind"] == "reasoning"
        assert chunks[1]["kind"] == "content"
        assert chunks[1]["task_id"] == str(task_id)

    async def test_chunks_are_filtered_when_scope_is_given(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """指定跟踪范围时服务端就该过滤，否则每个连接都要传大量无关内容。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await create_task(client, document["id"])
        task_id = UUID(task["id"])

        generator = _event_stream(FakeRequest(), task_ids=[task_id], batch_id=None)
        await asyncio.wait_for(generator.__anext__(), timeout=5.0)

        chunk_bus.publish(uuid4(), StreamKind.CONTENT, "别的任务的内容")
        chunk_bus.publish(task_id, StreamKind.CONTENT, "本任务的内容")

        # 任务正在运行，中间会夹着 progress 事件与它自己的模型输出分片，
        # 因此要多读几帧再筛选
        events = await read_sse_events(generator, limit=15)
        chunks = [payload["text"] for name, payload in events if name == "chunk"]

        assert "本任务的内容" in chunks
        assert "别的任务的内容" not in chunks


class TestLifecycle:
    async def test_subscriber_is_released_after_disconnect(self, app: FastAPI) -> None:
        """断开必须注销订阅者，否则每次刷新页面都泄漏一个队列。"""
        generator = _event_stream(FakeRequest(), task_ids=None, batch_id=None)
        await asyncio.wait_for(generator.__anext__(), timeout=5.0)
        assert chunk_bus.subscriber_count == 1

        await generator.aclose()
        assert chunk_bus.subscriber_count == 0

    async def test_disconnect_check_is_polled(self, app: FastAPI) -> None:
        """客户端断开后生成器要自己收尾，不能一直空转。"""
        request = FakeRequest(disconnect_after=0)

        # 第一次检查就返回"已断开"，生成器应该立刻结束
        events = await read_sse_events(
            _event_stream(request, task_ids=None, batch_id=None), limit=99
        )

        assert [name for name, _ in events] == ["snapshot"]
        assert chunk_bus.subscriber_count == 0

    async def test_heartbeat_is_emitted_when_idle(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """长时间没有状态变化时要有心跳，否则中间的代理会因为空闲掐断连接。"""
        from app.core.config import settings

        monkeypatch.setattr(settings, "SSE_HEARTBEAT_SECONDS", 0.01)

        frames = await read_sse_raw(
            _event_stream(FakeRequest(), task_ids=None, batch_id=None), limit=2
        )

        assert frames[0].startswith("event: snapshot")
        # 心跳是以冒号开头的注释行，客户端会忽略，但代理据此判断连接活跃
        assert frames[1].startswith(": ping")
