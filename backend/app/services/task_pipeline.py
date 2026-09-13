"""单个抽取任务的执行流水线。

    parsing（取文本） → extracting（调模型） → validating（校验） → completed

**这个类自己管理数据库会话，绝不复用请求的会话。**
请求一结束会话就被关掉了，后台任务再往里写会抛 "This session is closed"
或者悄悄泄漏连接池里的连接——这类问题在开发机上往往看不出来，
因为请求量小、连接池宽裕，到了线上高并发时才以"连接耗尽"的形式爆出来。
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import LLMUsage, StreamKind
from app.ai.extractor import Extractor
from app.ai.pricing import estimate_cost_usd
from app.ai.templates import ExtractionTemplate, load_builtin_templates
from app.core.errors import AppError, NoTextLayerError
from app.core.logging import get_logger
from app.db.enums import STAGE_LABELS, STAGE_PROGRESS, TaskStatus
from app.db.models import Document, ExtractionResult, ExtractionTask
from app.db.session import session_scope
from app.services.progress import ChunkKind, ProgressThrottle, chunk_bus, progress_from_stream

logger = get_logger(__name__)


def estimate_output_chars(template: ExtractionTemplate) -> int:
    """粗估模型输出 JSON 的字符数，用来把「已接收字符数」换算成进度百分比。

    估不准无所谓：它只影响进度条推进的快慢，抽取结束时会被强制推到区间上限。
    """
    total = 20
    for field in template.fields:
        if field.type == "array":
            item_cost = 70 if field.item_type == "object" else 20
            total += item_cost * 3 + len(field.name) + 10
        else:
            total += 45 + len(field.name)
    return max(total, 120)


class TaskPipeline:
    """执行一个任务的完整流程。"""

    def __init__(self, *, extractor: Extractor, max_input_chars: int) -> None:
        self._extractor = extractor
        self._max_input_chars = max_input_chars

    async def run(self, task_id: UUID) -> None:
        """执行任务。异常在内部消化并落成任务状态，不向外抛。"""
        async with session_scope() as db:
            task = await db.get(ExtractionTask, task_id)
            if task is None:
                logger.warning("task_missing_when_running", task_id=str(task_id))
                return

            if task.status.is_terminal:
                # 可能被重复提交，或者任务在排队期间被删除/重试了
                logger.info(
                    "task_skipped_already_terminal",
                    task_id=str(task_id),
                    status=task.status.value,
                )
                return

            document = await db.get(Document, task.document_id)
            if document is None:
                await self._mark_failed(
                    db, task, "DOCUMENT_NOT_FOUND", "关联的文档已被删除，请重新上传"
                )
                return

            task.started_at = datetime.now(UTC)
            task.attempts += 1
            task.error_code = None
            task.error_message = None
            started = time.perf_counter()

            try:
                await self._execute(db, task, document, started=started)
            except asyncio.CancelledError:
                # 超时或服务关闭导致的取消。
                # **必须原样抛出**，否则 asyncio.wait_for 无法感知取消，
                # 任务会一直挂在"进行中"，而后台其实早就没人管了。
                # 状态由 TaskRunner 用另一个会话改写（当前会话即将被关闭）。
                logger.warning("task_cancelled", task_id=str(task_id))
                raise
            except AppError as exc:
                await self._mark_failed(db, task, exc.code, exc.message)
            except Exception as exc:
                logger.error(
                    "task_unexpected_error",
                    task_id=str(task_id),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                await self._mark_failed(
                    db,
                    task,
                    "INTERNAL_ERROR",
                    f"处理过程中发生未预期的错误（{type(exc).__name__}），请重试或联系管理员",
                )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def _execute(
        self,
        db: AsyncSession,
        task: ExtractionTask,
        document: Document,
        *,
        started: float,
    ) -> None:
        template = self._resolve_template(task)

        # ---- 1. 解析文档 ----
        await self._set_state(db, task, TaskStatus.PARSING, message="正在读取文档文本")

        text = document.text_content
        if not text:
            # 扫描件走到这里。报错信息要说明原因和下一步，而不是一句"解析失败"。
            raise NoTextLayerError(
                document.text_error or "文档没有可提取的文本内容，无法进行信息抽取"
            )

        # ---- 2. 调用模型 ----
        await self._set_state(db, task, TaskStatus.EXTRACTING, message="大模型抽取中")

        throttle = ProgressThrottle()
        expected_chars = estimate_output_chars(template)
        received_chars = 0

        async def on_chunk(kind: StreamKind, delta: str) -> None:
            nonlocal received_chars

            # 逐字输出走进程内广播（不落库）——前端据此显示模型"正在写字"
            chunk_bus.publish(task.id, kind, delta)

            if kind is StreamKind.CONTENT:
                received_chars += len(delta)
            progress = progress_from_stream(received_chars, expected_chars)
            if throttle.should_emit(progress):
                # 进度落库，且经过节流——否则单个任务会产生上百次 UPDATE
                task.progress = progress
                await db.commit()

        async def on_restart() -> None:
            # 输出被截断要重跑，已接收的字符数归零，否则进度会虚高
            nonlocal received_chars
            received_chars = 0

            # 通知前端丢弃已推送的分片。不发这个信号的话，界面会把两次输出
            # 拼在一起，显示出一段看着合法、实则错乱的 JSON——
            # 用户会以为那是模型抽出来的真实结果，比直接报错更危险。
            chunk_bus.publish(task.id, ChunkKind.RESET, "")

            await self._set_state(
                db, task, TaskStatus.EXTRACTING, message="输出被截断，正在以更大预算重试"
            )

        outcome = await self._extractor.extract(
            template=template,
            document_text=text,
            max_input_chars=self._max_input_chars,
            on_chunk=on_chunk,
            on_restart=on_restart,
        )

        # ---- 3. 校验并落库 ----
        await self._set_state(db, task, TaskStatus.VALIDATING, message="校验抽取结果")

        db.add(
            ExtractionResult(
                task_id=task.id,
                data=outcome.data,
                raw_output=outcome.raw_output,
                warnings=outcome.warnings,
                truncated=outcome.truncated,
            )
        )

        self._apply_usage(task, outcome.usage, outcome.model)

        # 总耗时在这里算并随同一次提交落库。
        # 放在调用方（run）的成功分支里算的话，日志会先于赋值打印，
        # 表现为每条完成日志的 latency_ms 都是 None——看着像没记录耗时，
        # 实际是记录了的，非常误导排查。
        task.latency_ms = int((time.perf_counter() - started) * 1000)
        task.status = TaskStatus.COMPLETED
        task.progress = STAGE_PROGRESS[TaskStatus.COMPLETED]
        task.stage_message = STAGE_LABELS[TaskStatus.COMPLETED]
        task.finished_at = datetime.now(UTC)
        await db.commit()

        logger.info(
            "task_completed",
            task_id=str(task.id),
            template_key=task.template_key,
            attempts=task.attempts,
            warning_count=len(outcome.warnings),
            latency_ms=task.latency_ms,
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_template(task: ExtractionTask) -> ExtractionTemplate:
        """还原任务使用的模板。

        优先用任务行里的 ``custom_schema`` 快照，而不是去查模板表——
        模板被改名或删除后，重跑历史任务仍应复现当时的行为。
        """
        if task.custom_schema:
            return ExtractionTemplate.from_custom_schema(
                key=task.template_key,
                name=task.template_name,
                schema=task.custom_schema,
            )

        builtin = load_builtin_templates().get(task.template_key)
        if builtin is None:
            # 模板不存在（内置模板被删掉了），退回用任务快照里的名字构造一个空模板
            raise AppError(
                f"模板 {task.template_key} 已不可用，请重新选择模板创建任务",
                details={"template_key": task.template_key},
            )
        return builtin

    async def _set_state(
        self,
        db: AsyncSession,
        task: ExtractionTask,
        status: TaskStatus,
        *,
        message: str | None = None,
    ) -> None:
        """切换阶段并立即提交。

        **必须提交**：SSE 端点用的是另一个会话，未提交的改动它看不见，
        前端进度条就会一直停在初始状态。
        """
        task.status = status
        task.progress = STAGE_PROGRESS[status]
        task.stage_message = message or STAGE_LABELS[status]
        await db.commit()

    @staticmethod
    def _apply_usage(task: ExtractionTask, usage: LLMUsage, model: str) -> None:
        task.model = model
        task.prompt_tokens = usage.prompt_tokens
        task.completion_tokens = usage.completion_tokens
        task.reasoning_tokens = usage.reasoning_tokens
        task.cached_tokens = usage.cached_tokens
        task.cost_usd = estimate_cost_usd(usage)

    async def _mark_failed(
        self,
        db: AsyncSession,
        task: ExtractionTask,
        code: str,
        message: str,
    ) -> None:
        task.status = TaskStatus.FAILED
        task.progress = STAGE_PROGRESS[TaskStatus.FAILED]
        task.error_code = code
        task.error_message = message[:1000]
        task.stage_message = "失败"
        task.finished_at = datetime.now(UTC)
        await db.commit()

        logger.warning(
            "task_failed",
            task_id=str(task.id),
            error_code=code,
            error_message=message,
        )
