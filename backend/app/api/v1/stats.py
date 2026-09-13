"""统计接口。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import DbSession
from app.schemas.stats import StatsOut
from app.services import task_service

router = APIRouter(prefix="/stats", tags=["统计"])


@router.get(
    "",
    response_model=StatsOut,
    summary="用量与成本统计",
    description=(
        "累计的 token 用量、实际花费与成功率。\n\n"
        "这组数字用来回答「这个功能一个月要花多少钱」「调大 max_tokens 值不值」，"
        "而不是把成本藏起来等月底看账单。"
    ),
)
async def get_stats(db: DbSession) -> StatsOut:
    stats = await task_service.get_stats(db)
    return StatsOut(
        total_documents=stats.total_documents,
        total_tasks=stats.total_tasks,
        completed_tasks=stats.completed_tasks,
        failed_tasks=stats.failed_tasks,
        running_tasks=stats.running_tasks,
        total_prompt_tokens=stats.total_prompt_tokens,
        total_completion_tokens=stats.total_completion_tokens,
        total_reasoning_tokens=stats.total_reasoning_tokens,
        total_cached_tokens=stats.total_cached_tokens,
        total_cost_usd=stats.total_cost_usd,
        avg_latency_ms=stats.avg_latency_ms,
    )
