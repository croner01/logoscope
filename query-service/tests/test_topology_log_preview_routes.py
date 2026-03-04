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
            # /logs/preview/topology-edge 查询
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
                    "labels": "{\"app\":\"checkout\"}",
                    "attributes_json": "{\"peer.service\":\"payment\"}",
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
                    "labels": "{\"app\":\"payment\"}",
                    "attributes_json": "{\"upstream\":\"checkout\"}",
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
                    "labels": "{\"app\":\"checkout\"}",
                    "attributes_json": "{}",
                    "host_ip": "10.0.0.1",
                },
            ]

        if "FROM logs.logs" in condensed:
            # /logs 查询
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
                    "labels": "{\"app\":\"checkout\"}",
                    "attributes_json": "{\"peer.service\":\"payment\"}",
                    "host_ip": "10.0.0.1",
                }
            ]
        return []


@pytest.fixture(autouse=True)
def reset_state():
    query_routes.set_storage_adapter(None)
    yield
    query_routes.set_storage_adapter(None)


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
async def test_query_logs_accepts_topology_context_filters():
    """普通日志查询在缺少显式过滤时应自动应用拓扑上下文参数。"""
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
    assert query_call["params"]["service_name"] == "checkout"
    assert query_call["params"]["search"] == "payment"
    assert result["context"]["effective_service_name"] == "checkout"
    assert result["context"]["effective_search"] == "payment"
    assert result["context"]["time_window"] == "30 MINUTE"
