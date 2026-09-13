"""进程内的任务调度器。

为什么不用 Celery / RQ / ARQ
----------------------------
单机部署下，引入 Redis + 独立 worker 进程换来的是额外的运维成本，
而它能提供的能力（跨进程分发、水平扩容）在这个规模上用不到。

代价必须在文档里说清楚：**调度器是进程内的**，因此

- ``BACKEND_WORKERS > 1`` 时每个 worker 各有自己的并发计数，实际并发上限会翻倍
- 需要横向扩容时，得先把 ``TaskPipeline`` 拆到独立进程（接口已经预留好了）

这两条都写在 docs/architecture.md 里，属于"知道边界在哪"而不是"假装没有边界"。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update

from app.core.errors import TaskTimeoutError
from app.core.logging import get_logger
from app.db.enums import STAGE_LABELS, STAGE_PROGRESS, TaskStatus
from app.db.models import ExtractionTask
from app.db.session import session_scope
from app.services.task_pipeline import TaskPipeline

logger = get_logger(__name__)

#: 关闭时等待在途任务收尾的秒数
SHUTDOWN_GRACE_SECONDS = 5.0


class TaskRunner:
    """后台任务调度器。整个进程一个实例，随 lifespan 创建与销毁。"""

    def __init__(
        self,
        *,
        pipeline: TaskPipeline,
        max_concurrent: int,
        timeout_seconds: int,
    ) -> None:
        self._pipeline = pipeline
        self._timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        #: 持有后台任务的强引用。**不持有的话，事件循环可能在任务跑完前
        #: 把它当垃圾回收掉**——这是 asyncio 最隐蔽的坑之一，表现为任务莫名中断。
        self._running: set[asyncio.Task[None]] = set()
        self._accepting = True
        self.max_concurrent = max_concurrent

    @property
    def active_count(self) -> int:
        """当前在跑的任务数（含排队等待信号量的）。"""
        return len(self._running)

    # ------------------------------------------------------------------
    # 提交与执行
    # ------------------------------------------------------------------
    async def submit(self, task_ids: list[UUID]) -> None:
        """把任务交给后台执行，立即返回。

        调用方（接口层）不需要等待——这正是"异步任务 + SSE 进度"的意义所在。
        """
        if not self._accepting:
            logger.warning("task_runner_not_accepting", rejected=len(task_ids))
            return

        for task_id in task_ids:
            background = asyncio.create_task(self._run_one(task_id), name=f"extract-{task_id}")
            self._running.add(background)
            background.add_done_callback(self._running.discard)

        logger.info("tasks_submitted", count=len(task_ids), active=self.active_count)

    async def _run_one(self, task_id: UUID) -> None:
        """执行单个任务：受信号量限流，受超时保护。"""
        async with self._semaphore:
            try:
                await asyncio.wait_for(self._pipeline.run(task_id), timeout=self._timeout_seconds)
            except TimeoutError:
                # wait_for 超时会取消内层任务，CancelledError 已经在 pipeline 里
                # 被重新抛出。这里用**新的会话**写状态——原来那个已经随取消关闭了。
                logger.warning(
                    "task_timed_out",
                    task_id=str(task_id),
                    timeout_seconds=self._timeout_seconds,
                )
                await self._mark_timed_out(task_id)
            except asyncio.CancelledError:
                # 服务关闭时的取消。不写状态：恢复逻辑会在下次启动时处理它。
                logger.info("task_cancelled_by_shutdown", task_id=str(task_id))
                raise
            except Exception:
                # pipeline 内部已经把业务异常落成了任务状态，
                # 能漏到这里的都是它自己也兜不住的（例如写库失败），记日志即可。
                logger.error("task_runner_unhandled_error", task_id=str(task_id), exc_info=True)

    @staticmethod
    async def _mark_timed_out(task_id: UUID) -> None:
        error = TaskTimeoutError()
        async with session_scope() as db:
            task = await db.get(ExtractionTask, task_id)
            if task is None or task.status.is_terminal:
                return
            task.status = TaskStatus.FAILED
            task.progress = STAGE_PROGRESS[TaskStatus.FAILED]
            task.stage_message = STAGE_LABELS[TaskStatus.FAILED]
            task.error_code = error.code
            task.error_message = error.message
            task.finished_at = datetime.now(UTC)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def recover_interrupted_tasks(self) -> int:
        """把上次进程退出时残留的「进行中」任务标记为失败。

        进程被 kill、容器被重启、机器断电——这些情况下任务会永远停在
        ``extracting`` 状态，前端进度条卡住不动，用户也不知道该等还是该重试。
        **明确地失败并允许重试，比假装还在跑要好得多。**

        为什么是标记失败而不是自动重新入队：自动重跑会在"任务本身必然失败"
        的情况下（例如文档已损坏）形成无限重启循环，把额度烧光。
        重试交给用户显式点击。
        """
        async with session_scope() as db:
            result = await db.execute(
                update(ExtractionTask)
                .where(ExtractionTask.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED]))
                .values(
                    status=TaskStatus.FAILED,
                    progress=STAGE_PROGRESS[TaskStatus.FAILED],
                    stage_message=STAGE_LABELS[TaskStatus.FAILED],
                    error_code="SERVICE_RESTARTED",
                    error_message="服务重启导致任务中断，请点击「重新抽取」",
                    finished_at=func.now(),
                )
                .returning(ExtractionTask.id)
            )
            recovered_ids = result.scalars().all()

        if recovered_ids:
            logger.warning(
                "interrupted_tasks_recovered",
                count=len(recovered_ids),
                task_ids=[str(item) for item in recovered_ids[:20]],
            )
        return len(recovered_ids)

    async def count_pending(self) -> int:
        """还没进入终态的任务数。启动时用来判断是否需要提示。"""
        async with session_scope() as db:
            return (
                await db.scalar(
                    select(func.count(ExtractionTask.id)).where(
                        ExtractionTask.status.notin_([TaskStatus.COMPLETED, TaskStatus.FAILED])
                    )
                )
                or 0
            )

    async def shutdown(self) -> None:
        """停止接收新任务，并给在途任务一点收尾时间。

        直接全部 cancel 会让已经调用了模型、只差写库的任务丢掉结果——
        钱花了，结果没了，用户还得重跑一次。给一个短暂的宽限期更划算。
        """
        self._accepting = False

        if not self._running:
            return

        logger.info("task_runner_draining", active=len(self._running))
        pending = list(self._running)

        done, still_running = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)

        if still_running:
            logger.warning("task_runner_force_cancel", count=len(still_running))
            for background in still_running:
                background.cancel()
            await asyncio.gather(*still_running, return_exceptions=True)

        logger.info("task_runner_stopped", finished=len(done))
