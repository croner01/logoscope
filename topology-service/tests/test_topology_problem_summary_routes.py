"""
Topology routes 问题摘要字段测试（TS-02）
"""
import os
import sys
from typing import Any, Dict

import pytest

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import topology_routes


class FakeHybridBuilder:
    """hybrid builder 测试桩。"""

    def build_topology(
        self,
        time_window: str,
        namespace: str = None,
        confidence_threshold: float = 0.3,
        inference_mode: str = None,
        **_: Any,
    ) -> Dict[str, Any]:
        _ = (time_window, namespace, confidence_threshold, inference_mode)
        return {
            "nodes": [
                {
                    "id": "frontend",
                    "label": "frontend",
                    "type": "service",
                    "metrics": {
                        "error_count": 3,
                        "error_rate": 0.06,
                        "log_count": 1800,
                        "quality_score": 62,
                    },
                },
                {
                    "id": "payment",
                    "label": "payment",
                    "type": "service",
                    "metrics": {
                        "error_count": 0,
                        "error_rate": 0.0,
                        "log_count": 80,
                        "quality_score": 96,
                    },
                },
            ],
            "edges": [
                {
                    "id": "frontend-payment",
                    "source": "frontend",
                    "target": "payment",
                    "metrics": {
                        "error_rate": 0.09,
                        "timeout_rate": 0.06,
                        "p95": 840,
                        "p99": 1600,
                        "quality_score": 58,
                        "evidence_type": "observed",
                    },
                },
                {
                    "id": "payment-redis",
                    "source": "payment",
                    "target": "redis",
                    "metrics": {
                        "error_rate": 0.0,
                        "timeout_rate": 0.0,
                        "p95": 20,
                        "p99": 30,
                        "quality_score": 99,
                        "evidence_type": "observed",
                    },
                },
            ],
            "metadata": {
                "generated_by": "fake",
            },
        }


class FakeEnhancedBuilder:
    """enhanced builder 测试桩。"""

    def build_topology(self, time_window: str, namespace: str = None) -> Dict[str, Any]:
        _ = (time_window, namespace)
        return {
            "nodes": [
                {
                    "id": "legacy-gateway",
                    "label": "legacy-gateway",
                    "type": "service",
                    "metrics": {"error_count": 1, "error_rate": 0.03, "quality_score": 78, "log_count": 600},
                }
            ],
            "edges": [
                {
                    "id": "legacy-gateway-legacy-order",
                    "source": "legacy-gateway",
                    "target": "legacy-order",
                    "metrics": {"error_rate": 0.01, "timeout_rate": 0.03, "p99": 700, "quality_score": 82},
                }
            ],
        }


@pytest.fixture(autouse=True)
def reset_topology_router_state():
    topology_routes.set_storage_and_builders(None, None, None)
    yield
    topology_routes.set_storage_and_builders(None, None, None)


@pytest.mark.asyncio
async def test_hybrid_topology_contains_problem_summary_fields():
    """hybrid 输出应包含 node/edge 问题摘要与 metadata.issue_summary。"""
    topology_routes.set_storage_and_builders(None, FakeHybridBuilder(), FakeEnhancedBuilder())

    result = await topology_routes.get_hybrid_topology(time_window="1 HOUR", namespace=None, confidence_threshold=0.3)

    assert "nodes" in result and "edges" in result and "metadata" in result
    assert "issue_summary" in result["metadata"]
    assert result["metadata"]["issue_summary"]["unhealthy_nodes"] >= 1
    assert result["metadata"]["issue_summary"]["unhealthy_edges"] >= 1

    node = result["nodes"][0]
    assert "problem_summary" in node
    assert node["problem_summary"]["risk_level"] in {"高风险", "中风险", "低风险"}
    assert isinstance(node["problem_summary"]["issue_score"], float)
    assert "problem_summary" in node["metrics"]

    edge = result["edges"][0]
    assert "problem_summary" in edge
    assert edge["problem_summary"]["has_issue"] is True
    assert edge["problem_summary"]["risk_level"] in {"高风险", "中风险", "低风险"}
    assert "problem_summary" in edge["metrics"]


@pytest.mark.asyncio
async def test_enhanced_topology_contains_issue_summary():
    """enhanced 输出 metadata 应包含 issue_summary。"""
    topology_routes.set_storage_and_builders(None, FakeHybridBuilder(), FakeEnhancedBuilder())

    result = await topology_routes.get_enhanced_topology(time_window="1 HOUR", namespace=None)

    assert "metadata" in result
    assert "issue_summary" in result["metadata"]
    issue_summary = result["metadata"]["issue_summary"]
    assert issue_summary["unhealthy_nodes"] >= 1
    assert "top_problem_edges" in issue_summary
