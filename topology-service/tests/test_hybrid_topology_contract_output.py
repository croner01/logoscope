"""
Tests for graph/hybrid_topology.py contract output
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.hybrid_topology import HybridTopologyBuilder


class FakeStorageAdapter:
    """用于 hybrid topology 测试的存储桩。"""

    def __init__(self):
        self.ch_client = object()

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())

        if "FROM logs.traces" in condensed:
            return [
                {
                    "trace_id": "trace-1",
                    "span_id": "span-root",
                    "parent_span_id": "",
                    "service_name": "frontend",
                    "operation_name": "GET /api/orders",
                    "status": "STATUS_CODE_OK",
                    "attributes_json": json.dumps({"duration_ms": 20}),
                    "timestamp": datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc),
                },
                {
                    "trace_id": "trace-1",
                    "span_id": "span-child",
                    "parent_span_id": "span-root",
                    "service_name": "order-service",
                    "operation_name": "GET /api/orders/123",
                    "status": "STATUS_CODE_ERROR",
                    "attributes_json": json.dumps({
                        "duration_ms": 180,
                        "retry_count": 1,
                        "pending": 0,
                        "dlq": 0,
                    }),
                    "timestamp": datetime(2026, 2, 26, 12, 0, 1, tzinfo=timezone.utc),
                },
            ]

        if "FROM logs.logs" in condensed and "GROUP BY service_name" in condensed:
            return [
                {
                    "service_name": "frontend",
                    "log_count": 120,
                    "pod_count": 2,
                    "error_count": 1,
                    "last_seen": datetime(2026, 2, 26, 12, 0, 5, tzinfo=timezone.utc),
                },
                {
                    "service_name": "order-service",
                    "log_count": 240,
                    "pod_count": 3,
                    "error_count": 3,
                    "last_seen": datetime(2026, 2, 26, 12, 0, 6, tzinfo=timezone.utc),
                },
            ]

        if "FROM logs.metrics" in condensed:
            return []

        return []

    def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
        return {
            "frontend->order-service": {
                "call_count": 1,
                "error_count": 1,
                "error_rate": 1.0,
                "p95": 180.0,
                "p99": 180.0,
                "timeout_rate": 0.0,
                "retries": 1.0,
            }
        }


class TestHybridTopologyContractOutput:
    """Hybrid topology 输出契约测试。"""

    def test_build_topology_contains_m1_fields(self):
        """输出应包含 M1 新增字段并保持兼容。"""
        builder = HybridTopologyBuilder(FakeStorageAdapter())
        topology = builder.build_topology(time_window="1 HOUR", confidence_threshold=0.0)

        assert topology["metadata"]["contract_version"] == "topology-schema-v1"
        assert topology["metadata"]["quality_version"] == "quality-score-v1"
        assert len(topology["nodes"]) >= 1
        assert len(topology["edges"]) >= 1

        node = topology["nodes"][0]
        assert "node_key" in node
        assert "service" in node
        assert "evidence_type" in node
        assert "coverage" in node
        assert "quality_score" in node

        edge = topology["edges"][0]
        assert "edge_key" in edge
        assert "protocol" in edge
        assert "endpoint_pattern" in edge
        assert "evidence_type" in edge
        assert "coverage" in edge
        assert "quality_score" in edge
        assert "p95" in edge
        assert "p99" in edge
        assert "timeout_rate" in edge


class NamespaceAmbiguousStorage(FakeStorageAdapter):
    """用于验证同服务跨 namespace 节点标记的存储桩。"""

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())

        if "FROM logs.traces" in condensed:
            return []

        if "FROM logs.logs" in condensed and "GROUP BY service_name" in condensed:
            return [
                {
                    "service_name": "checkout",
                    "namespace": "prod-a",
                    "log_count": 120,
                    "pod_count": 4,
                    "error_count": 3,
                    "last_seen": datetime(2026, 2, 26, 12, 0, 5, tzinfo=timezone.utc),
                }
            ]

        if "FROM logs.metrics" in condensed:
            return []

        return []


class TestHybridTopologyNamespaceAmbiguity:
    """混合拓扑命名空间拆分兼容测试。"""

    def test_build_topology_splits_legacy_multi_namespace_node(self):
        builder = HybridTopologyBuilder(NamespaceAmbiguousStorage())
        topology = builder.build_topology(time_window="1 HOUR", confidence_threshold=0.0)

        checkout_nodes = [node for node in topology["nodes"] if node["service"]["name"] == "checkout"]
        assert len(checkout_nodes) == 1
        assert checkout_nodes[0]["service"]["namespace"] == "prod-a"
        assert checkout_nodes[0]["metrics"]["namespace_ambiguous"] is False


class NamespaceSplitStorage(FakeStorageAdapter):
    """用于验证同服务跨 namespace 会拆分节点的存储桩。"""

    def execute_query(self, query: str):
        condensed = " ".join(query.split())

        if "FROM logs.traces" in condensed:
            return []

        if "FROM logs.logs" in condensed and "GROUP BY service_name, namespace" in condensed:
            return [
                {
                    "service_name": "checkout",
                    "namespace": "prod-a",
                    "log_count": 120,
                    "pod_count": 2,
                    "error_count": 3,
                    "last_seen": datetime(2026, 2, 26, 12, 0, 5, tzinfo=timezone.utc),
                },
                {
                    "service_name": "checkout",
                    "namespace": "prod-b",
                    "log_count": 90,
                    "pod_count": 1,
                    "error_count": 1,
                    "last_seen": datetime(2026, 2, 26, 12, 0, 6, tzinfo=timezone.utc),
                },
            ]

        if "FROM logs.metrics" in condensed:
            return []

        return []


class TraceNamespaceEdgeStorage(FakeStorageAdapter):
    """用于验证 traces 边会保留 source/target namespace。"""

    def execute_query(self, query: str):
        condensed = " ".join(query.split())

        if "FROM logs.traces" in condensed:
            return [
                {
                    "trace_id": "trace-1",
                    "span_id": "span-root",
                    "parent_span_id": "",
                    "service_name": "frontend",
                    "operation_name": "GET /api/orders",
                    "status": "STATUS_CODE_OK",
                    "timestamp": datetime(2026, 2, 26, 12, 0, 0, tzinfo=timezone.utc),
                    "span_namespace": "prod-a",
                    "duration_ms_norm": 20,
                },
                {
                    "trace_id": "trace-1",
                    "span_id": "span-child",
                    "parent_span_id": "span-root",
                    "service_name": "order-service",
                    "operation_name": "GET /api/orders/123",
                    "status": "STATUS_CODE_ERROR",
                    "timestamp": datetime(2026, 2, 26, 12, 0, 1, tzinfo=timezone.utc),
                    "span_namespace": "prod-b",
                    "duration_ms_norm": 180,
                },
            ]

        if "FROM logs.logs" in condensed:
            return []

        if "FROM logs.metrics" in condensed:
            return []

        return []


class TestHybridTopologyNamespaceSplit:
    """混合拓扑命名空间拆分测试。"""

    def test_build_topology_splits_same_service_by_namespace(self):
        builder = HybridTopologyBuilder(NamespaceSplitStorage())
        topology = builder.build_topology(time_window="1 HOUR", confidence_threshold=0.0)

        checkout_nodes = [node for node in topology["nodes"] if node["service"]["name"] == "checkout"]
        assert len(checkout_nodes) == 2
        assert {node["service"]["namespace"] for node in checkout_nodes} == {"prod-a", "prod-b"}
        assert {node["id"] for node in checkout_nodes} == {"prod-a:checkout:prod", "prod-b:checkout:prod"}

    def test_build_topology_edge_preserves_source_target_namespace(self):
        builder = HybridTopologyBuilder(TraceNamespaceEdgeStorage())
        topology = builder.build_topology(time_window="1 HOUR", confidence_threshold=0.0)

        edge = topology["edges"][0]
        assert edge["source_service"] == "frontend"
        assert edge["target_service"] == "order-service"
        assert edge["source_namespace"] == "prod-a"
        assert edge["target_namespace"] == "prod-b"
        assert edge["source"] == "frontend"
        assert edge["target"] == "order-service"
        assert edge["source_node_key"] == "prod-a:frontend:prod"
        assert edge["target_node_key"] == "prod-b:order-service:prod"
