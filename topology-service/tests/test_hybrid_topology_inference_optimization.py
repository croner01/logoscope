"""
Hybrid topology 推断优化测试（P0/P1）
"""
import os
import sys
import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import patch

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.hybrid_topology import HybridTopologyBuilder


class FakeStorageForInference:
    """用于推断逻辑测试的存储桩。"""

    def __init__(
        self,
        grouped_rows: List[Dict[str, Any]],
        inference_rows: List[Dict[str, Any]],
    ):
        self.ch_client = object()
        self.grouped_rows = grouped_rows
        self.inference_rows = inference_rows

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        if "FROM logs.logs" in condensed and "GROUP BY service_name" in condensed:
            return self.grouped_rows
        if "FROM logs.logs" in condensed and "ORDER BY timestamp DESC" in condensed:
            return list(reversed(self.inference_rows))
        if "FROM logs.traces" in condensed:
            return []
        if "FROM logs.metrics" in condensed:
            return []
        return []

    def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
        _ = (time_window, namespace)
        return {}


class FakeStorageForMetricsTopology:
    """用于 metrics topology 查询兼容性的存储桩。"""

    def __init__(self):
        self.ch_client = object()
        self.captured_queries: List[str] = []

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        self.captured_queries.append(condensed)
        if "FROM logs.metrics" in condensed:
            return [
                {
                    "service_name": "payment-service",
                    "metric_count": 12,
                    "unique_metrics": 4,
                    "namespace_top": ["islap"],
                }
            ]
        return []

    def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
        _ = (time_window, namespace)
        return {}


class FakeStorageForTracesNamespaceTopology:
    """用于 traces namespace SQL 下推兼容性的存储桩。"""

    def __init__(self, has_traces_namespace_column: bool, traces_rows: List[Dict[str, Any]] = None):
        self.ch_client = object()
        self.has_traces_namespace_column = has_traces_namespace_column
        self.traces_rows = traces_rows or []
        self.captured_queries: List[str] = []

    def execute_query(self, query: str):
        condensed = " ".join(query.split())
        self.captured_queries.append(condensed)
        if (
            "FROM system.columns" in condensed
            and "table = 'traces'" in condensed
            and "name = 'traces_namespace'" in condensed
        ):
            return [{"cnt": 1 if self.has_traces_namespace_column else 0}]
        if "FROM logs.traces" in condensed:
            return self.traces_rows
        return []

    def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
        _ = (time_window, namespace)
        return {}


