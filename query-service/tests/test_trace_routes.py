"""
Query Service trace 路由单元测试
"""
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_observability_service, query_routes


class FakeStorageAdapter:
    """用于 trace 路由测试的轻量存储桩。"""

    def __init__(
        self,
        columns: List[str],
        traces_rows: Optional[List[Dict[str, Any]]] = None,
        spans_rows: Optional[List[Dict[str, Any]]] = None,
        trace_stats: Optional[Dict[str, Any]] = None,
    ):
        self.columns = columns
        self.traces_rows = traces_rows or []
        self.spans_rows = spans_rows or []
        self.trace_stats = trace_stats or {}
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        self.calls.append({"query": condensed, "params": params or {}})

        if "FROM system.columns" in condensed:
            return [{"name": name} for name in self.columns]

        if "FROM logs.traces" in condensed and "GROUP BY t.trace_id" in condensed and "argMin(t.operation_name" in condensed:
            return self.traces_rows

        if "FROM logs.traces" in condensed and "ORDER BY" in condensed and "LIMIT {limit:Int32}" in query:
            return self.spans_rows

        if "FROM logs.traces" in condensed and "uniqCombined64" in condensed and "span_count" in condensed:
            return [{
                "total_traces": self.trace_stats.get("total_traces", 0),
                "span_count": self.trace_stats.get("span_count", 0),
            }]

        if "GROUP BY service_name" in condensed:
            return self.trace_stats.get("by_service_rows", [])

        if "GROUP BY operation_name" in condensed:
            return self.trace_stats.get("by_operation_rows", [])

        if "avg(trace_duration) AS avg_duration" in condensed:
            return [{
                "avg_duration": self.trace_stats.get("avg_duration", 0.0),
                "p99_duration": self.trace_stats.get("p99_duration", 0.0),
            }]

        if "uniqCombined64If(trace_id" in condensed:
            return [{
                "error_traces": self.trace_stats.get("error_traces", 0),
                "total_traces": self.trace_stats.get("total_traces", 0),
            }]

        return []


@pytest.fixture(autouse=True)
def reset_trace_cache():
    """每个测试用例前重置 schema cache。"""
    query_routes._TRACE_COLUMNS_CACHE = None
    query_observability_service._PREAGG_TABLE_CACHE["expires_at"] = 0.0
    query_observability_service._PREAGG_TABLE_CACHE["tables"] = set()
    yield
    query_routes._TRACE_COLUMNS_CACHE = None
    query_observability_service._PREAGG_TABLE_CACHE["expires_at"] = 0.0
    query_observability_service._PREAGG_TABLE_CACHE["tables"] = set()
    query_routes.set_storage_adapter(None)


@pytest.mark.asyncio
async def test_query_trace_spans_fallback_duration_ns():
    """无 duration_ms 列时，从 attrs 中的 duration_ns 回退计算毫秒。"""
    storage = FakeStorageAdapter(
        columns=["timestamp", "trace_id", "span_id", "status", "tags"],
        spans_rows=[{
            "trace_id": "trace-1",
            "span_id": "span-1",
            "parent_span_id": "",
            "service_name": "checkout",
            "operation_name": "GET /checkout",
            "start_time": "2026-02-26 01:02:03.000",
            "status": "2",
            "duration_ms": 0.0,
            "attrs_payload": "{\"duration_ns\": 2500000, \"http.method\": \"GET\"}",
        }],
    )
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_trace_spans("trace-1", limit=100)

    assert len(result) == 1
    assert result[0]["trace_id"] == "trace-1"
    assert result[0]["duration_ms"] == 2.5
    assert result[0]["status"] == "STATUS_CODE_ERROR"
    assert result[0]["tags"]["http.method"] == "GET"


@pytest.mark.asyncio
async def test_query_trace_spans_uses_duration_ns_column_when_present():
    """存在 duration_ns 列时，SQL 应转换为毫秒后返回。"""
    storage = FakeStorageAdapter(
        columns=["timestamp", "trace_id", "span_id", "status", "attributes_json", "duration_ns"],
        spans_rows=[{
            "trace_id": "trace-2",
            "span_id": "span-2",
            "parent_span_id": "",
            "service_name": "checkout",
            "operation_name": "POST /checkout",
            "start_time": "2026-02-26 01:02:03.000",
            "status": "1",
            "duration_ms": 7.5,
            "attrs_payload": "{}",
        }],
    )
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_trace_spans("trace-2", limit=100)

    assert len(result) == 1
    assert result[0]["duration_ms"] == 7.5
    span_query = next(call for call in storage.calls if "FROM logs.traces" in call["query"] and "ORDER BY" in call["query"])
    assert "toFloat64OrZero(toString(duration_ns)) / 1000000.0" in span_query["query"]


