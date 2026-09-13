"""请求上下文中间件：请求 ID 透传 + 访问日志。

为什么用纯 ASGI 中间件而不是 Starlette 的 ``BaseHTTPMiddleware``
------------------------------------------------------------
``BaseHTTPMiddleware`` 会在应用与服务器之间再插一层 anyio 内存流，
对流式响应（本项目重度依赖 SSE）可能引入缓冲与背压问题。
纯 ASGI 中间件只做「读 header / 写 header / 记一条日志」，
不碰响应体，从根上避免把 SSE 攒成一坨。这也是排查"SSE 不走"时的常见暗坑之一。
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

logger = get_logger(__name__)

_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")

# 网关/代理常会注入的请求 ID 头，按优先级取第一个存在的
_INBOUND_REQUEST_ID_HEADERS = ("x-request-id", "x-correlation-id", "traceparent")


def get_request_id() -> str:
    """取当前请求的 ID。不在请求上下文中时返回空串。"""
    return _request_id_ctx.get()


class RequestContextMiddleware:
    """为每个请求分配请求 ID，并记录一条包含耗时的访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 只处理 HTTP；websocket / lifespan 直接放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = self._extract_request_id(headers)

        token = _request_id_ctx.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
                # 回写请求 ID，方便前端在报错时把它一起提供给后端排查
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
                # 显式告诉 nginx 等反向代理不要缓冲这条响应。
                # 与 nginx 的 proxy_buffering off 是双重保险：任一侧生效，SSE 就能逐条吐出。
                MutableHeaders(scope=message)["X-Accel-Buffering"] = "no"
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            client = scope.get("client")
            logger.info(
                "http_request",
                method=scope.get("method"),
                path=scope.get("path"),
                query=scope.get("query_string", b"").decode("latin-1"),
                status_code=status_code,
                duration_ms=round(duration_ms, 2),
                client_ip=client[0] if client else None,
                # SSE 这类长连接如果在响应开始前就断了，单独标记出来便于排查
                client_disconnected=not response_started,
            )
            structlog.contextvars.unbind_contextvars("request_id")
            _request_id_ctx.reset(token)

    @staticmethod
    def _extract_request_id(headers: Headers) -> str:
        for name in _INBOUND_REQUEST_ID_HEADERS:
            value = headers.get(name)
            if value:
                # traceparent 形如 00-<trace-id>-<span-id>-<flags>，取中间段更有意义
                if name == "traceparent" and value.count("-") >= 3:
                    return value.split("-")[1]
                return value
        return uuid4().hex


__all__ = ["RequestContextMiddleware", "get_request_id"]
