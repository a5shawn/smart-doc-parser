"""任务调度器的集成测试。

调度器是异步任务架构的承重墙，这里覆盖它的三条核心承诺：
**中断恢复、并发上限、超时保护**。
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.templates import get_builtin_template
from app.db.enums import TaskStatus
from app.db.models import ExtractionTask
from app.services import task_service
from app.services.task_runner import TaskRunner
from tests.helpers import upload, wait_for_task

pytestmark = pytest.mark.integration


class RecordingPipeline:
    """记录并发峰值的假流水线。"""

    def __init__(self, *, duration: float = 0.15) -> None:
        self._duration = duration
        self.active = 0
        self.peak = 0
        self.started: list[UUID] = []

    async def run(self, task_id: UUID) -> None:
        self.started.append(task_id)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self._duration)
        finally:
            self.active -= 1


class HangingPipeline:
    """永远不返回的假流水线，用于触发超时。"""

    def __init__(self) -> None:
        self.cancelled = False

    async def run(self, task_id: UUID) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _make_task_row(db: AsyncSession, document_id: str) -> ExtractionTask:
    """直接建任务行，不经过接口——这样不会触发应用自己的调度器。"""
    tasks = await task_service.create_tasks(
        db,
        document_ids=[UUID(document_id)],
        template=get_builtin_template("contract"),
        batch_id=uuid4(),
    )
    return tasks[0]


class TestConcurrencyLimit:
    async def test_peak_concurrency_never_exceeds_limit(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        """并发上限失效会把上游限流打满，是最需要守住的一条线。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)

        pipeline = RecordingPipeline()
        runner = TaskRunner(pipeline=pipeline, max_concurrent=2, timeout_seconds=30)

        await runner.submit([uuid4() for _ in range(6)])
        await runner.shutdown()

        assert pipeline.peak <= 2
        assert len(pipeline.started) == 6
        _ = document

    async def test_single_task_runs_immediately(
        self, client: AsyncClient, contract_pdf_bytes: bytes
    ) -> None:
        pipeline = RecordingPipeline(duration=0.0)
        runner = TaskRunner(pipeline=pipeline, max_concurrent=5, timeout_seconds=30)

        await runner.submit([uuid4()])
        await runner.shutdown()

        assert len(pipeline.started) == 1


