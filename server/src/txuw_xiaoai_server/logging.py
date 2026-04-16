from __future__ import annotations

import logging
import logging.config
import sys
from datetime import UTC, datetime
from typing import Any

_DEFAULT_CONTEXT: dict[str, str] = {
    "connectionId": "-",
    "direction": "-",
    "frameType": "-",
    "messageType": "-",
    "event": "-",
    "instructionName": "-",
    "payloadKind": "-",
    "tag": "-",
    "messageId": "-",
    "command": "-",
    "code": "-",
    "payloadError": "-",
    "rawPayload": "-",
    "rawPreview": "-",
    "summary": "-",
    "byteLength": "-",
    "status": "-",
    "errorType": "-",
}

_RESERVED_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
    # uvicorn/starlette internal fields
    "color_message",
    "websocket",
    "asgi_scope",
    "http_version",
    "method",
    "path",
    "query_string",
    "root_path",
    "scheme",
    "server",
    "client",
    "headers",
    "type",
}

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: "\033[36m",     # cyan
    logging.INFO: "\033[32m",      # green
    logging.WARNING: "\033[33m",   # yellow
    logging.ERROR: "\033[31m",     # red
    logging.CRITICAL: "\033[35m",  # magenta
}
_RESET = "\033[0m"
_DIM = "\033[2m"


class ContextFilter(logging.Filter):
    """为每条日志注入默认上下文字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, default in _DEFAULT_CONTEXT.items():
            val = getattr(record, key, None)
            setattr(record, key, _stringify(val if val is not None else default))
        return True


class KeyValueFormatter(logging.Formatter):
    """结构化键值对日志格式，带颜色输出。"""

    def __init__(self, *, use_color: bool = True) -> None:
        super().__init__()
        self._use_color = use_color and _supports_color()

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds",
        )
        task_name = getattr(record, "taskName", None) or getattr(
            record, "threadName", "MainThread",
        )

        if self._use_color:
            level_color = _LEVEL_COLORS.get(record.levelno, "")
            prefix = (
                f"{_DIM}{timestamp}{_RESET}  "
                f"{level_color}{record.levelname:<5}{_RESET} "
                f"{_DIM}{record.process}{_RESET} --- "
                f"[{_DIM}{task_name}{_RESET}] "
                f"{level_color}{record.name:<36}{_RESET} : {message}"
            )
        else:
            prefix = (
                f"{timestamp}  {record.levelname:<5} "
                f"{record.process} --- [{task_name}] "
                f"{record.name:<36} : {message}"
            )

        extras = self._build_extras(record)
        if extras:
            extra_str = " ".join(extras)
            if self._use_color:
                prefix = f"{prefix} {_DIM}{extra_str}{_RESET}"
            else:
                prefix = f"{prefix} {extra_str}"

        if record.exc_info:
            prefix = f"{prefix}\n{self.formatException(record.exc_info)}"
        return prefix

    def _build_extras(self, record: logging.LogRecord) -> list[str]:
        values: list[str] = []
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            rendered = _stringify(value)
            if rendered in {"", "-"}:
                continue
            values.append(f"{key}={rendered}")
        values.sort()
        return values


def build_log_config(log_level: str, *, access_log: bool = True) -> dict[str, Any]:
    level = log_level.upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "context": {
                "()": "txuw_xiaoai_server.logging.ContextFilter",
            },
        },
        "formatters": {
            "structured": {
                "()": "txuw_xiaoai_server.logging.KeyValueFormatter",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "structured",
                "filters": ["context"],
            },
        },
        "root": {
            "level": level,
            "handlers": ["default"],
        },
        "loggers": {
            "uvicorn": {"level": "INFO", "handlers": ["default"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": ["default"], "propagate": False},
            "uvicorn.access": {
                "level": "INFO" if access_log else "CRITICAL",
                "handlers": ["default"],
                "propagate": False,
            },
            "websockets": {"level": "INFO", "handlers": ["default"], "propagate": False},
            "websockets.server": {"level": "INFO", "handlers": ["default"], "propagate": False},
        },
    }


def configure_logging(log_level: str, *, access_log: bool = True) -> None:
    logging.config.dictConfig(build_log_config(log_level, access_log=access_log))


def _stringify(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.0f}" if value.is_integer() else f"{value:.3f}"
    return str(value)


def _supports_color() -> bool:
    """检测终端是否支持 ANSI 颜色。"""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # Windows 10+ 支持 ANSI 转义码
            return kernel32.GetConsoleMode(kernel32.GetStdHandle(-11)) & 0x0004 != 0
        except Exception:
            return False
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
