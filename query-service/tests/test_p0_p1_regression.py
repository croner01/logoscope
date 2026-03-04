"""
P0/P1 回归测试（query-service）

覆盖：
1) 统一错误模型（code/message/request_id + X-Request-ID）
2) 降级路径（storage 未就绪时 health mode=degraded）
3) SQL 时间窗口防注入（INTERVAL 安全化）
4) 聚合查询日志采样策略
5) pre-agg 运行态健康信息透出
"""
import json
import os
import sys

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import Response

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as query_main
from storage import adapter as query_storage_adapter


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
    request = _build_request(headers={"X-Request-ID": "rid-query-001"})

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain")

    response = await query_main.request_id_middleware(request, _call_next)
    assert request.state.request_id == "rid-query-001"
    assert response.headers.get("X-Request-ID") == "rid-query-001"


@pytest.mark.asyncio
async def test_request_id_middleware_generates_when_missing():
    request = _build_request()

    async def _call_next(_request: Request) -> Response:
        return Response(content="ok", media_type="text/plain")

    response = await query_main.request_id_middleware(request, _call_next)
    generated = response.headers.get("X-Request-ID")
    assert generated
    assert len(generated) == 32
    assert request.state.request_id == generated


@pytest.mark.asyncio
async def test_http_exception_handler_hides_500_detail():
    request = _build_request(request_id="rid-query-500")
    response = await query_main.http_exception_handler(
        request,
        HTTPException(status_code=500, detail="db password leaked"),
    )
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 500
    assert payload["code"] == "INTERNAL_SERVER_ERROR"
    assert payload["message"] == "Internal server error"
    assert payload["detail"] == "Internal server error"
    assert payload["request_id"] == "rid-query-500"


@pytest.mark.asyncio
async def test_validation_exception_handler_returns_standard_shape():
    request = _build_request(request_id="rid-query-422")
    exc = RequestValidationError(
        [
            {
                "loc": ("query", "limit"),
                "msg": "Input should be greater than 0",
                "type": "value_error",
                "input": 0,
            }
        ]
    )
    response = await query_main.validation_exception_handler(request, exc)
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 422
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["message"] == "Request validation failed"
    assert payload["request_id"] == "rid-query-422"
    assert isinstance(payload["detail"], list)


@pytest.mark.asyncio
async def test_health_check_degraded_mode_when_storage_unready():
    original_storage = query_main._storage_adapter
    original_preagg_refresh_fn = query_main.query_routes.refresh_preagg_runtime_status
    original_preagg_status_fn = query_main.query_routes.get_preagg_runtime_status
    refresh_calls = {"count": 0}

    def _refresh_stub(force_reload: bool = False):
        refresh_calls["count"] += 1
        return {
            "ready": False,
            "missing": ["obs_counts_1m", "obs_traces_1m"],
        }

    try:
        query_main.query_routes.refresh_preagg_runtime_status = _refresh_stub
        query_main.query_routes.get_preagg_runtime_status = lambda: _refresh_stub()
        query_main._storage_adapter = None
        degraded = await query_main.health_check()
        assert degraded["mode"] == "degraded"
        assert degraded["storage_connected"] is False
        assert degraded["preagg_ready"] is False
        assert degraded["warnings"] == []

        query_main._storage_adapter = object()
        normal = await query_main.health_check()
        assert normal["mode"] == "normal"
        assert normal["storage_connected"] is True
        assert normal["preagg_ready"] is False
        assert "preagg_tables_missing" in normal["warnings"]
        assert refresh_calls["count"] >= 2
    finally:
        query_main.query_routes.refresh_preagg_runtime_status = original_preagg_refresh_fn
        query_main.query_routes.get_preagg_runtime_status = original_preagg_status_fn
        query_main._storage_adapter = original_storage


@pytest.mark.asyncio
async def test_health_check_includes_preagg_snapshot_when_ready():
    original_storage = query_main._storage_adapter
    original_preagg_refresh_fn = query_main.query_routes.refresh_preagg_runtime_status
    original_preagg_status_fn = query_main.query_routes.get_preagg_runtime_status
    refresh_calls = {"count": 0}

    def _ready_snapshot(force_reload: bool = False):
        refresh_calls["count"] += 1
        return {
            "checked_at": "2026-03-02T12:00:00+00:00",
            "storage_connected": True,
            "expected": ["obs_counts_1m", "obs_traces_1m"],
            "available": ["obs_counts_1m", "obs_traces_1m"],
            "missing": [],
            "ready": True,
            "error": None,
        }

    try:
        query_main._storage_adapter = object()
        query_main.query_routes.refresh_preagg_runtime_status = _ready_snapshot
        query_main.query_routes.get_preagg_runtime_status = lambda: _ready_snapshot()
        payload = await query_main.health_check()
        assert payload["storage_connected"] is True
        assert payload["preagg_ready"] is True
        assert payload["preagg"]["ready"] is True
        assert payload["preagg"]["missing"] == []
        assert payload["warnings"] == []
        assert refresh_calls["count"] >= 1
    finally:
        query_main.query_routes.refresh_preagg_runtime_status = original_preagg_refresh_fn
        query_main.query_routes.get_preagg_runtime_status = original_preagg_status_fn
        query_main._storage_adapter = original_storage


def test_sanitize_interval_blocks_injection_payload():
    safe = query_storage_adapter._sanitize_interval(
        "1 HOUR; DROP TABLE logs.logs --",
        default_value="7 DAY",
    )
    assert safe == "7 DAY"
    assert "DROP" not in safe

    normalized = query_storage_adapter._sanitize_interval("15 minutes", default_value="7 DAY")
    assert normalized == "15 MINUTE"


def test_aggregation_query_logging_respects_sampling(monkeypatch: pytest.MonkeyPatch):
    aggregation_sql = "SELECT service_name, count() FROM logs.logs GROUP BY service_name"
    non_aggregation_sql = "SELECT id FROM logs.logs LIMIT 1"

    monkeypatch.setattr(query_storage_adapter, "AGG_QUERY_LOG_SAMPLE_RATE", 0.2, raising=False)
    monkeypatch.setattr(query_storage_adapter.random, "random", lambda: 0.95)
    assert query_storage_adapter._should_log_query_info(aggregation_sql) is False

    monkeypatch.setattr(query_storage_adapter.random, "random", lambda: 0.05)
    assert query_storage_adapter._should_log_query_info(aggregation_sql) is True

    monkeypatch.setattr(query_storage_adapter.random, "random", lambda: 0.99)
    assert query_storage_adapter._should_log_query_info(non_aggregation_sql) is True
