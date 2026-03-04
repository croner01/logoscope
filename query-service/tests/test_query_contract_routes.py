"""
Query Service 接口契约测试（QS-04）

覆盖范围：
1) logs / logs-aggregated 拓扑上下文参数
2) trace-lite 过滤行为
3) value-kpi 看板与周报导出返回结构
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_routes, data_quality


def _business_calls(storage: "FakeContractStorageAdapter") -> List[Dict[str, Any]]:
    """过滤掉 preagg 探测产生的 system.tables 查询。"""
    return [
        call
        for call in storage.calls
        if "FROM system.tables" not in call["query"]
    ]


def _first_business_call(storage: "FakeContractStorageAdapter") -> Dict[str, Any]:
    calls = _business_calls(storage)
    assert calls, "expected at least one business query call"
    return calls[0]


class FakeContractStorageAdapter:
    """契约测试存储桩。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        params = params or {}
        self.calls.append({"query": condensed, "params": params})

        if "FROM logs.logs" in condensed and "id, timestamp, service_name, namespace, message, trace_id, attributes_json" in condensed:
            # trace-lite/value-kpi inference coverage logs 查询
            base = datetime(2026, 2, 27, 10, 0, 0, tzinfo=timezone.utc)
            return [
                {
                    "id": "t1",
                    "timestamp": base,
                    "service_name": "legacy-gateway",
                    "namespace": "prod",
                    "message": "request_id=req-1 start",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-1\"}",
                },
                {
                    "id": "t2",
                    "timestamp": base + timedelta(seconds=1),
                    "service_name": "legacy-order",
                    "namespace": "prod",
                    "message": "request_id=req-1 done",
                    "trace_id": "",
                    "attributes_json": "{\"request_id\":\"req-1\"}",
                },
            ]

        if "FROM logs.metrics" in condensed and "value_float64 as value" in condensed:
            return [
                {
                    "timestamp": "2026-02-27 10:00:00.000",
                    "service_name": "checkout",
                    "metric_name": "http.server.duration",
                    "value": 23.4,
                    "labels": "{\"method\":\"GET\"}",
                }
            ]

        if "FROM logs.logs" in condensed and "GROUP BY service_name" in condensed and "count() AS count" in condensed:
            return [
                {"value": "checkout", "count": 12},
                {"value": "payment", "count": 8},
            ]

        if "FROM logs.logs" in condensed and "level_norm AS value" in condensed and "count() AS count" in condensed:
            return [
                {"value": "ERROR", "count": 5},
                {"value": "WARN", "count": 4},
                {"value": "INFO", "count": 3},
            ]

        if "FROM logs.logs" in condensed and "id," in condensed and "ORDER BY timestamp DESC" in condensed:
            # logs / logs-aggregated 查询
            return [
                {
                    "id": "l1",
                    "timestamp": "2026-02-27 10:00:00.000",
                    "service_name": "checkout",
                    "level": "ERROR",
                    "message": "checkout call payment timeout",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "cid-1",
                    "container_image": "checkout:v1",
                    "pod_id": "pid-1",
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "labels": "{\"app\":\"checkout\"}",
                    "attributes_json": "{\"peer.service\":\"payment\"}",
                    "host_ip": "10.0.0.1",
                }
            ]

        if "CREATE TABLE IF NOT EXISTS logs.value_kpi_snapshots" in condensed:
            return []

        if "count() AS total_logs" in condensed and "AS correlated_logs" in condensed and "FROM logs.logs" in condensed:
            return [{"total_logs": 100, "correlated_logs": 35}]

        if "AS total_services" in condensed and "service_name" in condensed:
            return [{"total_services": 10}]

        if "AS traced_services" in condensed and "service_name" in condensed:
            return [{"traced_services": 4}]

        if "FROM logs.logs" in condensed and "level_norm AS level" in condensed:
            base = datetime(2026, 2, 27, 0, 0, 0, tzinfo=timezone.utc)
            return [
                {
                    "timestamp": base,
                    "service_name": "query-service",
                    "level": "ERROR",
                    "message": "timeout",
                },
                {
                    "timestamp": base + timedelta(minutes=30),
                    "service_name": "query-service",
                    "level": "INFO",
                    "message": "resolved",
                },
            ]

        if "FROM logs.release_gate_reports" in condensed and "countIf(status = 'passed')" in condensed:
            return [{
                "total": 6,
                "passed": 5,
                "failed": 1,
                "bypassed": 0,
                "release_total": 6,
                "release_passed": 5,
                "release_failed": 1,
                "drill_total": 0,
                "trace_smoke_failed": 1,
                "ai_contract_failed": 0,
                "query_contract_failed": 1,
            }]

        if "FROM logs.release_gate_reports" in condensed and "ORDER BY started_at DESC" in condensed:
            return [{
                "gate_id": "gate-1",
                "started_at": "2026-02-27 00:00:00.000",
                "finished_at": "2026-02-27 00:01:00.000",
                "status": "passed",
                "candidate": "m4-rc",
                "tag": "m4-test",
                "target": "query-service",
                "trace_id": "trace-gate-1",
                "smoke_exit_code": 1,
                "trace_smoke_exit_code": 1,
                "ai_contract_exit_code": 0,
                "query_contract_exit_code": 1,
                "report_path": "/tmp/gate-report.json",
                "summary": "ok",
            }]

        if "AS child INNER JOIN (" in condensed and "FROM logs.traces PREWHERE timestamp > now() - INTERVAL" in condensed:
            return [
                {"source_service": "legacy-gateway", "target_service": "legacy-order"},
            ]

        return []


