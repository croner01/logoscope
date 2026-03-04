"""Tests for extracted observability query service helpers."""

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_observability_service as obs_service


def _reset_preagg_cache() -> None:
    os.environ["PREAGG_SCHEMA_VERSION"] = "auto"
    obs_service._PREAGG_TABLE_CACHE["expires_at"] = 0.0
    obs_service._PREAGG_TABLE_CACHE["tables"] = set()


class FakeStorageAdapter:
    """Storage stub for observability service tests."""

    def __init__(
        self,
        traces_rows: Optional[List[Dict[str, Any]]] = None,
        spans_rows: Optional[List[Dict[str, Any]]] = None,
        preagg_tables: Optional[List[str]] = None,
    ):
        self.traces_rows = traces_rows or []
        self.spans_rows = spans_rows or []
        self.preagg_tables = set(preagg_tables or [])
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        self.calls.append({"query": condensed, "params": params or {}})

        if "FROM system.tables" in condensed and "database = 'logs'" in condensed:
            return [{"name": name} for name in sorted(self.preagg_tables)]

        if "FROM logs.obs_counts_1m" in condensed and "signal = 'log'" in condensed and "sum(count) AS total" in condensed:
            return [{"total": 33}]
        if "FROM logs.obs_counts_1m" in condensed and "signal = 'log'" in condensed and "GROUP BY service_name" in condensed:
            return [{"service_name": "checkout", "count": 20}]
        if (
            "FROM logs.obs_counts_1m" in condensed
            and "signal = 'log'" in condensed
            and "AS level" in condensed
        ):
            return [{"level": "ERROR", "count": 4}, {"level": "OTHER", "count": 1}]

        if "FROM logs.obs_counts_1m" in condensed and "signal = 'metric'" in condensed and "sum(count) AS total" in condensed:
            return [{"total": 44}]
        if "FROM logs.obs_counts_1m" in condensed and "signal = 'metric'" in condensed and "GROUP BY service_name" in condensed:
            return [{"service_name": "checkout", "count": 30}]
        if "FROM logs.obs_counts_1m" in condensed and "signal = 'metric'" in condensed and "GROUP BY dim_value" in condensed:
            return [{"metric_name": "http.server.duration", "count": 18}]

        if "FROM logs.obs_traces_1m" in condensed and "sumMerge(span_count_state) AS span_count" in condensed:
            return [{"span_count": 55, "trace_count": 2, "error_trace_count": 1}]
        if "FROM logs.obs_traces_1m" in condensed and "GROUP BY service_name" in condensed:
            return [{"service_name": "checkout", "count": 12}]
        if "FROM logs.obs_traces_1m" in condensed and "GROUP BY operation_name" in condensed:
            return [{"operation_name": "GET /checkout", "count": 25}]

        if "FROM logs.metrics" in condensed and "value_float64 as value" in condensed:
            return [
                {
                    "timestamp": "2026-03-01 10:00:00.000",
                    "service_name": "checkout",
                    "metric_name": "http.server.duration",
                    "value": 23.4,
                    "labels": "{}",
                }
            ]
        if "SELECT COUNT(*) as total FROM logs.metrics" in condensed:
            return [{"total": 10}]
        if "FROM logs.metrics" in condensed and "GROUP BY service_name" in condensed:
            return [{"service_name": "checkout", "count": 6}]
        if "FROM logs.metrics" in condensed and "GROUP BY metric_name" in condensed:
            return [{"metric_name": "http.server.duration", "count": 6}]

        if "FROM logs.traces" in condensed and "GROUP BY t.trace_id" in condensed and "argMin(t.operation_name" in condensed:
            return self.traces_rows

        if "FROM logs.traces" in condensed and "PREWHERE trace_id = {trace_id:String}" in condensed:
            return self.spans_rows

        if "uniqCombined64(trace_id)" in condensed and "count() AS span_count" in condensed:
            return [{"total_traces": 2, "span_count": 4}]
        if "FROM logs.traces" in condensed and "GROUP BY service_name" in condensed:
            return [{"service_name": "checkout", "count": 2}]
        if "FROM logs.traces" in condensed and "GROUP BY operation_name" in condensed:
            return [{"operation_name": "GET /checkout", "count": 4}]
        if "avg(trace_duration) AS avg_duration" in condensed:
            return [{"avg_duration": 120.2, "p99_duration": 980.9}]
        if "uniqCombined64If(trace_id" in condensed:
            return [{"error_traces": 1, "total_traces": 2}]

        if "SELECT COUNT(*) as total FROM logs.logs" in condensed:
            return [{"total": 20}]
        if "FROM logs.logs" in condensed and "GROUP BY service_name" in condensed:
            return [{"service_name": "checkout", "count": 12}]
        if "FROM logs.logs" in condensed and "AS level" in condensed:
            return [{"level": "ERROR", "count": 3}]

        return []