class TestTimeout:
    async def test_hanging_task_is_marked_failed(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        """超时必须有明确的失败状态，不能让任务永远停在"进行中"。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await _make_task_row(db_session, document["id"])

        pipeline = HangingPipeline()
        runner = TaskRunner(pipeline=pipeline, max_concurrent=1, timeout_seconds=1)

        await runner.submit([task.id])
        # 等待超时触发并完成状态改写
        for _ in range(60):
            await asyncio.sleep(0.1)
            await db_session.refresh(task)
            if task.status is TaskStatus.FAILED:
                break

        assert pipeline.cancelled, "超时后必须取消底层协程，否则模型调用会继续烧钱"
        assert task.status is TaskStatus.FAILED
        assert task.error_code == "TASK_TIMEOUT"
        assert task.progress == 100

        await runner.shutdown()

    async def test_shutdown_cancels_without_marking_failed(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        """服务关闭时的取消不写失败状态——留给下次启动的恢复逻辑统一处理。

        两者都写的话会互相覆盖，且关闭瞬间的批量改写会拖慢退出。
        """
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await _make_task_row(db_session, document["id"])

        runner = TaskRunner(pipeline=HangingPipeline(), max_concurrent=1, timeout_seconds=300)
        await runner.submit([task.id])
        await asyncio.sleep(0.1)

        # 宽限期很短，会走到强制取消分支
        import app.services.task_runner as runner_module

        original = runner_module.SHUTDOWN_GRACE_SECONDS
        runner_module.SHUTDOWN_GRACE_SECONDS = 0.2
        try:
            await runner.shutdown()
        finally:
            runner_module.SHUTDOWN_GRACE_SECONDS = original

        await db_session.refresh(task)
        assert task.status is TaskStatus.PENDING


class TestRecovery:
    async def test_interrupted_tasks_are_marked_failed(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        """进程被 kill 后，残留的"进行中"任务必须变成可重试的失败状态。

        不做这一步，前端进度条会永远卡住，用户不知道该等还是该重试。
        """
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await _make_task_row(db_session, document["id"])

        # 模拟"上次进程退出时它正好在抽取中"
        task.status = TaskStatus.EXTRACTING
        task.progress = 45
        await db_session.commit()

        runner = TaskRunner(pipeline=RecordingPipeline(), max_concurrent=1, timeout_seconds=30)
        recovered = await runner.recover_interrupted_tasks()

        assert recovered == 1
        await db_session.refresh(task)
        assert task.status is TaskStatus.FAILED
        assert task.error_code == "SERVICE_RESTARTED"
        assert "重新抽取" in task.error_message

    async def test_terminal_tasks_are_not_touched(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        """已完成的任务不能被恢复逻辑改坏——那会把用户的结果标记成失败。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await _make_task_row(db_session, document["id"])

        task.status = TaskStatus.COMPLETED
        task.progress = 100
        await db_session.commit()

        runner = TaskRunner(pipeline=RecordingPipeline(), max_concurrent=1, timeout_seconds=30)
        assert await runner.recover_interrupted_tasks() == 0

        await db_session.refresh(task)
        assert task.status is TaskStatus.COMPLETED

    async def test_recovery_handles_every_active_status(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        """pending / parsing / extracting / validating 四种进行中状态都要覆盖到。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)

        for status in (
            TaskStatus.PENDING,
            TaskStatus.PARSING,
            TaskStatus.EXTRACTING,
            TaskStatus.VALIDATING,
        ):
            task = await _make_task_row(db_session, document["id"])
            task.status = status
        await db_session.commit()

        runner = TaskRunner(pipeline=RecordingPipeline(), max_concurrent=1, timeout_seconds=30)
        assert await runner.recover_interrupted_tasks() == 4

        remaining = (
            (
                await db_session.execute(
                    select(ExtractionTask).where(
                        ExtractionTask.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []

    async def test_count_pending(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await _make_task_row(db_session, document["id"])

        runner = TaskRunner(pipeline=RecordingPipeline(), max_concurrent=1, timeout_seconds=30)
        assert await runner.count_pending() == 1

        task.status = TaskStatus.COMPLETED
        await db_session.commit()
        assert await runner.count_pending() == 0


class TestRecoveryThroughApi:
    async def test_recovered_task_can_be_retried(
        self, client: AsyncClient, db_session: AsyncSession, contract_pdf_bytes: bytes
    ) -> None:
        """恢复成失败状态只是第一步，用户必须能一键重跑。"""
        document = await upload(client, "contract.pdf", contract_pdf_bytes)
        task = await _make_task_row(db_session, document["id"])
        task.status = TaskStatus.EXTRACTING
        await db_session.commit()

        runner = TaskRunner(pipeline=RecordingPipeline(), max_concurrent=1, timeout_seconds=30)
        await runner.recover_interrupted_tasks()

        response = await client.post(f"/api/v1/tasks/{task.id}/retry")
        assert response.status_code == 200

        finished = await wait_for_task(client, task.id)
        assert finished["status"] == "completed"


class TestSubmitAfterShutdown:
    async def test_new_tasks_are_rejected_after_shutdown(self) -> None:
        """关闭过程中不再接新活，否则它们会被立刻取消并留下"进行中"的僵尸状态。"""
        runner = TaskRunner(pipeline=RecordingPipeline(), max_concurrent=1, timeout_seconds=30)
        await runner.shutdown()

        await runner.submit([uuid4()])
        assert runner.active_count == 0
