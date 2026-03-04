"""
结构化日志配置模块。

支持通过环境变量控制日志格式:
- LOG_FORMAT=text (默认): 输出 OpenStack 风格文本日志
- LOG_FORMAT=json: 输出结构化 JSON 日志
- LOG_LEVEL: 控制日志级别 (DEBUG, INFO, WARN, ERROR)
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, Optional


HEALTH_CHECK_PATHS = (
    "/health",
    "/healthz",
    "/ready",
    "/readiness",
    "/live",
    "/liveness",
)

_HEALTH_ACCESS_PATTERN = re.compile(r'"(?:GET|HEAD)\s+([^"\s]+)\s+HTTP/\d\.\d"', re.IGNORECASE)
_REQUEST_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "logoscope_request_context",
    default=None,
)

_CORE_CONTEXT_FIELDS = (
    "request_id",
    "trace_id",
    "span_id",
    "event_id",
    "action",
    "outcome",
    "duration_ms",
    "method",
    "path",
    "status_code",
    "client_ip",
)

_RESERVED_LOG_RECORD_FIELDS = {
    "name",
    "msg",
    "args",
    "created",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "exc_info",
    "exc_text",
    "thread",
    "threadName",
    "message",
    "asctime",
    "color_message",
}


@lru_cache(maxsize=1)
def _load_otel_trace_api() -> Any:
    """Lazy-load OpenTelemetry trace API. Returns None when unavailable."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore

        return otel_trace
    except Exception:
        return None


def _extract_otel_trace_context() -> Dict[str, str]:
    """Extract current active OpenTelemetry trace/span ids in lowercase hex."""
    otel_trace = _load_otel_trace_api()
    if otel_trace is None:
        return {}

    try:
        span = otel_trace.get_current_span()
        if span is None:
            return {}
        span_context = span.get_span_context()
        if not span_context or not getattr(span_context, "is_valid", False):
            return {}

        trace_id_value = int(getattr(span_context, "trace_id", 0) or 0)
        span_id_value = int(getattr(span_context, "span_id", 0) or 0)
        trace_id = f"{trace_id_value:032x}" if trace_id_value > 0 else ""
        span_id = f"{span_id_value:016x}" if span_id_value > 0 else ""

        context: Dict[str, str] = {}
        if trace_id:
            context["trace_id"] = trace_id
        if span_id:
            context["span_id"] = span_id
        return context
    except Exception:
        return {}


def _display_logger_name(name: str) -> str:
    """Normalize noisy logger names for human-facing text output."""
    raw = str(name or "").strip()
    if raw == "uvicorn.error":
        return "uvicorn"
    return raw


def _utc_iso_now() -> str:
    """返回 UTC ISO 8601 时间戳（带 Z 后缀）。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_log_format(raw: str) -> str:
    """规范化日志格式类型，默认返回 text。"""
    value = str(raw or "").strip().lower()
    if value in {"json", "text"}:
        return value
    if value in {"human", "pretty", "plain", "console"}:
        return "text"
    return "text"


def _normalize_path(path: str) -> str:
    """规范化请求路径，去除 query string 与尾部斜杠。"""
    raw = str(path or "").strip()
    if not raw:
        return ""
    raw = raw.split("?", 1)[0]
    if not raw.startswith("/"):
        raw = f"/{raw}"
    if raw != "/":
        raw = raw.rstrip("/")
    return raw.lower()


def is_health_check_path(path: str) -> bool:
    """判断路径是否为健康检查路径。"""
    normalized = _normalize_path(path)
    if not normalized:
        return False
    return any(
        normalized == candidate or normalized.startswith(f"{candidate}/")
        for candidate in HEALTH_CHECK_PATHS
    )


def get_request_context() -> Dict[str, Any]:
    """获取当前请求上下文快照。"""
    context = _REQUEST_CONTEXT.get()
    if not context:
        return {}
    return dict(context)


def set_request_context(**kwargs: Any) -> Dict[str, Any]:
    """设置请求上下文（增量更新）。"""
    context = get_request_context()
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        context[key] = value
    _REQUEST_CONTEXT.set(context)
    return dict(context)


def clear_request_context() -> None:
    """清理请求上下文。"""
    _REQUEST_CONTEXT.set({})


def _extract_uvicorn_access_path(record: logging.LogRecord) -> str:
    """提取 uvicorn.access 日志中的请求路径。"""
    args = getattr(record, "args", None)
    if isinstance(args, tuple) and len(args) >= 3:
        return str(args[2] or "")

    message = record.getMessage()
    match = _HEALTH_ACCESS_PATTERN.search(message)
    if not match:
        return ""
    return match.group(1)


class HealthEndpointFilter(logging.Filter):
    """过滤健康检查访问日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not str(record.name).startswith("uvicorn.access"):
            return True

        path = _extract_uvicorn_access_path(record)
        if path and is_health_check_path(path):
            return False

        lowered_message = record.getMessage().lower()
        return not any(token in lowered_message for token in HEALTH_CHECK_PATHS)


