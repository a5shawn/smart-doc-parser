"""结构化日志配置。

本地开发渲染成带颜色的可读文本，生产环境渲染成一行一个 JSON 对象（便于采集）。
第三方库（uvicorn、sqlalchemy、openai）的日志也统一走同一套渲染管线，
避免出现"一半 JSON 一半纯文本"的混合输出。
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import settings

# 这些库在 INFO 级别过于聒噪，压到 WARNING
_NOISY_LOGGERS = {
    "uvicorn.access": logging.WARNING,  # 我们有自己的访问日志中间件
    "uvicorn.error": logging.INFO,
    "sqlalchemy.engine": logging.WARNING,
    "sqlalchemy.pool": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "openai": logging.WARNING,
    "python_multipart": logging.WARNING,
    "asyncio": logging.WARNING,
}


def configure_logging() -> None:
    """初始化日志。应在进程启动时调用一次（见 app/main.py 的 lifespan）。"""
    level = getattr(logging, settings.LOG_LEVEL)

    # 所有日志共用的前置处理器：注入 request_id、级别、时间戳等
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_JSON:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer(
            ensure_ascii=False,  # 中文日志保持可读，而不是 \uXXXX
        )
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            # 交给 ProcessorFormatter 决定最终渲染，这样 structlog 与 stdlib 日志
            # 能共用同一个 renderer，输出格式完全一致
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # 非 structlog 的日志（第三方库）先经过这些处理器再渲染
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name, logger_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(max(logger_level, level))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取一个绑定了模块名的 logger。"""
    return structlog.get_logger(name)
