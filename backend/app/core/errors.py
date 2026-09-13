"""领域异常与统一错误响应。

约定
----
所有对外错误都返回同一个信封结构，前端只需处理一种形状：

.. code-block:: json

    {
      "error": {
        "code": "FILE_TOO_LARGE",
        "message": "文件超过 20 MB 上限",
        "details": {"size_bytes": 31457280, "limit_bytes": 20971520},
        "request_id": "0f3c...",
      }
    }

``code`` 是给程序判断的稳定标识（前端据此决定展示方式），``message`` 是给人看的中文说明。
把两者分开，前端才能在文案调整时不至于失效。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger
from app.core.middleware import get_request_id

logger = get_logger(__name__)


# ============================================================================
# 异常基类与子类
# ============================================================================
class AppError(Exception):
    """所有业务异常的基类。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_message: str = "服务内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


# ---------------------------- 通用 ----------------------------
class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "请求参数不合法"


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = status.HTTP_404_NOT_FOUND
    default_message = "资源不存在"


class ConflictError(AppError):
    code = "CONFLICT"
    http_status = status.HTTP_409_CONFLICT
    default_message = "资源状态冲突"


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    http_status = status.HTTP_401_UNAUTHORIZED
    default_message = "缺少或无效的 API Key"


class ServiceUnavailableError(AppError):
    code = "SERVICE_UNAVAILABLE"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    default_message = "依赖服务暂不可用"


# ---------------------------- 文件与解析 ----------------------------
class FileValidationError(AppError):
    code = "FILE_VALIDATION_ERROR"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "文件校验未通过"


class FileTypeMismatchError(FileValidationError):
    code = "FILE_TYPE_MISMATCH"
    default_message = "不支持的文件类型"


class FileTooLargeError(FileValidationError):
    code = "FILE_TOO_LARGE"
    http_status = status.HTTP_413_CONTENT_TOO_LARGE
    default_message = "文件超过大小上限"


class EmptyFileError(FileValidationError):
    code = "EMPTY_FILE"
    default_message = "文件内容为空"


class ParsingError(AppError):
    code = "PARSING_ERROR"
    http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    default_message = "文档解析失败"


class NoTextLayerError(ParsingError):
    """扫描版 PDF 没有文本层。

    单独建一个异常类型是因为它需要在前端以「提示」而非「报错」呈现——
    这不是系统故障，而是文档本身的形态问题，用户换一份电子版即可。
    """

    code = "PDF_NO_TEXT_LAYER"
    default_message = "该 PDF 没有可提取的文本层，可能是扫描件"


class CorruptedFileError(ParsingError):
    code = "CORRUPTED_FILE"
    default_message = "文件已损坏或格式不完整"


# ---------------------------- 任务与模板 ----------------------------
class DocumentNotFoundError(NotFoundError):
    code = "DOCUMENT_NOT_FOUND"
    default_message = "文档不存在"


class TaskNotFoundError(NotFoundError):
    code = "TASK_NOT_FOUND"
    default_message = "抽取任务不存在"


class ResultNotFoundError(NotFoundError):
    code = "RESULT_NOT_FOUND"
    default_message = "该任务尚无抽取结果"


class TemplateNotFoundError(NotFoundError):
    code = "TEMPLATE_NOT_FOUND"
    default_message = "抽取模板不存在"


class TemplateInUseError(ConflictError):
    code = "TEMPLATE_IN_USE"
    default_message = "该模板正在被任务使用，无法删除"


class InvalidSchemaError(ValidationError):
    code = "INVALID_JSON_SCHEMA"
    default_message = "自定义 JSON Schema 不合法"


# ---------------------------- 大模型 ----------------------------
class LLMError(AppError):
    code = "LLM_ERROR"
    http_status = status.HTTP_502_BAD_GATEWAY
    default_message = "大模型调用失败"


class LLMTimeoutError(LLMError):
    code = "LLM_TIMEOUT"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    default_message = "大模型调用超时"


class LLMRateLimitError(LLMError):
    code = "LLM_RATE_LIMITED"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    default_message = "大模型接口限流，请稍后重试"


class LLMInvalidJSONError(LLMError):
    code = "LLM_INVALID_JSON"
    default_message = "大模型返回的内容不是合法 JSON"


class LLMOutputTruncatedError(LLMError):
    """输出被 max_tokens 截断。

    deepseek-flash 是推理模型，思考 token 与正文共享 max_tokens 预算，
    文档较复杂时思考可能吃掉大部分额度。见 docs/troubleshooting。
    """

    code = "LLM_OUTPUT_TRUNCATED"
    default_message = "大模型输出被 max_tokens 截断，请调大 LLM_MAX_TOKENS"


class LLMUpstreamError(LLMError):
    code = "LLM_UPSTREAM_ERROR"
    default_message = "大模型服务返回错误"


class TaskTimeoutError(AppError):
    code = "TASK_TIMEOUT"
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    default_message = "任务处理超时"


class TaskInterruptedError(AppError):
    """服务重启导致进行中的任务被中断。可重试。"""

    code = "SERVICE_RESTARTED"
    http_status = status.HTTP_409_CONFLICT
    default_message = "服务重启导致任务中断，请重新抽取"


# ============================================================================
# 统一响应构造
# ============================================================================
def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": get_request_id(),
            }
        },
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """把各类异常统一转换成同一种响应结构。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        # 5xx 说明是服务端问题，需要带堆栈；4xx 是调用方问题，记 warning 即可
        if exc.http_status >= 500:
            logger.error(
                "app_error",
                code=exc.code,
                message=exc.message,
                details=exc.details,
                path=request.url.path,
                exc_info=True,
            )
        else:
            logger.warning(
                "app_error",
                code=exc.code,
                message=exc.message,
                details=exc.details,
                path=request.url.path,
            )
        return error_response(
            status_code=exc.http_status,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            headers={"WWW-Authenticate": 'APIKey header="X-API-Key"'}
            if exc.http_status == 401
            else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 把 pydantic 的错误列表压成 {字段: 说明}，前端可以直接标记到表单项上
        field_errors: dict[str, str] = {}
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"] if part != "body")
            field_errors[location or "body"] = error["msg"]

        logger.warning("request_validation_failed", path=request.url.path, fields=field_errors)
        return error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="VALIDATION_ERROR",
            message="请求参数校验失败",
            details={"fields": field_errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            413: "FILE_TOO_LARGE",
        }.get(exc.status_code, f"HTTP_{exc.status_code}")

        # RFC 9110 要求 401 响应必须带 WWW-Authenticate，客户端据此知道该用哪种凭证。
        # 缺了它，一些 HTTP 客户端与网关不会把凭证补上，表现为"明明配了 key 还是 401"。
        headers = (
            {"WWW-Authenticate": 'APIKey header="X-API-Key"'} if exc.status_code == 401 else None
        )

        return error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            details={},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：任何未预期的异常都不能把堆栈泄露给调用方，但必须完整记进日志
        logger.error(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_ERROR",
            message="服务内部错误，请稍后重试或联系管理员",
        )