@pytest.fixture(autouse=True)
def reset_state():
    query_routes.set_storage_adapter(None)
    query_routes._INFERENCE_ALERT_SUPPRESSIONS.clear()
    query_routes._VALUE_KPI_ALERT_SUPPRESSIONS.clear()
    query_routes._TRACE_COLUMNS_CACHE = None
    yield
    query_routes.set_storage_adapter(None)
    query_routes._INFERENCE_ALERT_SUPPRESSIONS.clear()
    query_routes._VALUE_KPI_ALERT_SUPPRESSIONS.clear()
    query_routes._TRACE_COLUMNS_CACHE = None


@pytest.mark.asyncio
async def test_logs_explicit_filters_override_topology_context():
    """显式 service/search/start_time 应覆盖拓扑上下文回填。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_logs(
        limit=20,
        service_name="explicit-service",
        trace_id=None,
        pod_name=None,
        level="ERROR",
        start_time="2026-02-27T10:00:00Z",
        end_time="2026-02-27T11:00:00Z",
        exclude_health_check=False,
        search="explicit-keyword",
        source_service="checkout",
        target_service="payment",
        time_window="15 MINUTE",
    )

    query_call = _first_business_call(storage)
    assert "timestamp > now() - INTERVAL" not in query_call["query"]
    assert query_call["params"]["service_name"] == "explicit-service"
    assert query_call["params"]["search"] == "explicit-keyword"
    assert "start_time" in query_call["params"]
    assert "end_time" in query_call["params"]
    assert result["context"]["effective_service_name"] == "explicit-service"
    assert result["context"]["effective_search"] == "explicit-keyword"


@pytest.mark.asyncio
async def test_logs_support_multi_value_filters():
    """logs 查询应支持 service_names/levels 多值过滤。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_logs(
        limit=20,
        service_name=None,
        service_names=["checkout", "payment"],
        trace_id=None,
        pod_name=None,
        level=None,
        levels=["ERROR", "WARN"],
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
    )

    query_call = _first_business_call(storage)
    assert "service_name = {service_name_0:String}" in query_call["query"]
    assert "service_name = {service_name_1:String}" in query_call["query"]
    assert "level_norm = {level_0:String}" in query_call["query"]
    assert "level_norm = {level_1:String}" in query_call["query"]
    assert query_call["params"]["service_name_0"] == "checkout"
    assert query_call["params"]["service_name_1"] == "payment"
    assert query_call["params"]["level_0"] == "ERROR"
    assert query_call["params"]["level_1"] == "WARN"
    assert result["context"]["effective_service_names"] == ["checkout", "payment"]
    assert result["context"]["effective_levels"] == ["ERROR", "WARN"]


