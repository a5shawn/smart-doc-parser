"""接口鉴权。

按环境变量 ``AUTH_ENABLED`` 开关：

- ``false``（默认）：完全开放，适合本地开发与公网作品集演示
- ``true``：所有业务接口要求 ``X-API-Key`` 请求头（也兼容 ``Authorization: Bearer``）

健康检查接口永远不鉴权，否则容器编排的健康检查会一直失败。
"""

from __future__ import annotations

import secrets

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.core.errors import UnauthorizedError

# auto_error=False 让我们自己抛统一格式的异常，而不是 FastAPI 默认的 {"detail": ...}
_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description=(
        "接口密钥。仅在 AUTH_ENABLED=true 时校验。也可用 `Authorization: Bearer <key>` 传递。"
    ),
)


def _extract_presented_key(request: Request, header_value: str | None) -> str:
    """从 X-API-Key 或 Authorization: Bearer 中取出调用方提供的密钥。"""
    if header_value:
        return header_value.strip()

    authorization = request.headers.get("Authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() == "bearer" and credentials:
        return credentials.strip()

    return ""


def verify_api_key(request: Request, header_value: str | None = Security(_api_key_header)) -> None:
    """FastAPI 依赖：校验通过返回 None，失败抛 401。

    使用 ``secrets.compare_digest`` 而非 ``==`` 做比较，避免通过响应时间差
    逐字节猜测密钥（时序攻击）。
    """
    if not settings.AUTH_ENABLED:
        return

    presented = _extract_presented_key(request, header_value)
    if not presented:
        raise UnauthorizedError(
            "缺少 API Key。请在请求头中携带 X-API-Key。",
            details={"header": "X-API-Key"},
        )

    # 逐个比对而不是用 in 判断，保证每个候选 key 都走等时比较
    matched = any(secrets.compare_digest(presented, valid) for valid in settings.API_KEYS)
    if not matched:
        raise UnauthorizedError("API Key 无效。")
