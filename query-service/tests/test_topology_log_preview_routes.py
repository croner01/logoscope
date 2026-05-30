"""
Query Service 拓扑日志预览与上下文参数测试
"""
import os
import sys
from typing import Any, Dict, List

import pytest

# 添加 query-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import query_routes


def _first_business_call(storage: "FakeStorageAdapter") -> Dict[str, Any]:
    for call in storage.calls:
        if "FROM system.tables" in call["query"]:
            continue
        return call
    raise AssertionError("expected at least one business query call")


class FakeStorageAdapter:
    """拓扑日志预览测试存储桩。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        payload = {"query": condensed, "params": params or {}}
        self.calls.append(payload)

        query_limit = int((params or {}).get("query_limit") or 0)
        if "FROM logs.logs" in condensed and query_limit > 0:
            if "trace_id IN {trace_ids:Array(String)}" in condensed or "request_ids:Array(String)" in condensed:
                return [
                    {
                        "id": "e4",
                        "timestamp": "2026-02-27 10:01:20.000",
                        "service_name": "api-gateway",
                        "level": "ERROR",
                        "message": "forward checkout -> payment failed",
                        "pod_name": "gateway-1",
                        "namespace": "prod",
                        "node_name": "node-3",
                        "container_name": "gateway",
                        "container_id": "c-3",
                        "container_image": "gateway:v1",
                        "pod_id": "p-3",
                        "trace_id": "trace-1",
                        "span_id": "span-4",
                        "labels": '{"app":"gateway"}',
                        "attributes_json": '{"request_id":"req-1","peer.service":"payment"}',
                        "host_ip": "10.0.0.3",
                    }
                ]
            return [
                {
                    "id": "e1",
                    "timestamp": "2026-02-27 10:01:01.000",
                    "service_name": "checkout",
                    "level": "ERROR",
                    "message": "call payment failed: timeout",
                    "pod_name": "checkout-abc",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "labels": '{"app":"checkout"}',
                    "attributes_json": '{"peer.service":"payment","request_id":"req-1"}',
                    "host_ip": "10.0.0.1",
                },
                {
                    "id": "e2",
                    "timestamp": "2026-02-27 10:00:59.000",
                    "service_name": "payment",
                    "level": "WARN",
                    "message": "upstream checkout latency high",
                    "pod_name": "payment-xyz",
                    "namespace": "prod",
                    "node_name": "node-2",
                    "container_name": "payment",
                    "container_id": "c-2",
                    "container_image": "payment:v1",
                    "pod_id": "p-2",
                    "trace_id": "trace-1",
                    "span_id": "span-2",
                    "labels": '{"app":"payment"}',
                    "attributes_json": '{"upstream":"checkout","request_id":"req-1"}',
                    "host_ip": "10.0.0.2",
                },
                {
                    "id": "e3",
                    "timestamp": "2026-02-27 09:59:30.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "prepare payment request",
                    "pod_name": "checkout-abc",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "",
                    "span_id": "span-3",
                    "labels": '{"app":"checkout"}',
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                },
            ]

        if "FROM logs.logs" in condensed:
            return [
                {
                    "id": "l1",
                    "timestamp": "2026-02-27 10:02:00.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "checkout invoke payment",
                    "pod_name": "checkout-abc",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "trace-ctx-1",
                    "span_id": "span-ctx-1",
                    "labels": '{"app":"checkout"}',
                    "attributes_json": '{"peer.service":"payment"}',
                    "host_ip": "10.0.0.1",
                }
            ]
        return []


@pytest.fixture(autouse=True)
def reset_state():
    query_routes.set_storage_adapter(None)
    yield
    query_routes.set_storage_adapter(None)


@pytest.fixture(autouse=True)
def inline_query_routes_run_blocking(monkeypatch: pytest.MonkeyPatch):
    """在单元测试中内联执行阻塞逻辑，避免线程池引入的测试假死。"""

    async def _inline_run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(query_routes, "_run_blocking", _inline_run_blocking)


@pytest.mark.asyncio
async def test_query_topology_edge_logs_preview_returns_ranked_logs():
    """链路日志预览应返回按关联度排序的日志。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_topology_edge_logs_preview(
        source_service="checkout",
        target_service="payment",
        time_window="1 HOUR",
        limit=2,
        exclude_health_check=False,
    )

    assert result["count"] == 2
    assert result["context"]["source_service"] == "checkout"
    assert result["context"]["target_service"] == "payment"
    assert result["context"]["time_window"] == "1 HOUR"
    assert result["data"][0]["id"] == "e1"
    assert result["data"][0]["edge_match_score"] >= result["data"][1]["edge_match_score"]
    assert result["data"][0]["edge_side"] == "source"