@pytest.mark.asyncio
async def test_logs_facets_disjunctive_contract():
    """logs facets 应对 service/level 维度采用 disjunctive 统计。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_logs_facets(
        service_name=None,
        service_names=["checkout"],
        trace_id=None,
        pod_name=None,
        level=None,
        levels=["ERROR"],
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search="timeout",
        source_service=None,
        target_service=None,
        time_window=None,
        limit_services=10,
        limit_levels=10,
    )

    business_calls = _business_calls(storage)
    assert len(business_calls) == 2
    service_query = business_calls[0]["query"]
    level_query = business_calls[1]["query"]

    assert "GROUP BY service_name" in service_query
    assert "level_norm = {facet_level:String}" in service_query
    assert "service_name = {facet_service:String}" not in service_query

    assert "GROUP BY value" in level_query
    assert "service_name = {facet_service:String}" in level_query
    assert "level = {facet_level:String}" not in level_query

    assert result["services"][0]["value"] == "checkout"
    assert result["levels"][0]["value"] == "ERROR"


@pytest.mark.asyncio
async def test_logs_aggregated_accepts_topology_context_params():
    """聚合日志查询应支持 source/target/time_window 上下文参数。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    await query_routes.query_logs_aggregated(
        limit=50,
        min_pattern_count=1,
        max_patterns=10,
        max_samples=2,
        service_name=None,
        trace_id=None,
        pod_name=None,
        level=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="payment",
        time_window="15 MINUTE",
    )

    query_call = _first_business_call(storage)
    assert "timestamp > now() - INTERVAL 15 MINUTE" in query_call["query"]
    assert query_call["params"]["service_name"] == "checkout"
    assert query_call["params"]["search"] == "payment"


@pytest.mark.asyncio
async def test_metrics_query_uses_parameterized_filters():
    """/metrics 查询应使用参数化条件，避免用户输入直拼 SQL。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    await query_routes.query_metrics(
        limit=50,
        service_name="checkout'; DROP TABLE logs.metrics --",
        metric_name="http.server.duration",
        start_time="2026-02-27T10:00:00Z",
        end_time="2026-02-27T11:00:00Z",
    )

    query_call = _first_business_call(storage)
    query_text = query_call["query"]
    params = query_call["params"]

    assert "service_name = {service_name:String}" in query_text
    assert "metric_name = {metric_name:String}" in query_text
    assert "toDateTime64({start_time:String}, 9)" in query_text
    assert "toDateTime64({end_time:String}, 9)" in query_text
    assert "LIMIT {limit:Int32}" in query_text
    assert "DROP TABLE" not in query_text

    assert params["service_name"] == "checkout'; DROP TABLE logs.metrics --"
    assert params["metric_name"] == "http.server.duration"
    assert params["start_time"] == "2026-02-27 10:00:00.000"
    assert params["end_time"] == "2026-02-27 11:00:00.000"
    assert params["limit"] == 50


@pytest.mark.asyncio
async def test_logs_query_end_time_only_backfills_default_window():
    """logs 查询仅传 end_time 时应自动补默认窗口下界，避免无下界扫描。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    await query_routes.query_logs(
        limit=20,
        service_name=None,
        trace_id=None,
        pod_name=None,
        level=None,
        start_time=None,
        end_time="2026-02-27T11:00:00Z",
        exclude_health_check=False,
        search=None,
        source_service=None,
        target_service=None,
        time_window=None,
    )

    query_call = _first_business_call(storage)
    assert "timestamp <= toDateTime64({end_time:String}, 9)" in query_call["query"]
    assert "timestamp > toDateTime64({end_time:String}, 9) - INTERVAL 24 HOUR" in query_call["query"]


