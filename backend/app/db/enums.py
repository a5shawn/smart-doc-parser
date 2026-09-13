"""数据库枚举类型。

放在独立模块里，避免 models / schemas / services 之间为了拿到枚举而互相 import。
"""

from __future__ import annotations

from enum import StrEnum


class TextStatus(StrEnum):
    """文档文本提取结果。"""

    EXTRACTED = "extracted"  # 成功取到文本
    NO_TEXT_LAYER = "no_text_layer"  # 能打开但没有文本层（扫描件）
    FAILED = "failed"  # 文件损坏或解析器报错


class TaskStatus(StrEnum):
    """抽取任务状态机。

    .. code-block:: text

        pending ──► parsing ──► extracting ──► validating ──► completed
           │           │            │             │
           └───────────┴────────────┴─────────────┴────────► failed

    只有 completed / failed 是终态。
    """

    PENDING = "pending"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED)

    @property
    def is_active(self) -> bool:
        """是否处于「进行中」。用于启动时识别需要恢复的残留任务。"""
        return not self.is_terminal


#: 各阶段的初始进度，前端进度条据此推进
STAGE_PROGRESS: dict[TaskStatus, int] = {
    TaskStatus.PENDING: 0,
    TaskStatus.PARSING: 10,
    TaskStatus.EXTRACTING: 30,
    TaskStatus.VALIDATING: 90,
    TaskStatus.COMPLETED: 100,
    TaskStatus.FAILED: 100,
}

#: 阶段的中文说明，直接展示在前端进度条下方
STAGE_LABELS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "排队中",
    TaskStatus.PARSING: "解析文档",
    TaskStatus.EXTRACTING: "大模型抽取中",
    TaskStatus.VALIDATING: "校验抽取结果",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.FAILED: "失败",
}
