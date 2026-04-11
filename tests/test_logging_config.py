"""
日志配置模块回归测试。
"""
import logging

from shared_src.utils.logging_config import HealthEndpointFilter


def _build_uvicorn_access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %s',
        args=("127.0.0.1:1111", "GET", path, "1.1", 200),
        exc_info=None,
    )


def _build_uvicorn_access_message_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_health_endpoint_filter_suppresses_health_path():
    filter_obj = HealthEndpointFilter()
    record = _build_uvicorn_access_record("/health")

    assert filter_obj.filter(record) is False


def test_health_endpoint_filter_keeps_non_health_path_with_health_query_word():
    filter_obj = HealthEndpointFilter()
    record = _build_uvicorn_access_record("/api/v1/search?redirect=/health")

    assert filter_obj.filter(record) is True


def test_health_endpoint_filter_suppresses_health_path_for_non_get_methods():
    filter_obj = HealthEndpointFilter()
    record = _build_uvicorn_access_message_record(
        '127.0.0.1:1111 - "POST /health HTTP/1.1" 200'
    )

    assert filter_obj.filter(record) is False


def test_health_endpoint_filter_suppresses_health_path_for_http2_message():
    filter_obj = HealthEndpointFilter()
    record = _build_uvicorn_access_message_record(
        '127.0.0.1:1111 - "GET /health HTTP/2" 200'
    )

    assert filter_obj.filter(record) is False
