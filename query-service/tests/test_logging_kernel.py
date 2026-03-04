"""
日志内核回归测试。
"""
import json
import logging
import os
import sys
from typing import List

import pytest
from starlette.requests import Request
from starlette.responses import Response

_QUERY_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_QUERY_SERVICE_DIR)
_SHARED_SRC_DIR = os.path.join(_REPO_ROOT, "shared_src")
if _SHARED_SRC_DIR not in sys.path:
    sys.path.insert(0, _SHARED_SRC_DIR)

import platform_kernel.fastapi_kernel as fastapi_kernel
import utils.logging_config as logging_config
from platform_kernel.fastapi_kernel import create_request_id_middleware
from utils.logging_config import (
    HealthEndpointFilter,
    RequestContextFilter,
    StructuredFormatter,
    TextFormatter,
    clear_request_context,
    set_request_context,
)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _build_request(path: str, headers: dict | None = None) -> Request:
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((str(key).lower().encode("utf-8"), str(value).encode("utf-8")))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_structured_formatter_includes_core_context_fields():
    clear_request_context()
    set_request_context(
        request_id="rid-log-001",
        trace_id="trace-001",
        span_id="span-001",
        method="GET",
        path="/api/v1/logs",
    )
    formatter = StructuredFormatter(service_name="query-service")
    logger = logging.getLogger("test.logging.structured")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    capture = _CaptureHandler()
    capture.setFormatter(formatter)
    logger.addHandler(capture)

    logger.info(
        "query executed",
        extra={
            "event_id": "CH_QUERY_EXECUTED",
            "action": "clickhouse.query",
            "outcome": "success",
            "duration_ms": 12.5,
            "rows": 3,
        },
    )

    rendered = capture.format(capture.records[-1])
    payload = json.loads(rendered)
    assert payload["request_id"] == "rid-log-001"
    assert payload["trace_id"] == "trace-001"
    assert payload["span_id"] == "span-001"
    assert payload["event_id"] == "CH_QUERY_EXECUTED"
    assert payload["action"] == "clickhouse.query"
    assert payload["outcome"] == "success"
    assert payload["duration_ms"] == 12.5
    assert payload["context"]["rows"] == 3
    clear_request_context()


def test_text_formatter_hides_empty_request_context_placeholders():
    clear_request_context()
    formatter = TextFormatter(service_name="query-service")
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)
    assert "req--" not in rendered
    assert "trace=-" not in rendered
    assert "span=-" not in rendered
    assert "uvicorn: Application startup complete." in rendered


def test_text_formatter_normalizes_uvicorn_error_logger_name():
    clear_request_context()
    formatter = TextFormatter(service_name="query-service")
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Uvicorn running on http://0.0.0.0:8080",
        args=(),
        exc_info=None,
    )
    rendered = formatter.format(record)
    assert "uvicorn.error:" not in rendered
    assert "uvicorn: Uvicorn running on http://0.0.0.0:8080" in rendered


def test_structured_formatter_normalizes_uvicorn_error_logger_name():
    clear_request_context()
    formatter = StructuredFormatter(service_name="query-service")
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["logger"] == "uvicorn"


def test_health_endpoint_filter_suppresses_uvicorn_access_health_logs():
    filter_obj = HealthEndpointFilter()
    health_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %s',
        args=("127.0.0.1:1111", "GET", "/health", "1.1", 200),
        exc_info=None,
    )
    normal_record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=2,
        msg='%s - "%s %s HTTP/%s" %s',
        args=("127.0.0.1:1111", "GET", "/api/v1/logs", "1.1", 200),
        exc_info=None,
    )

    assert filter_obj.filter(health_record) is False
    assert filter_obj.filter(normal_record) is True


@pytest.mark.asyncio
async def test_request_middleware_skips_health_check_summary_log():
    logger = logging.getLogger("test.logging.middleware.health")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    capture = _CaptureHandler()
    logger.addHandler(capture)

    middleware = create_request_id_middleware(logger=logger)
    request = _build_request("/health", headers={"X-Request-ID": "rid-health-001"})

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain", status_code=200)

    response = await middleware(request, _call_next)
    assert response.headers.get("X-Request-ID") == "rid-health-001"
    assert len(capture.records) == 0


@pytest.mark.asyncio
async def test_request_middleware_logs_non_health_summary():
    logger = logging.getLogger("test.logging.middleware.query")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    capture = _CaptureHandler()
    logger.addHandler(capture)

    middleware = create_request_id_middleware(logger=logger)
    request = _build_request("/api/v1/logs", headers={"X-Request-ID": "rid-query-logs-001"})

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain", status_code=200)

    response = await middleware(request, _call_next)
    assert response.headers.get("X-Request-ID") == "rid-query-logs-001"
    assert len(capture.records) == 1
    record = capture.records[0]
    assert record.event_id == "http.request"
    assert record.path == "/api/v1/logs"
    assert record.outcome == "success"


def _install_fake_otel_context(monkeypatch, trace_id_hex: str, span_id_hex: str):
    class _SpanContext:
        def __init__(self):
            self.trace_id = int(trace_id_hex, 16)
            self.span_id = int(span_id_hex, 16)
            self.is_valid = True

    class _Span:
        def get_span_context(self):
            return _SpanContext()

    class _TraceApi:
        @staticmethod
        def get_current_span():
            return _Span()

    class _Otel:
        trace = _TraceApi()

    monkeypatch.setitem(sys.modules, "opentelemetry", _Otel)
    logging_config._load_otel_trace_api.cache_clear()


def test_request_context_filter_backfills_trace_span_from_otel(monkeypatch):
    clear_request_context()
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    span_id = "00f067aa0ba902b7"
    _install_fake_otel_context(monkeypatch, trace_id, span_id)

    record = logging.LogRecord(
        name="test.logging.otel",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="otel ctx test",
        args=(),
        exc_info=None,
    )

    assert RequestContextFilter().filter(record) is True
    assert getattr(record, "trace_id") == trace_id
    assert getattr(record, "span_id") == span_id
    assert logging_config.get_request_context().get("trace_id") == trace_id
    assert logging_config.get_request_context().get("span_id") == span_id
    clear_request_context()
    logging_config._load_otel_trace_api.cache_clear()


@pytest.mark.asyncio
async def test_request_middleware_uses_otel_context_when_headers_missing(monkeypatch):
    logger = logging.getLogger("test.logging.middleware.otel")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    capture = _CaptureHandler()
    logger.addHandler(capture)

    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    span_id = "00f067aa0ba902b7"
    monkeypatch.setattr(fastapi_kernel, "_extract_trace_span_from_otel_context", lambda: (trace_id, span_id))

    middleware = create_request_id_middleware(logger=logger)
    request = _build_request("/api/v1/logs")

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain", status_code=200)

    response = await middleware(request, _call_next)
    assert response.headers.get("X-Request-ID")
    assert len(capture.records) == 1
    record = capture.records[0]
    assert record.trace_id == trace_id
    assert record.span_id == span_id
