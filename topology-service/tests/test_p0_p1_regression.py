"""
P0/P1 回归测试（topology-service）

覆盖：
1) 统一错误模型（code/message/request_id + X-Request-ID）
2) SQL 时间窗口防注入（INTERVAL 安全化）
3) 聚合查询日志采样策略
"""
import json
import os
import sys

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import Response

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as topology_main
from storage import adapter as topology_storage_adapter


def _build_request(
    *,
    path: str = "/test",
    headers: dict = None,
    request_id: str = None,
) -> Request:
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
    request = Request(scope)
    if request_id is not None:
        request.state.request_id = request_id
    return request


@pytest.mark.asyncio
async def test_request_id_middleware_propagates_header():
    request = _build_request(headers={"X-Request-ID": "rid-topology-001"})

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain")

    response = await topology_main.request_id_middleware(request, _call_next)
    assert request.state.request_id == "rid-topology-001"
    assert response.headers.get("X-Request-ID") == "rid-topology-001"


@pytest.mark.asyncio
async def test_http_exception_handler_hides_500_detail():
    request = _build_request(request_id="rid-topology-500")
    response = await topology_main.http_exception_handler(
        request,
        HTTPException(status_code=500, detail="clickhouse credential leaked"),
    )
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 500
    assert payload["code"] == "INTERNAL_SERVER_ERROR"
    assert payload["message"] == "Internal server error"
    assert payload["detail"] == "Internal server error"
    assert payload["request_id"] == "rid-topology-500"


@pytest.mark.asyncio
async def test_validation_exception_handler_returns_standard_shape():
    request = _build_request(request_id="rid-topology-422")
    exc = RequestValidationError(
        [
            {
                "loc": ("query", "time_window"),
                "msg": "Field required",
                "type": "missing",
                "input": None,
            }
        ]
    )
    response = await topology_main.validation_exception_handler(request, exc)
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 422
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["message"] == "Request validation failed"
    assert payload["request_id"] == "rid-topology-422"
    assert isinstance(payload["detail"], list)


def test_sanitize_interval_blocks_injection_payload():
    safe = topology_storage_adapter._sanitize_interval(
        "1 HOUR; DROP TABLE logs.traces --",
        default_value="1 HOUR",
    )
    assert safe == "1 HOUR"
    assert "DROP" not in safe

    normalized = topology_storage_adapter._sanitize_interval("30 minute", default_value="1 HOUR")
    assert normalized == "30 MINUTE"


def test_aggregation_query_logging_respects_sampling(monkeypatch: pytest.MonkeyPatch):
    aggregation_sql = "SELECT source, count() FROM logs.traces GROUP BY source"
    non_aggregation_sql = "SELECT trace_id FROM logs.traces LIMIT 1"

    monkeypatch.setattr(topology_storage_adapter, "AGG_QUERY_LOG_SAMPLE_RATE", 0.2, raising=False)
    monkeypatch.setattr(topology_storage_adapter.random, "random", lambda: 0.95)
    assert topology_storage_adapter._should_log_query_info(aggregation_sql) is False

    monkeypatch.setattr(topology_storage_adapter.random, "random", lambda: 0.05)
    assert topology_storage_adapter._should_log_query_info(aggregation_sql) is True

    monkeypatch.setattr(topology_storage_adapter.random, "random", lambda: 0.99)
    assert topology_storage_adapter._should_log_query_info(non_aggregation_sql) is True