@pytest.mark.asyncio
async def test_query_topology_edge_logs_preview_route_omitted_optional_params_do_not_leak_query_objects():
    """直接调用路由函数且省略可选参数时，不应把 Query(None) 对象透传到 service 层。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_topology_edge_logs_preview(
        source_service="checkout",
        target_service="payment",
    )

    query_call = _first_business_call(storage)
    assert result["count"] >= 1
    assert "edge_namespaces" not in query_call["params"]
    assert "namespace" not in query_call["params"]


def test_query_topology_edge_logs_preview_uses_normalized_service_identity_from_pod_name():
    """链路日志预览应支持 service_name 缺失时回退 pod_name 的服务名。"""
    from api.query_logs_service import query_topology_edge_logs_preview

    class PodFallbackStorage:
        def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
            return [
                {
                    "id": "src-pod",
                    "timestamp": "2026-02-27 10:01:00.000",
                    "service_name": "unknown",
                    "level": "ERROR",
                    "message": "issue while calling payment",
                    "pod_name": "checkout-7d9c6f8b5d-abcde",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "",
                    "span_id": "span-1",
                    "labels": '{}',
                    "attributes_json": '{}',
                    "host_ip": "10.0.0.1",
                },
                {
                    "id": "dst-1",
                    "timestamp": "2026-02-27 10:00:59.000",
                    "service_name": "payment",
                    "level": "WARN",
                    "message": "checkout timeout while handling request",
                    "pod_name": "payment-5f6d9c8b7c-abcde",
                    "namespace": "prod",
                    "node_name": "node-2",
                    "container_name": "payment",
                    "container_id": "c-2",
                    "container_image": "payment:v1",
                    "pod_id": "p-2",
                    "trace_id": "",
                    "span_id": "span-2",
                    "labels": '{}',
                    "attributes_json": '{}',
                    "host_ip": "10.0.0.2",
                },
            ]

    result = query_topology_edge_logs_preview(
        storage_adapter=PodFallbackStorage(),
        source_service="checkout",
        target_service="payment",
        time_window="1 HOUR",
        limit=2,
        exclude_health_check=False,
        namespace=None,
        anchor_time=None,
        sanitize_interval_fn=query_routes._sanitize_interval,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        to_datetime_fn=query_routes._to_datetime,
    )

    assert result["data"][0]["id"] == "src-pod"
    assert result["data"][0]["edge_side"] == "source"
    assert result["data"][0]["edge_match_kind"] == "source_mentions_target"


def test_query_topology_edge_logs_preview_prefers_source_side_over_target_side_when_scores_are_close():
    """链路日志预览应优先展示源端日志，避免目标端日志抢占顶部。"""
    from api.query_logs_service import query_topology_edge_logs_preview

    class SourcePreferredStorage:
        def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
            return [
                {
                    "id": "src-1",
                    "timestamp": "2026-02-27 10:01:00.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "issue while calling payment",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "",
                    "span_id": "span-1",
                    "labels": '{}',
                    "attributes_json": '{}',
                    "host_ip": "10.0.0.1",
                },
                {
                    "id": "dst-1",
                    "timestamp": "2026-02-27 10:02:00.000",
                    "service_name": "payment",
                    "level": "ERROR",
                    "message": "checkout timeout while handling request",
                    "pod_name": "payment-1",
                    "namespace": "prod",
                    "node_name": "node-2",
                    "container_name": "payment",
                    "container_id": "c-2",
                    "container_image": "payment:v1",
                    "pod_id": "p-2",
                    "trace_id": "trace-2",
                    "span_id": "span-2",
                    "labels": '{}',
                    "attributes_json": '{"request_id":"req-2"}',
                    "host_ip": "10.0.0.2",
                },
            ]

    result = query_topology_edge_logs_preview(
        storage_adapter=SourcePreferredStorage(),
        source_service="checkout",
        target_service="payment",
        time_window="1 HOUR",
        limit=2,
        exclude_health_check=False,
        namespace=None,
        anchor_time=None,
        sanitize_interval_fn=query_routes._sanitize_interval,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        to_datetime_fn=query_routes._to_datetime,
    )

    assert result["data"][0]["id"] == "src-1"
    assert result["data"][0]["edge_side"] == "source"
    assert result["data"][1]["id"] == "dst-1"
    assert result["data"][1]["edge_side"] == "target"


def test_query_topology_edge_logs_preview_matches_service_case_insensitively_for_edge_side():
    """链路日志预览应按大小写不敏感方式识别 source/target 服务。"""
    from api.query_logs_service import query_topology_edge_logs_preview

    class CaseInsensitiveServiceStorage:
        def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
            return [
                {
                    "id": "src-case",
                    "timestamp": "2026-02-27 10:01:00.000",
                    "service_name": "checkout",
                    "level": "INFO",
                    "message": "processing request",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "",
                    "span_id": "span-1",
                    "labels": '{}',
                    "attributes_json": '{}',
                    "host_ip": "10.0.0.1",
                },
            ]

    result = query_topology_edge_logs_preview(
        storage_adapter=CaseInsensitiveServiceStorage(),
        source_service="Checkout",
        target_service="Payment",
        time_window="1 HOUR",
        limit=5,
        exclude_health_check=False,
        namespace=None,
        source_namespace=None,
        target_namespace=None,
        anchor_time=None,
        sanitize_interval_fn=query_routes._sanitize_interval,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        to_datetime_fn=query_routes._to_datetime,
    )

    assert result["count"] == 1
    assert result["data"][0]["id"] == "src-case"
    assert result["data"][0]["edge_side"] == "source"


def test_query_topology_edge_logs_preview_relaxes_namespace_scope_when_seed_empty():
    """严格命名空间无命中时应自动回退到服务维度，避免预览完全为空。"""
    from api.query_logs_service import query_topology_edge_logs_preview

    class NamespaceFallbackStorage:
        def __init__(self):
            self.calls: List[Dict[str, Any]] = []

        def execute_query(self, query: str, params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
            condensed = " ".join(query.split())
            payload = {"query": condensed, "params": params or {}}
            self.calls.append(payload)

            if "namespace = {namespace:String}" in condensed:
                return []

            return [
                {
                    "id": "seed-1",
                    "timestamp": "2026-02-27 10:01:00.000",
                    "service_name": "checkout",
                    "level": "ERROR",
                    "message": "call payment failed",
                    "pod_name": "checkout-1",
                    "namespace": "prod",
                    "node_name": "node-1",
                    "container_name": "checkout",
                    "container_id": "c-1",
                    "container_image": "checkout:v1",
                    "pod_id": "p-1",
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "labels": '{}',
                    "attributes_json": '{"request_id":"req-1"}',
                    "host_ip": "10.0.0.1",
                },
            ]

    storage = NamespaceFallbackStorage()
    result = query_topology_edge_logs_preview(
        storage_adapter=storage,
        source_service="checkout",
        target_service="payment",
        time_window="1 HOUR",
        limit=5,
        exclude_health_check=False,
        namespace="wrong-ns",
        source_namespace=None,
        target_namespace=None,
        anchor_time=None,
        sanitize_interval_fn=query_routes._sanitize_interval,
        append_health_check_exclusion_fn=query_routes._append_health_check_exclusion,
        convert_timestamp_fn=query_routes._convert_timestamp,
        to_datetime_fn=query_routes._to_datetime,
    )

    assert result["count"] == 1
    assert result["data"][0]["id"] == "seed-1"
    assert result["context"]["namespace_scope_relaxed"] is True
    assert "namespace_scope_relaxed" in result["context"]["degrade_reasons"]
    assert len(storage.calls) >= 2
    assert any("namespace = {namespace:String}" in call["query"] for call in storage.calls)
    assert any("namespace = {namespace:String}" not in call["query"] for call in storage.calls)


@pytest.mark.asyncio
async def test_query_topology_edge_logs_preview_expands_by_trace_and_request_id():
    """链路日志预览应基于 trace_id/request_id 扩展同命名空间关联日志。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_topology_edge_logs_preview(
        source_service="checkout",
        target_service="payment",
        namespace="prod",
        time_window="15 MINUTE",
        anchor_time="2026-02-27T10:05:00Z",
        limit=4,
        exclude_health_check=True,
    )

    assert result["count"] == 4
    assert any(item["id"] == "e4" for item in result["data"])
    correlated = next(item for item in result["data"] if item["id"] == "e4")
    assert correlated["edge_side"] == "correlated"
    assert correlated["correlation_trace_id"] == "trace-1"
    assert correlated["correlation_request_id"] == "req-1"
    assert result["context"]["expanded_count"] >= 1
    assert result["context"]["expansion_enabled"] is True
    assert result["context"]["trace_id_count"] == 1
    assert result["context"]["request_id_count"] == 1