@pytest.mark.asyncio
async def test_query_trace_spans_infers_timeline_duration_when_all_zero():
    """当 span 时长全为 0 时，按时间线顺序推断近似时长。"""
    storage = FakeStorageAdapter(
        columns=["timestamp", "trace_id", "span_id", "status", "attributes_json", "duration_ms"],
        spans_rows=[
            {
                "trace_id": "trace-3",
                "span_id": "span-a",
                "parent_span_id": "",
                "service_name": "frontend",
                "operation_name": "GET /api",
                "start_time": "2026-03-04 10:00:00.000",
                "status": "1",
                "duration_ms": 0.0,
                "attrs_payload": "{}",
            },
            {
                "trace_id": "trace-3",
                "span_id": "span-b",
                "parent_span_id": "span-a",
                "service_name": "query-service",
                "operation_name": "query_traces",
                "start_time": "2026-03-04 10:00:00.040",
                "status": "1",
                "duration_ms": 0.0,
                "attrs_payload": "{}",
            },
        ],
    )
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_trace_spans("trace-3", limit=100)

    assert len(result) == 2
    assert result[0]["duration_ms"] == 40.0
    assert result[1]["duration_ms"] == 0.0


def test_parse_start_time_epoch_ms_supports_numeric_epoch():
    """start_time 为纯数字 epoch 时也应可解析（秒/毫秒/微秒/纳秒）。"""
    parse_fn = query_observability_service._parse_start_time_epoch_ms
    assert parse_fn("1709546400") == 1709546400000.0
    assert parse_fn("1709546400123") == 1709546400123.0
    assert parse_fn("1709546400123456") == pytest.approx(1709546400123.456)
    assert parse_fn("1709546400123456789") == pytest.approx(1709546400123.4568)


def test_build_grouped_trace_duration_expr_converts_duration_ns_to_ms():
    """trace 聚合时长表达式应兼容 duration_ns 列换算。"""
    expr = query_routes._build_grouped_trace_duration_expr({
        "time_col": "timestamp",
        "attrs_col": "attributes_json",
        "duration_col": "duration_ns",
    })
    assert "toFloat64OrZero(toString(duration_ns)) / 1000000.0" in expr


@pytest.mark.asyncio
async def test_query_traces_aggregates_and_normalizes_status():
    """/traces 应按 trace 聚合并标准化状态。"""
    storage = FakeStorageAdapter(
        columns=["timestamp", "trace_id", "status", "attributes_json"],
        traces_rows=[{
            "trace_id": "trace-2",
            "service_name": "order",
            "operation_name": "POST /orders",
            "start_time_str": "2026-02-26 02:00:00.000",
            "duration_ms": 123,
            "status": "2",
        }],
    )
    query_routes.set_storage_adapter(storage)

    response = await query_routes.query_traces(
        limit=10,
        service_name="order",
        trace_id="trace-2",
        start_time="2026-02-26T02:00:00Z",
        end_time="2026-02-26T02:10:00Z",
    )

    assert response["count"] == 1
    assert response["data"][0]["trace_id"] == "trace-2"
    assert response["data"][0]["status"] == "STATUS_CODE_ERROR"

    trace_query_call = next(
        call for call in storage.calls if "argMin(t.operation_name" in call["query"]
    )
    assert trace_query_call["params"]["service_name"] == "order"
    assert trace_query_call["params"]["trace_id"] == "trace-2"
    assert "start_time" in trace_query_call["params"]
    assert "end_time" in trace_query_call["params"]


@pytest.mark.asyncio
async def test_query_traces_stats_returns_datadog_like_summary_fields():
    """/traces/stats 应返回 total/avg/p99/error_rate 等关键字段。"""
    storage = FakeStorageAdapter(
        columns=["timestamp", "trace_id", "duration_ms", "status", "attributes_json"],
        trace_stats={
            "total_traces": 5,
            "span_count": 20,
            "avg_duration": 120.25,
            "p99_duration": 980.5,
            "error_traces": 1,
            "by_service_rows": [{"service_name": "checkout", "count": 3}],
            "by_operation_rows": [{"operation_name": "GET /checkout", "count": 4}],
        },
    )
    query_routes.set_storage_adapter(storage)

    stats = await query_routes.query_traces_stats()

    assert stats["total"] == 5
    assert stats["spanCount"] == 20
    assert stats["avg_duration"] == 120.25
    assert stats["p99_duration"] == 980.5
    assert stats["error_rate"] == 0.2
    assert stats["byService"]["checkout"] == 3
    assert stats["byOperation"]["GET /checkout"] == 4