@pytest.mark.asyncio
async def test_topology_edge_preview_rejects_blank_source():
    """链路预览接口应拒绝空 source_service。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    with pytest.raises(HTTPException) as exc_info:
        await query_routes.query_topology_edge_logs_preview(
            source_service="",
            target_service="payment",
            time_window="1 HOUR",
            limit=5,
            exclude_health_check=False,
        )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_trace_lite_filters_contract():
    """trace-lite source/target 过滤应生效。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    matched = await query_routes.query_trace_lite_inferred(
        time_window="1 HOUR",
        source_service="legacy-gateway",
        target_service="legacy-order",
        namespace=None,
        limit=20,
    )
    assert matched["count"] >= 1

    unmatched = await query_routes.query_trace_lite_inferred(
        time_window="1 HOUR",
        source_service="legacy-gateway",
        target_service="unknown-target",
        namespace=None,
        limit=20,
    )
    assert unmatched["count"] == 0


@pytest.mark.asyncio
async def test_value_kpi_dashboard_contract_fields():
    """value-kpi 看板接口应返回固定 contract 字段。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.value_kpi_dashboard(time_window="7 DAY")

    assert result["status"] == "ok"
    assert "metrics" in result
    assert "incident_summary" in result
    assert "release_gate_summary" in result
    assert "generated_at" in result
    metrics = result["metrics"]
    for key in (
        "mttd_minutes",
        "mttr_minutes",
        "trace_log_correlation_rate",
        "topology_coverage_rate",
        "release_regression_pass_rate",
    ):
        assert key in metrics
    gate = result["release_gate_summary"]
    for key in (
        "trace_smoke_failed",
        "ai_contract_failed",
        "query_contract_failed",
        "trace_smoke_pass_rate",
        "ai_contract_pass_rate",
        "query_contract_pass_rate",
    ):
        assert key in gate
    assert gate["trace_smoke_failed"] == 1
    assert gate["query_contract_failed"] == 1
    assert gate["last_result"]["trace_smoke_exit_code"] == 1
    assert gate["last_result"]["query_contract_exit_code"] == 1


@pytest.mark.asyncio
async def test_value_kpi_weekly_export_contract_headers():
    """value-kpi 周报导出应返回 CSV 且包含约定表头。"""
    storage = FakeContractStorageAdapter()
    query_routes.set_storage_adapter(storage)

    response = await query_routes.value_kpi_weekly_export(weeks=2)
    csv_text = response.body.decode("utf-8")
    lines = [line for line in csv_text.splitlines() if line.strip()]

    assert response.media_type.startswith("text/csv")
    assert len(lines) >= 3  # header + 2 weeks
    header = lines[0].split(",")
    assert header[:5] == [
        "week_index",
        "week_start_utc",
        "week_end_utc",
        "mttd_minutes",
        "mttr_minutes",
    ]


def test_metrics_routes_contract_no_conflict():
    """QS-03: /metrics 仅保留业务查询，性能指标使用 /metrics/performance。"""
    get_paths = [
        route.path
        for route in query_routes.router.routes
        if "GET" in getattr(route, "methods", set())
    ]

    assert get_paths.count("/api/v1/metrics") == 1


def test_data_quality_router_contract_paths():
    """QS-04: data quality 路由应保持固定前缀与关键路径。"""
    get_paths = [
        route.path
        for route in data_quality.router.routes
        if "GET" in getattr(route, "methods", set())
    ]
    post_paths = [
        route.path
        for route in data_quality.router.routes
        if "POST" in getattr(route, "methods", set())
    ]

    assert "/api/v1/quality/overview" in get_paths
    assert "/api/v1/quality/unknown/analysis" in get_paths
    assert "/api/v1/quality/service/distribution" in get_paths
    assert "/api/v1/quality/unknown/reprocess" in post_paths


@pytest.mark.asyncio
async def test_performance_metrics_endpoint_contract():
    """QS-03: 性能指标接口应可调用并返回最小契约字段。"""
    result = await query_routes.get_performance_metrics()

    assert "timestamp" in result
    assert (
        ("performance" in result and "requests" in result)
        or ("error" in result)
    )