@pytest.mark.asyncio
async def test_query_topology_edge_logs_preview_supports_namespace_and_anchor_time():
    """链路日志预览应复用拓扑命名空间和锚点时间。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_topology_edge_logs_preview(
        source_service="checkout",
        target_service="payment",
        namespace="prod",
        time_window="15 MINUTE",
        anchor_time="2026-02-27T10:05:00Z",
        limit=3,
        exclude_health_check=True,
    )

    query_call = _first_business_call(storage)
    assert "namespace = {namespace:String}" in query_call["query"]
    assert "timestamp <= toDateTime64({anchor_time:String}, 9, 'UTC')" in query_call["query"]
    assert "timestamp > toDateTime64({anchor_time:String}, 9, 'UTC') - INTERVAL 15 MINUTE" in query_call["query"]
    assert query_call["params"]["namespace"] == "prod"
    assert query_call["params"]["anchor_time"] in {"2026-02-27 10:05:00.000", "2026-02-27 10:05:00.000000"}
    assert result["context"]["namespace"] == "prod"
    assert result["context"]["anchor_time"] == "2026-02-27T10:05:00Z"


@pytest.mark.asyncio
async def test_query_logs_accepts_topology_edge_context_filters():
    """普通日志查询在仅提供 source/target 时应进入边上下文候选查询。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_logs(
        limit=20,
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
        time_window="30 MINUTE",
    )

    query_call = _first_business_call(storage)
    assert "timestamp > now() - INTERVAL 30 MINUTE" in query_call["query"]
    assert "replaceRegexpOne" in query_call["query"]
    assert "message ILIKE concat('%', {target_service:String}, '%')" in query_call["query"]
    assert query_call["params"]["source_service"] == "checkout"
    assert query_call["params"]["target_service"] == "payment"
    assert "service_name" not in query_call["params"]
    assert "search" not in query_call["params"]
    assert result["context"]["edge_context_active"] is True
    assert result["context"]["effective_service_name"] is None
    assert result["context"]["effective_search"] is None
    assert result["context"]["time_window"] == "30 MINUTE"


