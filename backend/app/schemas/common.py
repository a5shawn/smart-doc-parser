"""通用响应结构：分页信封与错误信封。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, computed_field

#: 分页参数的默认值与上限。上限存在的意义是防止调用方用 page_size=100000 拖垮数据库。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page[T](BaseModel):
    """分页信封。列表接口一律返回这个结构，前端只需写一次分页逻辑。"""

    items: list[T]
    total: int = Field(description="符合条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pages(self) -> int:
        """总页数。空结果集返回 0 而不是 1，前端据此显示"暂无数据"。"""
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


class ErrorDetail(BaseModel):
    code: str = Field(description="稳定的错误标识，供程序判断")
    message: str = Field(description="面向人的中文说明")
    details: dict[str, Any] = Field(default_factory=dict, description="附加上下文")
    request_id: str = Field(description="请求 ID，排查问题时提供给后端")


class ErrorResponse(BaseModel):
    """所有非 2xx 响应的统一结构。"""

    error: ErrorDetail