def _resolve_trace_schema() -> Dict[str, Optional[str]]:
    return {
        "time_col": "timestamp",
        "attrs_col": "attributes_json",
        "duration_col": "duration_ms",
    }


def test_query_metrics_and_stats_contract():
    _reset_preagg_cache()
    storage = FakeStorageAdapter()

    metrics = obs_service.query_metrics(
        storage_adapter=storage,
        limit=50,
        service_name="checkout'; DROP TABLE logs.metrics --",
        metric_name="http.server.duration",
        start_time="2026-03-01T00:00:00Z",
        end_time="2026-03-01T01:00:00Z",
        convert_timestamp_fn=lambda ts: ts.replace("T", " ").replace("Z", ".000"),
    )
    assert metrics["count"] == 1
    query_call = storage.calls[0]
    assert "service_name = {service_name:String}" in query_call["query"]
    assert "LIMIT {limit:Int32}" in query_call["query"]
    assert "DROP TABLE" not in query_call["query"]

    stats = obs_service.query_metrics_stats(storage_adapter=storage)
    assert stats["total"] == 10
    assert stats["byService"]["checkout"] == 6


def test_query_metrics_end_time_only_backfills_lower_bound_window():
    _reset_preagg_cache()
    storage = FakeStorageAdapter()

    obs_service.query_metrics(
        storage_adapter=storage,
        limit=50,
        service_name=None,
        metric_name=None,
        start_time=None,
        end_time="2026-03-01T01:00:00Z",
        convert_timestamp_fn=lambda ts: ts.replace("T", " ").replace("Z", ".000"),
    )

    query_call = storage.calls[0]
    assert "timestamp <= toDateTime64({end_time:String}, 9)" in query_call["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9) - INTERVAL 24 HOUR" in query_call["query"]


def test_query_traces_and_spans_contract():
    storage = FakeStorageAdapter(
        traces_rows=[
            {
                "trace_id": "trace-1",
                "service_name": "checkout",
                "operation_name": "GET /checkout",
                "start_time_str": "2026-03-01 00:00:00.000",
                "duration_ms": 100,
                "status": "2",
            }
        ],
        spans_rows=[
            {
                "trace_id": "trace-1",
                "span_id": "span-1",
                "parent_span_id": "",
                "service_name": "checkout",
                "operation_name": "GET /checkout",
                "start_time": "2026-03-01 00:00:00.000",
                "status": "STATUS_CODE_OK",
                "duration_ms": 120.0,
                "attrs_payload": '{"http.method":"GET"}',
            }
        ],
    )

    traces = obs_service.query_traces(
        storage_adapter=storage,
        limit=10,
        service_name="checkout",
        trace_id="trace-1",
        start_time=None,
        end_time=None,
        resolve_trace_schema_fn=_resolve_trace_schema,
        build_grouped_trace_duration_expr_fn=lambda _schema: "max(toFloat64OrZero(duration_ms))",
        normalize_trace_status_fn=lambda value: "STATUS_CODE_ERROR" if str(value) in {"2", "ERROR"} else "STATUS_CODE_OK",
        convert_timestamp_fn=lambda value: value,
    )
    assert traces["count"] == 1
    assert traces["data"][0]["status"] == "STATUS_CODE_ERROR"

    spans = obs_service.query_trace_spans(
        storage_adapter=storage,
        trace_id="trace-1",
        limit=100,
        resolve_trace_schema_fn=_resolve_trace_schema,
        parse_json_dict_fn=lambda raw: {"http.method": "GET"} if raw else {},
        extract_duration_ms_fn=lambda row, _tags: float(row.get("duration_ms") or 0.0),
        normalize_trace_status_fn=lambda status: str(status),
    )
    assert len(spans) == 1
    assert spans[0]["tags"]["http.method"] == "GET"


def test_query_traces_end_time_only_backfills_lower_bound_window():
    _reset_preagg_cache()
    storage = FakeStorageAdapter(traces_rows=[])

    obs_service.query_traces(
        storage_adapter=storage,
        limit=10,
        service_name=None,
        trace_id=None,
        start_time=None,
        end_time="2026-03-01T01:00:00Z",
        resolve_trace_schema_fn=_resolve_trace_schema,
        build_grouped_trace_duration_expr_fn=lambda _schema: "max(toFloat64OrZero(duration_ms))",
        normalize_trace_status_fn=lambda value: str(value),
        convert_timestamp_fn=lambda value: value.replace("T", " ").replace("Z", ".000") if value else value,
    )

    trace_query = next(call for call in storage.calls if "FROM logs.traces" in call["query"])
    assert "timestamp <= toDateTime64({end_time:String}, 9)" in trace_query["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9) - INTERVAL 24 HOUR" in trace_query["query"]


def test_query_logs_stats_and_traces_stats():
    _reset_preagg_cache()
    storage = FakeStorageAdapter()

    logs_stats = obs_service.query_logs_stats(storage)
    assert logs_stats["total"] == 20
    assert logs_stats["byService"]["checkout"] == 12

    traces_stats = obs_service.query_traces_stats(
        storage_adapter=storage,
        resolve_trace_schema_fn=_resolve_trace_schema,
        build_grouped_trace_duration_expr_fn=lambda _schema: "max(toFloat64OrZero(duration_ms))",
    )
    assert traces_stats["total"] == 2
    assert traces_stats["spanCount"] == 4
    assert traces_stats["avg_duration"] == 120.2
    assert traces_stats["p99_duration"] == 980.9
    assert traces_stats["error_rate"] == 0.5


def test_query_traces_stats_end_time_only_backfills_lower_bound_window():
    _reset_preagg_cache()
    storage = FakeStorageAdapter()

    obs_service.query_traces_stats(
        storage_adapter=storage,
        resolve_trace_schema_fn=_resolve_trace_schema,
        build_grouped_trace_duration_expr_fn=lambda _schema: "max(toFloat64OrZero(duration_ms))",
        start_time=None,
        end_time="2026-03-01T01:00:00Z",
        convert_timestamp_fn=lambda value: value.replace("T", " ").replace("Z", ".000") if value else value,
    )

    total_query = next(call for call in storage.calls if "uniqCombined64(trace_id)" in call["query"])
    assert "timestamp <= toDateTime64({stats_end:String}, 9)" in total_query["query"]
    assert "timestamp > toDateTime64({stats_end:String}, 9) - INTERVAL 24 HOUR" in total_query["query"]


def test_query_traces_rejects_non_whitelisted_time_column():
    _reset_preagg_cache()
    storage = FakeStorageAdapter(traces_rows=[])

    try:
        obs_service.query_traces(
            storage_adapter=storage,
            limit=5,
            service_name=None,
            trace_id=None,
            start_time=None,
            end_time=None,
            resolve_trace_schema_fn=lambda: {
                "time_col": "timestamp DESC; DROP TABLE logs.traces --",
                "attrs_col": "attributes_json",
                "duration_col": "duration_ms",
            },
            build_grouped_trace_duration_expr_fn=lambda _schema: "max(toFloat64OrZero(duration_ms))",
            normalize_trace_status_fn=lambda value: str(value),
            convert_timestamp_fn=lambda value: value,
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "timestamp/start_time" in str(exc)


def test_query_stats_preaggregated_paths_preferred_when_tables_exist():
    _reset_preagg_cache()
    storage = FakeStorageAdapter(
        preagg_tables=["obs_counts_1m", "obs_traces_1m"],
    )

    logs_stats = obs_service.query_logs_stats(storage)
    assert logs_stats["total"] == 33
    assert logs_stats["byService"]["checkout"] == 20
    assert logs_stats["byLevel"]["ERROR"] == 4
    assert logs_stats["byLevel"]["OTHER"] == 1
    assert any(
        "FROM logs.obs_counts_1m" in call["query"]
        and "AS level" in call["query"]
        for call in storage.calls
    )

    metrics_stats = obs_service.query_metrics_stats(storage)
    assert metrics_stats["total"] == 44
    assert metrics_stats["byService"]["checkout"] == 30
    assert metrics_stats["byMetricName"]["http.server.duration"] == 18

    traces_stats = obs_service.query_traces_stats(
        storage_adapter=storage,
        resolve_trace_schema_fn=_resolve_trace_schema,
        build_grouped_trace_duration_expr_fn=lambda _schema: "max(toFloat64OrZero(duration_ms))",
    )
    assert traces_stats["total"] == 2
    assert traces_stats["spanCount"] == 55
    assert traces_stats["byService"]["checkout"] == 12
    assert traces_stats["byOperation"]["GET /checkout"] == 25