class TestHybridTopologyInferenceOptimization:
    """推断优化测试集合。"""

    def test_message_target_inference_extracts_service_edges(self):
        """应从日志 URL host 中提取服务目标并生成 message_target 边。"""
        storage = FakeStorageForInference(
            grouped_rows=[],
            inference_rows=[
                {
                    "id": "log-1",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "calling http://payment.svc.cluster.local/api/v1/pay",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-2",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 2, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "retry target=https://payment:8080/api/pay",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-3",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 3, tzinfo=timezone.utc),
                    "service_name": "payment",
                    "namespace": "islap",
                    "message": "request finished",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )
        builder = HybridTopologyBuilder(storage)

        edges, stats = builder._infer_edges_from_logs(time_window="1 HOUR", namespace="islap")

        matched = [
            edge
            for edge in edges
            if edge.get("source") == "frontend" and edge.get("target") == "payment"
        ]
        assert len(matched) == 1
        assert matched[0]["metrics"]["inference_method"] == "message_target"
        assert matched[0]["metrics"]["reason"] == "message_endpoint_pattern"
        assert stats["message_target_edges"] == 2
        assert stats["message_target_enabled"] is True
        assert "url" in (stats.get("message_target_patterns") or [])
        assert stats["evidence_sparse"] is False

    def test_inference_normalizes_node_key_style_service_names(self):
        """推断链路应将 node_key 风格 service_name 归一化为服务名。"""
        storage = FakeStorageForInference(
            grouped_rows=[],
            inference_rows=[
                {
                    "id": "log-1",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "islap:query-service:prod",
                    "namespace": "",
                    "message": "calling http://otel-collector:4317/v1/logs",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-2",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 1, tzinfo=timezone.utc),
                    "service_name": "default:otel-collector:prod",
                    "namespace": "",
                    "message": "collector ready",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )
        builder = HybridTopologyBuilder(storage)

        edges, stats = builder._infer_edges_from_logs(
            time_window="1 HOUR",
            namespace=None,
            message_target_min_support=1,
        )

        matched = [
            edge
            for edge in edges
            if edge.get("source") == "query-service" and edge.get("target") == "otel-collector"
        ]
        assert matched
        assert matched[0]["metrics"]["source_service"] == "query-service"
        assert matched[0]["metrics"]["target_service"] == "otel-collector"
        assert matched[0]["metrics"]["source_namespace"] == "islap"
        assert stats["message_target_edges"] >= 1

    def test_message_target_supports_kv_proxy_and_rpc_patterns(self):
        """应支持 host/upstream、proxy cluster、rpc 错误日志的目标提取。"""
        storage = FakeStorageForInference(
            grouped_rows=[],
            inference_rows=[
                {
                    "id": "log-1",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "host=payment upstream=order-service:8080",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-2",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 1, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "upstream_cluster=outbound|8080||inventory.svc.cluster.local",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-3",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 2, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "rpc failed to payment:9000, connection refused",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-4",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 3, tzinfo=timezone.utc),
                    "service_name": "payment",
                    "namespace": "islap",
                    "message": "ok",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-5",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 4, tzinfo=timezone.utc),
                    "service_name": "order-service",
                    "namespace": "islap",
                    "message": "ok",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-6",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 5, tzinfo=timezone.utc),
                    "service_name": "inventory",
                    "namespace": "islap",
                    "message": "ok",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )

        with patch.dict(
            os.environ,
            {
                "TOPOLOGY_MESSAGE_TARGET_PATTERNS": "kv,proxy,rpc",
                "TOPOLOGY_MIN_SUPPORT_MESSAGE_TARGET": "1",
                "TOPOLOGY_MAX_MESSAGE_TARGETS_PER_LOG": "6",
            },
            clear=False,
        ):
            builder = HybridTopologyBuilder(storage)
            edges, stats = builder._infer_edges_from_logs(time_window="1 HOUR", namespace="islap")

        pairs = {(edge.get("source"), edge.get("target")) for edge in edges}
        assert ("frontend", "payment") in pairs
        assert ("frontend", "order-service") in pairs
        assert ("frontend", "inventory") in pairs
        assert stats["message_target_edges"] >= 4
        assert stats["message_target_enabled"] is True
        assert stats["method"] == "request_id_then_trace_id_then_message_target_then_time_window"

    def test_message_target_can_be_disabled_by_env_switch(self):
        """关闭 message_target 开关后，不应生成 message_target 推断边。"""
        storage = FakeStorageForInference(
            grouped_rows=[],
            inference_rows=[
                {
                    "id": "log-1",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "host=payment upstream=order-service",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-2",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 1, tzinfo=timezone.utc),
                    "service_name": "payment",
                    "namespace": "islap",
                    "message": "ok",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-3",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 2, tzinfo=timezone.utc),
                    "service_name": "order-service",
                    "namespace": "islap",
                    "message": "ok",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )

        with patch.dict(
            os.environ,
            {
                "TOPOLOGY_MESSAGE_TARGET_ENABLED": "false",
                "TOPOLOGY_MESSAGE_TARGET_PATTERNS": "url,kv,proxy,rpc",
            },
            clear=False,
        ):
            builder = HybridTopologyBuilder(storage)
            edges, stats = builder._infer_edges_from_logs(time_window="1 HOUR", namespace="islap")

        assert edges == []
        assert stats["message_target_edges"] == 0
        assert stats["message_target_enabled"] is False
        assert stats["method"] == "request_id_then_trace_id_then_time_window"
        assert stats["evidence_sparse"] is True

    def test_registry_heuristic_is_suppressed_when_no_strong_evidence(self):
        """无强证据时应抑制 registry image_pull 启发式边，避免噪声。"""
        storage = FakeStorageForInference(
            grouped_rows=[
                {
                    "service_name": "frontend",
                    "log_count": 220,
                    "pod_count": 2,
                    "namespace_top": ["islap"],
                    "error_count": 0,
                    "last_seen": datetime(2026, 2, 28, 11, 0, 0, tzinfo=timezone.utc),
                },
                {
                    "service_name": "registry",
                    "log_count": 480,
                    "pod_count": 1,
                    "namespace_top": ["islap"],
                    "error_count": 0,
                    "last_seen": datetime(2026, 2, 28, 11, 0, 2, tzinfo=timezone.utc),
                },
            ],
            inference_rows=[
                {
                    "id": "log-a",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "startup complete",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-b",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 10, tzinfo=timezone.utc),
                    "service_name": "registry",
                    "namespace": "islap",
                    "message": "registry healthy",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )
        builder = HybridTopologyBuilder(storage)

        result = builder._get_logs_topology(time_window="1 HOUR", namespace="islap")
        edges = result.get("edges", [])
        metadata = result.get("metadata", {})
        inference_stats = metadata.get("inference_stats", {})

        assert edges == []
        assert inference_stats.get("evidence_sparse") is True
        assert inference_stats.get("message_target_edges") == 0

    def test_strong_evidence_direction_overrides_reverse_heuristic(self):
        """当 request_id 已确定方向时，不应再追加相反方向的启发式边。"""
        storage = FakeStorageForInference(
            grouped_rows=[
                {
                    "service_name": "backend",
                    "log_count": 320,
                    "pod_count": 2,
                    "namespace_top": ["islap"],
                    "error_count": 0,
                    "last_seen": datetime(2026, 2, 28, 11, 0, 0, tzinfo=timezone.utc),
                },
                {
                    "service_name": "frontend",
                    "log_count": 210,
                    "pod_count": 2,
                    "namespace_top": ["islap"],
                    "error_count": 0,
                    "last_seen": datetime(2026, 2, 28, 11, 0, 1, tzinfo=timezone.utc),
                },
            ],
            inference_rows=[
                {
                    "id": "log-1",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "request_id=req-123456 calling backend",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-2",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, 250000, tzinfo=timezone.utc),
                    "service_name": "backend",
                    "namespace": "islap",
                    "message": "request_id=req-123456 handled",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )
        builder = HybridTopologyBuilder(storage)

        result = builder._get_logs_topology(time_window="1 HOUR", namespace="islap")
        edges = result.get("edges", [])
        pairs = {(edge.get("source"), edge.get("target")) for edge in edges}

        assert ("frontend", "backend") in pairs
        assert ("backend", "frontend") not in pairs

    def test_heuristic_relation_is_order_independent_for_db_like_services(self):
        """数据库/缓存类服务排在前面时，也应识别关系并推断业务 -> DB 方向。"""
        storage = FakeStorageForInference(
            grouped_rows=[
                {
                    "service_name": "redis",
                    "log_count": 500,
                    "pod_count": 1,
                    "namespace_top": ["islap"],
                    "error_count": 0,
                    "last_seen": datetime(2026, 2, 28, 11, 0, 0, tzinfo=timezone.utc),
                },
                {
                    "service_name": "payment-service",
                    "log_count": 210,
                    "pod_count": 2,
                    "namespace_top": ["islap"],
                    "error_count": 0,
                    "last_seen": datetime(2026, 2, 28, 11, 0, 1, tzinfo=timezone.utc),
                },
            ],
            inference_rows=[],
        )
        builder = HybridTopologyBuilder(storage)

        result = builder._get_logs_topology(time_window="1 HOUR", namespace="islap")
        edges = result.get("edges", [])
        pairs = {(edge.get("source"), edge.get("target")) for edge in edges}

        assert ("payment-service", "redis") in pairs

    def test_hybrid_score_supports_service_alias_and_evidence_score(self):
        """hybrid_score 模式应支持服务短名匹配并输出证据充分度。"""
        storage = FakeStorageForInference(
            grouped_rows=[],
            inference_rows=[
                {
                    "id": "log-1",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc),
                    "service_name": "frontend",
                    "namespace": "islap",
                    "message": "upstream=payment host=payment.svc.cluster.local",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
                {
                    "id": "log-2",
                    "timestamp": datetime(2026, 2, 28, 10, 0, 1, tzinfo=timezone.utc),
                    "service_name": "payment-service",
                    "namespace": "islap",
                    "message": "request received",
                    "trace_id": "",
                    "attributes_json": json.dumps({}),
                },
            ],
        )
        builder = HybridTopologyBuilder(storage)

        edges, stats = builder._infer_edges_from_logs(
            time_window="1 HOUR",
            namespace="islap",
            inference_mode="hybrid_score",
            message_target_min_support=1,
        )

        matched = [
            edge
            for edge in edges
            if edge.get("source") == "frontend" and edge.get("target") == "payment-service"
        ]
        assert len(matched) == 1
        assert stats["inference_mode"] == "hybrid_score"
        assert matched[0]["metrics"]["inference_mode"] == "hybrid_score"
        assert float(matched[0]["metrics"]["evidence_sufficiency_score"]) > 0

    def test_hybrid_score_time_window_finds_non_adjacent_candidate(self):
        """hybrid_score 时间窗候选打分应能识别非相邻但高相关调用。"""
        rows: List[Dict[str, Any]] = []
        base = datetime(2026, 2, 28, 10, 0, 0, tzinfo=timezone.utc)
        for idx in range(5):
            t0 = base.replace(second=idx * 2)
            rows.extend(
                [
                    {
                        "id": f"log-a-{idx}",
                        "timestamp": t0,
                        "service_name": "api",
                        "namespace": "islap",
                        "message": "call downstream service",
                        "trace_id": "",
                        "attributes_json": json.dumps({}),
                    },
                    {
                        "id": f"log-b-{idx}",
                        "timestamp": t0.replace(microsecond=120000),
                        "service_name": "worker",
                        "namespace": "islap",
                        "message": "processing request",
                        "trace_id": "",
                        "attributes_json": json.dumps({}),
                    },
                    {
                        "id": f"log-c-{idx}",
                        "timestamp": t0.replace(microsecond=220000),
                        "service_name": "db",
                        "namespace": "islap",
                        "message": "request received",
                        "trace_id": "",
                        "attributes_json": json.dumps({}),
                    },
                ]
            )

        storage = FakeStorageForInference(grouped_rows=[], inference_rows=rows)
        builder = HybridTopologyBuilder(storage)

        edges_rule, _ = builder._infer_edges_from_logs(
            time_window="1 HOUR",
            namespace="islap",
            inference_mode="rule",
        )
        edges_hybrid, _ = builder._infer_edges_from_logs(
            time_window="1 HOUR",
            namespace="islap",
            inference_mode="hybrid_score",
        )

        pairs_rule = {(edge.get("source"), edge.get("target")) for edge in edges_rule}
        pairs_hybrid = {(edge.get("source"), edge.get("target")) for edge in edges_hybrid}

        assert ("api", "db") not in pairs_rule
        assert ("api", "db") in pairs_hybrid

    def test_metrics_topology_uses_attributes_namespace_without_namespace_column(self):
        """metrics 查询应从 attributes_json 提取 namespace，避免依赖不存在的 namespace 列。"""
        storage = FakeStorageForMetricsTopology()
        builder = HybridTopologyBuilder(storage)

        result = builder._get_metrics_topology(time_window="1 HOUR", namespace="islap")
        nodes = result.get("nodes", [])
        assert len(nodes) == 1
        assert nodes[0]["id"] == "payment-service"
        assert nodes[0]["namespace"] == "islap"
        assert nodes[0]["metrics"]["service_namespace"] == "islap"

        metrics_queries = [item for item in storage.captured_queries if "FROM logs.metrics" in item]
        assert metrics_queries
        metrics_query = metrics_queries[-1]
        assert "topK(1)(namespace)" not in metrics_query
        assert " namespace = 'islap'" not in metrics_query
        assert "JSONExtractString(attributes_json, 'service_namespace')" in metrics_query
        assert "JSONExtractString(attributes_json, 'namespace')" in metrics_query

    def test_traces_topology_prefers_traces_namespace_column_when_available(self):
        """traces 存在 traces_namespace 列时，应在 SQL 扫描阶段下推命名空间过滤。"""
        storage = FakeStorageForTracesNamespaceTopology(has_traces_namespace_column=True)
        builder = HybridTopologyBuilder(storage)

        builder._get_traces_topology(time_window="1 HOUR", namespace="islap")

        traces_queries = [item for item in storage.captured_queries if "FROM logs.traces" in item]
        assert traces_queries
        traces_query = traces_queries[-1]
        assert "PREWHERE timestamp > now() - INTERVAL 1 HOUR" in traces_query
        assert "traces_namespace = 'islap'" in traces_query
        assert "JSONExtractString(attributes_json, 'service_namespace')" not in traces_query

    def test_traces_topology_fallbacks_to_attributes_namespace_when_column_missing(self):
        """traces 缺少 traces_namespace 列时，应回退到 attributes_json 表达式过滤。"""
        storage = FakeStorageForTracesNamespaceTopology(has_traces_namespace_column=False)
        builder = HybridTopologyBuilder(storage)

        builder._get_traces_topology(time_window="1 HOUR", namespace="islap")

        traces_queries = [item for item in storage.captured_queries if "FROM logs.traces" in item]
        assert traces_queries
        traces_query = traces_queries[-1]
        assert "traces_namespace = 'islap'" not in traces_query
        assert "JSONExtractString(attributes_json, 'service_namespace')" in traces_query
        assert "JSONExtractString(attributes_json, 'namespace')" in traces_query

    def test_traces_topology_avoids_namespace_json_extract_when_namespace_not_requested(self):
        """未指定 namespace 且缺少 traces_namespace 列时，明细扫描应避免 JSONExtract 命名空间表达式。"""
        storage = FakeStorageForTracesNamespaceTopology(has_traces_namespace_column=False)
        builder = HybridTopologyBuilder(storage)

        builder._get_traces_topology(time_window="1 HOUR", namespace=None)

        traces_queries = [item for item in storage.captured_queries if "FROM logs.traces" in item]
        assert traces_queries
        traces_query = traces_queries[-1]
        assert "AS span_namespace" in traces_query
        assert "JSONExtractString(attributes_json, 'service_namespace')" not in traces_query
        assert "JSONExtractString(attributes_json, 'namespace')" not in traces_query
        assert "optimize_read_in_order = 1" in traces_query

    def test_traces_topology_prefers_k8s_namespace_over_generic_namespace_key(self):
        """
        当 attributes 同时包含 k8s.namespace.name 与 namespace 时，应优先使用 k8s 命名空间，
        避免把 generic namespace（可能是组件名）误判为命名空间。
        """
        storage = FakeStorageForTracesNamespaceTopology(
            has_traces_namespace_column=False,
            traces_rows=[
                {
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "parent_span_id": "",
                    "service_name": "semantic-engine",
                    "operation_name": "GET /api/v1/topology",
                    "status": "STATUS_CODE_OK",
                    "attributes_json": json.dumps(
                        {
                            "namespace": "semantic-engine",
                            "k8s.namespace.name": "islap",
                            "duration_ms": 8,
                        }
                    ),
                    "timestamp": datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
                }
            ],
        )
        builder = HybridTopologyBuilder(storage)

        result = builder._get_traces_topology(time_window="1 HOUR", namespace="islap")
        nodes = result.get("nodes", [])
        assert len(nodes) == 1
        assert nodes[0]["id"] == "semantic-engine"
        assert nodes[0]["metrics"]["namespace"] == "islap"
        assert nodes[0]["metrics"]["service_namespace"] == "islap"

    def test_edge_red_aggregation_skips_query_when_no_traces_edge(self):
        """无 traces 边时应跳过 edge RED 聚合 SQL，避免无效重查询。"""
        class StorageStub:
            def __init__(self):
                self.calls = 0

            def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
                _ = (time_window, namespace)
                self.calls += 1
                return {}

        storage = StorageStub()
        builder = HybridTopologyBuilder(storage)
        merged_edges = [
            {
                "source": "frontend",
                "target": "registry",
                "metrics": {
                    "data_source": "logs_heuristic",
                    "data_sources": ["logs"],
                },
            },
            {
                "source": "query-service",
                "target": "frontend",
                "metrics": {
                    "data_source": "inferred",
                    "data_sources": ["logs", "inferred"],
                },
            },
        ]

        builder._apply_edge_red_aggregation(merged_edges=merged_edges, time_window="1 HOUR", namespace="islap")
        assert storage.calls == 0

    def test_infer_edges_fallbacks_to_lightweight_logs_query_when_primary_empty(self):
        """主查询空但窗口内有日志时，应回退到轻量查询避免返回空拓扑。"""
        fallback_rows = [
            {
                "id": "log-2",
                "timestamp": datetime(2026, 3, 1, 12, 0, 2, tzinfo=timezone.utc),
                "service_name": "payment",
                "namespace": "islap",
                "message": "request_id=req-1 handled",
                "trace_id": "",
            },
            {
                "id": "log-1",
                "timestamp": datetime(2026, 3, 1, 12, 0, 1, tzinfo=timezone.utc),
                "service_name": "frontend",
                "namespace": "islap",
                "message": "request_id=req-1 calling payment",
                "trace_id": "",
            },
        ]

        class StorageStub:
            def __init__(self):
                self.ch_client = object()
                self.queries: List[str] = []

            def execute_query(self, query: str):
                condensed = " ".join(query.split())
                self.queries.append(condensed)
                if "SELECT count() AS cnt FROM logs.logs" in condensed:
                    return [{"cnt": 128}]
                if (
                    "FROM logs.logs" in condensed
                    and "ORDER BY timestamp DESC" in condensed
                    and "attributes_json" in condensed
                ):
                    return []
                if (
                    "FROM logs.logs" in condensed
                    and "ORDER BY timestamp DESC" in condensed
                    and "attributes_json" not in condensed
                ):
                    return fallback_rows
                if "FROM logs.traces" in condensed:
                    return []
                if "FROM logs.metrics" in condensed:
                    return []
                return []

            def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
                _ = (time_window, namespace)
                return {}

        storage = StorageStub()
        builder = HybridTopologyBuilder(storage)

        edges, stats = builder._infer_edges_from_logs(time_window="1 HOUR", namespace="islap")
        assert edges == []
        assert stats["total_candidates"] == 2

        assert any("SELECT count() AS cnt FROM logs.logs" in item for item in storage.queries)
        assert any(
            "FROM logs.logs" in item and "ORDER BY timestamp DESC" in item and "attributes_json" not in item
            for item in storage.queries
        )
        assert any(
            "FROM logs.logs" in item
            and "ORDER BY timestamp DESC" in item
            and "optimize_read_in_order = 1" in item
            for item in storage.queries
        )

    def test_traces_topology_uses_fast_node_aggregation_when_no_parent_relations(self):
        """当窗口内无 parent span 关系时，应走节点聚合快速路径并跳过重型明细扫描。"""
        class StorageStub:
            def __init__(self):
                self.ch_client = object()
                self.queries: List[str] = []

            def execute_query(self, query: str):
                condensed = " ".join(query.split())
                self.queries.append(condensed)
                if (
                    "FROM system.columns" in condensed
                    and "table = 'traces'" in condensed
                    and "name = 'traces_namespace'" in condensed
                ):
                    return [{"cnt": 1}]
                if (
                    "SELECT parent_span_id FROM logs.traces" in condensed
                    and "notEmpty(parent_span_id)" in condensed
                    and "LIMIT 1" in condensed
                ):
                    return []
                if "topK(1)(traces_namespace) AS namespace_top" in condensed:
                    return [
                        {
                            "service_name": "frontend",
                            "namespace_top": ["islap"],
                            "span_count": 20,
                            "avg_duration": 18.6,
                            "error_count": 1,
                            "trace_count": 5,
                        }
                    ]
                if "SELECT trace_id, span_id, parent_span_id" in condensed:
                    raise AssertionError("heavy traces detail query should not be executed")
                return []

            def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
                _ = (time_window, namespace)
                return {}

        storage = StorageStub()
        builder = HybridTopologyBuilder(storage)
        builder.ENABLE_TRACES_FAST_PATH = True

        result = builder._get_traces_topology(time_window="1 HOUR", namespace="islap")
        assert len(result.get("edges") or []) == 0
        nodes = result.get("nodes") or []
        assert len(nodes) == 1
        assert nodes[0]["id"] == "frontend"
        assert nodes[0]["metrics"]["namespace"] == "islap"
        assert nodes[0]["metrics"]["trace_count"] == 5

        assert any(
            "SELECT parent_span_id FROM logs.traces" in item and "LIMIT 1" in item
            for item in storage.queries
        )
        assert any("topK(1)(traces_namespace) AS namespace_top" in item for item in storage.queries)

    def test_time_window_fallback_records_are_capped(self):
        """time_window 回退推断应只处理最新上限记录，避免过大样本拖慢请求。"""
        class StorageStub:
            def __init__(self):
                self.ch_client = object()

            def execute_query(self, query: str):
                condensed = " ".join(query.split())
                if "FROM logs.logs" in condensed and "ORDER BY timestamp DESC" in condensed:
                    rows = []
                    base = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
                    for i in range(3000):
                        rows.append(
                            {
                                "id": f"log-{i}",
                                "timestamp": base,
                                "service_name": "frontend" if i % 2 == 0 else "payment",
                                "namespace": "islap",
                                "message": "plain log without request id",
                                "trace_id": "",
                                "attributes_json": "{}",
                            }
                        )
                    return rows
                if "FROM logs.traces" in condensed:
                    return []
                if "FROM logs.metrics" in condensed:
                    return []
                return []

            def get_edge_red_metrics(self, time_window: str = "1 HOUR", namespace: str = None):
                _ = (time_window, namespace)
                return {}

        storage = StorageStub()
        builder = HybridTopologyBuilder(storage)
        builder.MAX_TIME_WINDOW_FALLBACK_RECORDS = 1000

        observed = {"count": None}

        def _fake_accumulate_time_window_fallback_edges(**kwargs):
            observed["count"] = len(kwargs.get("fallback_records") or [])
            return 0

        with patch(
            "graph.hybrid_topology.hybrid_utils.accumulate_time_window_fallback_edges",
            side_effect=_fake_accumulate_time_window_fallback_edges,
        ):
            builder._infer_edges_from_logs(time_window="1 HOUR", namespace="islap")

        assert observed["count"] == 1000
