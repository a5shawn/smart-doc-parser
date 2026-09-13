"""v1 路由汇总。

两个约束写在这里，改动时务必保持：

1. **鉴权在这里统一挂载**，而不是逐个路由加依赖——集中一处，新增模块时不会漏掉，
   也不会出现"某个接口忘了加鉴权"这种最难发现的安全缺口。
2. **``task_stream`` 必须先于 ``tasks`` 注册**。FastAPI 按注册顺序匹配路由，
   ``/tasks/{task_id}`` 会把 ``stream`` 当成 UUID 解析并返回 422。
   把它放在前面，具体的 ``/tasks/stream`` 才优先命中。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1 import documents, health, stats, task_stream, tasks, templates
from app.core.security import verify_api_key

api_router = APIRouter()

# 健康检查必须放在鉴权之外：带鉴权的健康检查会让容器编排永远探活失败。
api_router.include_router(health.router)

# 其余业务接口统一要求 API Key（AUTH_ENABLED=false 时该依赖是空操作）
_protected = APIRouter(dependencies=[Depends(verify_api_key)])
_protected.include_router(task_stream.router)  # 注意：必须在 tasks 之前
_protected.include_router(tasks.router)
_protected.include_router(documents.router)
_protected.include_router(templates.router)
_protected.include_router(stats.router)

api_router.include_router(_protected)
