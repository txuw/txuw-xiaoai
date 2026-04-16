from __future__ import annotations

import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

_DEFAULT_CONTEXT: dict[str, str] = {
    "connectionId": "-",
    "event": "-",
    "tag": "-",
    "messageId": "-",
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
}


class ContextFilter(logging.Filter):
    """为每条日志注入默认上下文字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, default in _DEFAULT_CONTEXT.items():
            val = getattr(record, key, None)
            setattr(record, key, _stringify(val if val is not None else default))
        return True


class KeyValueFormatter(logging.Formatter):
    """结构化键值对日志格式：ISO时间戳 级别 PID --- [线程] 模块 : 消息 k=v ..."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds",
        )
        task_name = getattr(record, "taskName", None) or getattr(
            record, "threadName", "MainThread",
        )
        prefix = (
            f"{timestamp}  {record.levelname:<5} "
            f"{record.process} --- [{task_name}] "
            f"{record.name:<36} : {message}"
        )
        extras = self._build_extras(record)
        if extras:
            prefix = f"{prefix} {' '.join(extras)}"
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
                "formatter": "structured",
                "filters": ["context"],
            },
        },
        "root": {
            "level": level,
            "handlers": ["default"],
        },
        "loggers": {
            "uvicorn": {"level": level, "handlers": ["default"], "propagate": False},
            "uvicorn.error": {"level": level, "handlers": ["default"], "propagate": False},
            "uvicorn.access": {
                "level": "INFO" if access_log else "CRITICAL",
                "handlers": ["default"],
                "propagate": False,
            },
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