@pytest.mark.asyncio
async def test_query_logs_same_source_target_degrades_to_single_service_filter():
    """source/target 相同不应触发 edge-context 扩大扫描，应回退单服务过滤。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_logs(
        limit=20,
        service_name=None,
        trace_id=None,
        pod_name=None,
        level=None,
        start_time=None,
        end_time=None,
        exclude_health_check=False,
        search=None,
        source_service="checkout",
        target_service="checkout",
        time_window="30 MINUTE",
    )

    query_call = _first_business_call(storage)
    assert "target_service:String" not in query_call["query"]
    assert "service_name" in query_call["params"]
    assert "source_service" not in query_call["params"]
    assert "target_service" not in query_call["params"]
    assert result["context"]["edge_context_active"] is False
    assert result["context"]["effective_service_name"] == "checkout"
    assert result["context"]["effective_search"] is None


@pytest.mark.asyncio
async def test_topology_edge_preview_returns_correlation_id_context():
    """链路预览应返回 trace/request 相关 ID，供查看全部精确跳转复用。"""
    storage = FakeStorageAdapter()
    query_routes.set_storage_adapter(storage)

    result = await query_routes.query_topology_edge_logs_preview(
        source_service="checkout",
        target_service="payment",
        namespace="prod",
        time_window="15 MINUTE",
        anchor_time="2026-02-27T10:05:00Z",
        limit=10,
        exclude_health_check=False,
    )

    assert result["context"]["trace_id_count"] >= 1
    assert result["context"]["request_id_count"] >= 1
    assert result["context"]["trace_ids"] == ["trace-1"]
    assert result["context"]["request_ids"] == ["req-1"]