class RequestContextFilter(logging.Filter):
    """将请求上下文字段注入到日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = get_request_context()
        if not context.get("trace_id") or not context.get("span_id"):
            otel_context = _extract_otel_trace_context()
            if otel_context:
                context = set_request_context(**otel_context)

        for key in _CORE_CONTEXT_FIELDS:
            value = context.get(key)
            if value is None:
                continue
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _format_core_context(record: logging.LogRecord) -> Dict[str, Any]:
    """合并 record 与 contextvar 中的核心上下文字段。"""
    context = get_request_context()
    merged: Dict[str, Any] = {}
    for key in _CORE_CONTEXT_FIELDS:
        record_value = getattr(record, key, None)
        value = record_value if record_value is not None else context.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        merged[key] = value

    if "trace_id" not in merged or "span_id" not in merged:
        otel_context = _extract_otel_trace_context()
        if otel_context:
            if "trace_id" not in merged and otel_context.get("trace_id"):
                merged["trace_id"] = otel_context["trace_id"]
            if "span_id" not in merged and otel_context.get("span_id"):
                merged["span_id"] = otel_context["span_id"]
            set_request_context(**otel_context)
    return merged


def _collect_extra_data(record: logging.LogRecord, core_context: Dict[str, Any]) -> Dict[str, Any]:
    """收集非保留字段与非核心上下文字段。"""
    context = get_request_context()
    extra_data: Dict[str, Any] = {}

    for key, value in record.__dict__.items():
        if key in _RESERVED_LOG_RECORD_FIELDS or key in _CORE_CONTEXT_FIELDS:
            continue
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        extra_data[key] = value

    for key, value in context.items():
        if key in core_context or key in extra_data:
            continue
        extra_data[key] = value

    return extra_data


class StructuredFormatter(logging.Formatter):
    """
    结构化 JSON 日志格式化器
    
    输出格式:
    {
        "timestamp": "2024-01-15T10:30:00.123456Z",
        "level": "INFO",
        "service": "query-service",
        "logger": "api.query_routes",
        "message": "Query executed",
        "context": {
            "query_id": "abc123",
            "rows": 100
        }
    }
    """
    
    def __init__(self, service_name: str = "unknown"):
        super().__init__()
        self.service_name = service_name
    
    def format(self, record: logging.LogRecord) -> str:
        core_context = _format_core_context(record)
        log_entry: Dict[str, Any] = {
            "timestamp": _utc_iso_now(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": _display_logger_name(record.name),
            "message": record.getMessage(),
        }

        if core_context:
            log_entry.update(core_context)

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        extra_data = _collect_extra_data(record, core_context)
        if extra_data:
            log_entry["context"] = extra_data

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """
    人类可读的文本日志格式化器
    
    输出格式:
    2024-01-15 10:30:00.123 [INFO] [query-service] api.query_routes: Query executed
    """
    
    def __init__(self, service_name: str = "unknown"):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        core_context = _format_core_context(record)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        request_id = str(core_context.get("request_id", "")).strip()
        trace_id = str(core_context.get("trace_id", "")).strip()
        span_id = str(core_context.get("span_id", "")).strip()
        event_id = str(core_context.get("event_id", "")).strip()
        action = core_context.get("action")
        outcome = core_context.get("outcome")
        duration_ms = core_context.get("duration_ms")

        def _is_present(value: str) -> bool:
            return bool(value and value not in {"-", "none", "None", "null"})

        context_parts = []
        if _is_present(request_id) or _is_present(trace_id) or _is_present(span_id):
            context_parts.append(
                f"[req-{request_id or '-'} trace={trace_id or '-'} span={span_id or '-'}]"
            )
        if _is_present(event_id):
            context_parts.append(f"[{event_id}]")

        context_prefix = f"{' '.join(context_parts)} " if context_parts else ""
        base_msg = (
            f"{timestamp} {record.levelname:5} [{self.service_name}] "
            f"{context_prefix}{_display_logger_name(record.name)}: {record.getMessage()}"
        )

        trailing_parts = []
        if action is not None:
            trailing_parts.append(f"action={action}")
        if outcome is not None:
            trailing_parts.append(f"outcome={outcome}")
        if duration_ms is not None:
            trailing_parts.append(f"duration_ms={duration_ms}")

        extra_data = _collect_extra_data(record, core_context)
        for key, value in extra_data.items():
            trailing_parts.append(f"{key}={value}")

        if trailing_parts:
            base_msg += " | " + " ".join(trailing_parts)

        if record.exc_info:
            base_msg += f"\n{self.formatException(record.exc_info)}"

        return base_msg


@lru_cache(maxsize=1)
def get_log_level() -> int:
    """获取日志级别"""
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    return level_map.get(level_str, logging.INFO)


@lru_cache(maxsize=1)
def get_log_format() -> str:
    """获取日志格式类型"""
    return normalize_log_format(os.getenv("LOG_FORMAT", "text"))


def setup_logging(
    service_name: str,
    level: Optional[int] = None,
    log_format: Optional[str] = None
) -> logging.Logger:
    """
    配置服务的结构化日志
    
    Args:
        service_name: 服务名称 (如 query-service, semantic-engine)
        level: 日志级别，默认从 LOG_LEVEL 环境变量读取
        log_format: 日志格式 (json/text)，默认从 LOG_FORMAT 环境变量读取
    
    Returns:
        logging.Logger: 配置好的根日志器
    
    Example:
        >>> from shared_src.utils.logging_config import setup_logging
        >>> logger = setup_logging("query-service")
        >>> logger.info("Query executed", extra={"query_id": "abc123", "rows": 100})
    """
    if level is None:
        level = get_log_level()
    
    if log_format is None:
        log_format = get_log_format()
    else:
        log_format = normalize_log_format(log_format)
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.addFilter(RequestContextFilter())

    if log_format == "json":
        formatter = StructuredFormatter(service_name)
    else:
        formatter = TextFormatter(service_name)

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    health_filter = HealthEndpointFilter()
    for noisy_name in ("urllib3", "requests"):
        noisy_logger = logging.getLogger(noisy_name)
        noisy_logger.setLevel(logging.WARNING)
    logging.getLogger("clickhouse_driver.connection").setLevel(logging.ERROR)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = False
        uvicorn_logger.setLevel(level)
        uvicorn_logger.addHandler(handler)
        if logger_name == "uvicorn.access":
            uvicorn_logger.addFilter(health_filter)

    return root_logger


def get_logger(name: str, service_name: Optional[str] = None) -> logging.Logger:
    """
    获取带有服务名的日志器
    
    Args:
        name: 日志器名称 (通常是 __name__)
        service_name: 服务名称，可选
    
    Returns:
        logging.Logger: 配置好的日志器
    """
    logger = logging.getLogger(name)
    if service_name and not logger.handlers:
        setup_logging(service_name)
    return logger


class LogContext:
    """
    日志上下文管理器，用于添加额外的上下文信息
    
    Example:
        >>> with LogContext(request_id="abc123", user="admin"):
        ...     logger.info("Processing request")
    """
    @classmethod
    def set(cls, **kwargs):
        """设置上下文变量"""
        set_request_context(**kwargs)

    @classmethod
    def get(cls) -> Dict[str, Any]:
        """获取当前上下文"""
        return get_request_context()

    @classmethod
    def clear(cls):
        """清除上下文"""
        clear_request_context()

    def __init__(self, **kwargs):
        self._new_context = kwargs
        self._token: Optional[contextvars.Token] = None

    def __enter__(self):
        merged = get_request_context()
        merged.update(self._new_context)
        self._token = _REQUEST_CONTEXT.set(merged)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token is not None:
            _REQUEST_CONTEXT.reset(self._token)
        return False


def log_with_context(logger: logging.Logger, level: int, message: str, **kwargs):
    """
    带上下文的日志记录
    
    Args:
        logger: 日志器
        level: 日志级别
        message: 日志消息
        **kwargs: 额外的上下文信息
    """
    context = LogContext.get()
    context.update(kwargs)
    logger.log(level, message, extra=context)
