"""
Tests for graph/topology_contract.py
"""
import os
import sys

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.topology_contract import (
    apply_edge_contract,
    apply_node_contract,
    build_edge_key,
    build_node_key,
    infer_env,
    normalize_endpoint_pattern,
)


class TestTopologyContract:
    """统一拓扑契约测试。"""

    def test_node_key_and_env(self):
        """Node key 应符合 namespace:name:env。"""
        env = infer_env("prod-order")
        node_key = build_node_key("prod-order", "checkout-service", env)
        assert env == "prod"
        assert node_key == "prod-order:checkout-service:prod"

    def test_edge_key_contains_protocol_and_endpoint(self):
        """Edge key 应包含 protocol 和 endpoint_pattern。"""
        key = build_edge_key(
            "prod:frontend:prod",
            "prod:backend:prod",
            "http",
            "/api/orders/:id",
        )
        assert key == "prod:frontend:prod|prod:backend:prod|http|/api/orders/:id"

    def test_normalize_endpoint_pattern_replaces_dynamic_segments(self):
        """路径中的数字/长 ID 应被归一化为 :id。"""
        pattern = normalize_endpoint_pattern("GET /api/orders/123/items/8fd0a1b23")
        assert pattern == "/api/orders/:id/items/:id"

    def test_apply_contract_fields(self):
        """契约转换后应输出核心字段。"""
        node = {
            "id": "checkout",
            "label": "checkout",
            "type": "service",
            "name": "checkout",
            "metrics": {
                "data_source": "traces",
                "confidence": 0.9,
                "trace_count": 20,
                "log_count": 200,
            },
        }
        node = apply_node_contract(node)
        assert node["node_key"] == "default:checkout:prod"
        assert node["service"]["name"] == "checkout"
        assert node["evidence_type"] == "observed"
        assert "coverage" in node
        assert "quality_score" in node

        edge = {
            "id": "checkout-payment",
            "source": "checkout",
            "target": "payment",
            "label": "GET /api/pay/123",
            "type": "calls",
            "metrics": {
                "data_source": "traces",
                "confidence": 0.85,
                "error_rate": 0.02,
                "p95": 120,
                "p99": 200,
                "timeout_rate": 0.01,
            },
        }
        edge = apply_edge_contract(edge, source_node=node, target_node=node)
        assert "edge_key" in edge
        assert edge["protocol"] == "http"
        assert edge["endpoint_pattern"] == "/api/pay/:id"
        assert edge["evidence_type"] == "observed"
        assert edge["quality_score"] >= 0
