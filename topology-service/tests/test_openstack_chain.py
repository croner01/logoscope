"""
Tests for GET /api/v1/topology/openstack-chain endpoint
"""
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.hybrid_topology import HybridTopologyBuilder


class FakeOpenstackStorage:
    """存储桩：返回模拟的 openstack global_request_id 数据"""
    def __init__(self):
        self.ch_client = object()

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        condensed = " ".join(query.split())
        if "openstack_global_request_id" not in condensed:
            return []

        return [
            {
                "service_name": "nova-api",
                "openstack_request_id": "req-aaa",
                "openstack_global_request_id": "req-global-1",
                "timestamp": datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc),
            },
            {
                "service_name": "nova-compute",
                "openstack_request_id": "req-bbb",
                "openstack_global_request_id": "req-global-1",
                "timestamp": datetime(2026, 6, 24, 10, 0, 1, tzinfo=timezone.utc),
            },
            {
                "service_name": "cinder-volume",
                "openstack_request_id": "req-ccc",
                "openstack_global_request_id": "req-global-1",
                "timestamp": datetime(2026, 6, 24, 10, 0, 2, tzinfo=timezone.utc),
            },
        ]


class TestOpenstackChainEndpoint:
    """测试 openstack-chain 端点后端逻辑"""

    def test_get_openstack_topology_creates_edges(self):
        """测试 _get_openstack_topology 从模拟数据生成正确的边"""
        builder = HybridTopologyBuilder(FakeOpenstackStorage())
        result = builder._get_openstack_topology("1 HOUR")

        assert len(result["nodes"]) == 3
        assert len(result["edges"]) == 2

        # 检查边
        edge_pairs = {(e["source"], e["target"]): e for e in result["edges"]}
        assert ("nova-api", "nova-compute") in edge_pairs
        assert ("nova-compute", "cinder-volume") in edge_pairs

        # 检查边指标
        edge = edge_pairs[("nova-api", "nova-compute")]
        assert edge["metrics"]["data_source"] == "openstack"
        assert edge["metrics"]["confidence"] == 0.6
        assert edge["metrics"]["evidence_type"] == "observed"

    def test_get_openstack_topology_empty(self):
        """测试无数据时返回空"""
        builder = HybridTopologyBuilder(FakeOpenstackStorage())
        # 修改 SQL 返回空
        builder.storage.execute_query = Mock(return_value=[])
        result = builder._get_openstack_topology("1 HOUR")
        assert result == {"nodes": [], "edges": []}

    def test_get_openstack_topology_skip_same_service(self):
        """测试连续同服务名不生成边"""
        class SameSvcStorage:
            def __init__(self):
                self.ch_client = object()
            def execute_query(self, query):
                return [
                    {"service_name": "nova-api", "openstack_request_id": "req-1",
                     "openstack_global_request_id": "req-global-1",
                     "timestamp": datetime(2026, 6, 24, 10, 0, 0, tzinfo=timezone.utc)},
                    {"service_name": "nova-api", "openstack_request_id": "req-2",
                     "openstack_global_request_id": "req-global-1",
                     "timestamp": datetime(2026, 6, 24, 10, 0, 1, tzinfo=timezone.utc)},
                ]

        builder = HybridTopologyBuilder(SameSvcStorage())
        result = builder._get_openstack_topology("1 HOUR")
        assert len(result["edges"]) == 0


class TestOpenstackChainEndpointIntegration:
    """测试 /api/v1/topology/openstack-chain 端点的数据组装逻辑"""

    def test_sanitize_interval_validates_and_defaults(self):
        """测试 _sanitize_interval 正确规范化时间窗口参数"""
        from api.topology_routes import _sanitize_interval

        # 测试 sanitize 函数
        assert _sanitize_interval("1 HOUR") == "1 HOUR"
        assert _sanitize_interval("invalid") == "1 HOUR"
