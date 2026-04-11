"""
日志工具模块

提供统一的日志配置和 request_id 上下文传播。
所有 service 模块通过 get_logger() 获取 logger，日志自动附带 request_id。
"""
import contextvars
import logging
import os

# --- Request ID 上下文变量 ---
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_LOG_INITIALIZED = False


class _RequestIdFilter(logging.Filter):
    """在日志 record 中注入当前 request_id。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get("-")  # type: ignore[attr-defined]
        return True


def setup_basic_logging() -> None:
    global _LOG_INITIALIZED
    if _LOG_INITIALIZED:
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] [req:%(request_id)s] %(message)s",
    )
    # 为 root logger 添加 request_id filter
    for handler in logging.root.handlers:
        handler.addFilter(_RequestIdFilter())
    _LOG_INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    setup_basic_logging()
    return logging.getLogger(name)
