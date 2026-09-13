"""统计接口的响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, computed_field

from app.ai.pricing import usd_to_cny


class StatsOut(BaseModel):
    """全局用量与成本统计。

    "我知道每份文档花多少钱"是生产系统与 demo 的分水岭——
    这组数字直接回答"这个功能一个月要花多少钱"。
    """

    total_documents: int = Field(description="已上传的文档总数")
    total_tasks: int = Field(description="抽取任务总数")
    completed_tasks: int
    failed_tasks: int
    running_tasks: int = Field(description="尚未进入终态的任务数")

    total_prompt_tokens: int
    total_completion_tokens: int
    total_reasoning_tokens: int = Field(description="其中思考 token（已含在 completion 内）")
    total_cached_tokens: int = Field(description="命中前缀缓存的输入 token")

    total_cost_usd: float
    avg_latency_ms: float | None = Field(default=None, description="已结束任务的平均耗时")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost_cny(self) -> float:
        return usd_to_cny(self.total_cost_usd)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success_rate(self) -> float:
        """成功率。

        只统计**已结束**的任务——把还在排队的算进分母会让成功率虚低，
        用户看到"成功率 20%"会以为系统有问题，实际只是任务还没跑完。
        """
        finished = self.completed_tasks + self.failed_tasks
        if finished == 0:
            return 0.0
        return round(self.completed_tasks / finished, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def avg_cost_per_task_usd(self) -> float:
        """单任务平均成本。用于回答"调大 max_tokens 值不值"。"""
        if self.total_tasks == 0:
            return 0.0
        return round(self.total_cost_usd / self.total_tasks, 8)
