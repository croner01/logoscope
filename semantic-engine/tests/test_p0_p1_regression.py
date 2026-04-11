"""
P0/P1 回归测试（semantic-engine）

覆盖：
1) 统一错误模型（code/message/request_id + X-Request-ID）
2) SQL 时间窗口防注入（INTERVAL 安全化）
3) 聚合查询日志采样策略
4) worker 停止时同步 close() 降级路径
"""
import json
import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import Response

# 添加 semantic-engine 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as semantic_main
from msgqueue.worker import LogWorker
from storage import adapter as semantic_storage_adapter


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
    request = _build_request(headers={"X-Request-ID": "rid-semantic-001"})

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain")

    response = await semantic_main.request_id_middleware(request, _call_next)
    assert request.state.request_id == "rid-semantic-001"
    assert response.headers.get("X-Request-ID") == "rid-semantic-001"


@pytest.mark.asyncio
async def test_http_exception_handler_hides_500_detail():
    request = _build_request(request_id="rid-semantic-500")
    response = await semantic_main.http_exception_handler(
        request,
        HTTPException(status_code=500, detail="internal stack leaked"),
    )
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 500
    assert payload["code"] == "INTERNAL_SERVER_ERROR"
    assert payload["message"] == "Internal server error"
    assert payload["detail"] == "Internal server error"
    assert payload["request_id"] == "rid-semantic-500"


@pytest.mark.asyncio
async def test_validation_exception_handler_returns_standard_shape():
    request = _build_request(request_id="rid-semantic-422")
    exc = RequestValidationError(
        [
            {
                "loc": ("body", "trace_id"),
                "msg": "Field required",
                "type": "missing",
                "input": None,
            }
        ]
    )
    response = await semantic_main.validation_exception_handler(request, exc)
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 422
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["message"] == "Request validation failed"
    assert payload["request_id"] == "rid-semantic-422"
    assert isinstance(payload["detail"], list)


def test_sanitize_interval_blocks_injection_payload():
    safe = semantic_storage_adapter._sanitize_interval(
        "1 HOUR; DROP TABLE logs.logs --",
        default_value="1 HOUR",
    )
    assert safe == "1 HOUR"
    assert "DROP" not in safe

    normalized = semantic_storage_adapter._sanitize_interval("20 MINUTE", default_value="1 HOUR")
    assert normalized == "20 MINUTE"


def test_aggregation_query_logging_respects_sampling(monkeypatch: pytest.MonkeyPatch):
    aggregation_sql = "SELECT service_name, count() FROM logs.logs GROUP BY service_name"
    non_aggregation_sql = "SELECT id FROM logs.logs LIMIT 1"

    monkeypatch.setattr(semantic_storage_adapter, "AGG_QUERY_LOG_SAMPLE_RATE", 0.2, raising=False)
    monkeypatch.setattr(semantic_storage_adapter.random, "random", lambda: 0.95)
    assert semantic_storage_adapter._should_log_query_info(aggregation_sql) is False

    monkeypatch.setattr(semantic_storage_adapter.random, "random", lambda: 0.05)
    assert semantic_storage_adapter._should_log_query_info(aggregation_sql) is True

    monkeypatch.setattr(semantic_storage_adapter.random, "random", lambda: 0.99)
    assert semantic_storage_adapter._should_log_query_info(non_aggregation_sql) is True


@pytest.mark.asyncio
async def test_worker_stop_closes_async_queue_and_sync_storage():
    worker = LogWorker()
    worker.running = True
    worker.queue = Mock()
    worker.queue.close = AsyncMock()
    worker.storage = Mock()
    worker.storage.close = Mock()

    await worker.stop()

    worker.queue.close.assert_awaited_once()
    worker.storage.close.assert_called_once()
    assert worker.running is False


@pytest.mark.asyncio
async def test_worker_stop_is_idempotent_when_called_twice():
    worker = LogWorker()
    worker.running = True
    worker.queue = Mock()
    worker.queue.close = AsyncMock()
    worker.storage = Mock()
    worker.storage.close = Mock()
    worker.log_writer = Mock()
    worker.log_writer.stop = Mock()
    worker.log_writer.get_stats = Mock(return_value={"buffer_size": 0, "total_rows": 0})

    await worker.stop()
    await worker.stop()

    worker.queue.close.assert_awaited_once()
    worker.storage.close.assert_called_once()
    worker.log_writer.stop.assert_called_once()
    assert worker.running is False


@pytest.mark.asyncio
async def test_worker_stop_continues_when_queue_close_raises():
    worker = LogWorker()
    worker.running = True
    worker.queue = Mock()
    worker.queue.close = AsyncMock(side_effect=RuntimeError("queue close failed"))
    worker.storage = Mock()
    worker.storage.close = Mock()
    worker.log_writer = Mock()
    worker.log_writer.stop = Mock()
    worker.log_writer.get_stats = Mock(return_value={"buffer_size": 0, "total_rows": 0})

    await worker.stop()

    worker.queue.close.assert_awaited_once()
    worker.storage.close.assert_called_once()
    worker.log_writer.stop.assert_called_once()
    assert worker.running is False
