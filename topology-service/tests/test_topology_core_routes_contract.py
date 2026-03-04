"""
Topology 核心路由契约测试（TS-04）

覆盖：
1) /api/v1/topology/hybrid
2) /api/v1/topology/stats
3) /api/v1/monitor/topology
4) /ws/topology（websocket 协议）
"""
import json
import os
import sys
from collections import deque
from typing import Any, Dict, List

import pytest
from fastapi import WebSocketDisconnect

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import monitor_topology, topology_routes, websocket as ws_api


class FakeStorageForStats:
    """topology stats 存储桩。"""

    def execute_neo4j_query(self, query: str) -> List[Dict[str, Any]]:
        if "count(n) as total_nodes" in query:
            return [{"total_nodes": 8}]
        if "count(r) as total_edges" in query:
            return [{"total_edges": 13}]
        return []

    def execute_query(self, query: str) -> List[Dict[str, Any]]:
        if "COUNT(DISTINCT service_name) as service_count" in query:
            return [{"service_count": 5}]
        return []


class FakeHybridBuilder:
    """hybrid builder 测试桩。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def build_topology(
        self,
        time_window: str,
        namespace: str = None,
        confidence_threshold: float = 0.3,
        inference_mode: str = None,
        message_target_enabled: bool = None,
        message_target_patterns: Any = None,
        message_target_min_support: int = None,
        message_target_max_per_log: int = None,
    ) -> Dict[str, Any]:
        self.calls.append({
            "time_window": time_window,
            "namespace": namespace,
            "confidence_threshold": confidence_threshold,
            "inference_mode": inference_mode,
            "message_target_enabled": message_target_enabled,
            "message_target_patterns": message_target_patterns,
            "message_target_min_support": message_target_min_support,
            "message_target_max_per_log": message_target_max_per_log,
        })
        return {
            "nodes": [
                {
                    "id": "frontend",
                    "label": "frontend",
                    "type": "service",
                    "metrics": {
                        "log_count": 120,
                        "error_count": 2,
                        "quality_score": 76,
                        "error_rate": 0.03,
                        "instance_count": 2,
                        "healthy_instance_count": 1,
                    },
                },
                {
                    "id": "payment",
                    "label": "payment",
                    "type": "service",
                    "metrics": {
                        "log_count": 80,
                        "error_count": 0,
                        "quality_score": 98,
                        "error_rate": 0.0,
                        "instance_count": 2,
                        "healthy_instance_count": 2,
                    },
                },
            ],
            "edges": [
                {
                    "id": "frontend-payment",
                    "source": "frontend",
                    "target": "payment",
                    "metrics": {
                        "error_rate": 0.07,
                        "timeout_rate": 0.03,
                        "p95": 320,
                        "p99": 820,
                        "quality_score": 68,
                        "call_count": 180,
                        "avg_duration": 56,
                        "p99_latency_ms": 820,
                        "evidence_type": "observed",
                    },
                }
            ],
            "metadata": {
                "avg_confidence": 0.81,
                "source_breakdown": {
                    "traces": {"nodes": 2, "edges": 1},
                    "logs": {"nodes": 2, "edges": 1},
                    "metrics": {"nodes": 0, "edges": 0},
                },
            },
        }


class FakeEnhancedBuilder:
    """enhanced builder 测试桩。"""

    def build_topology(
        self,
        time_window: str,
        namespace: str = None,
        confidence_threshold: float = 0.3,
    ) -> Dict[str, Any]:
        _ = (time_window, namespace, confidence_threshold)
        return {
            "nodes": [
                {
                    "id": "frontend",
                    "label": "Frontend",
                    "type": "service",
                    "metrics": {
                        "qps": 32,
                        "avg_duration": 40,
                        "error_rate": 0.02,
                        "instance_count": 2,
                        "healthy_instance_count": 2,
                        "log_count": 200,
                        "trace_count": 20,
                        "span_count": 200,
                    },
                },
                {
                    "id": "payment",
                    "label": "Payment",
                    "type": "service",
                    "metrics": {
                        "qps": 24,
                        "avg_duration": 55,
                        "error_rate": 0.0,
                        "instance_count": 2,
                        "healthy_instance_count": 2,
                        "log_count": 180,
                        "trace_count": 18,
                        "span_count": 180,
                    },
                },
            ],
            "edges": [
                {
                    "id": "frontend-payment",
                    "source": "frontend",
                    "target": "payment",
                    "type": "calls",
                    "metrics": {
                        "call_count": 300,
                        "avg_duration": 64,
                        "p99_latency_ms": 1200,
                        "error_rate": 0.03,
                    },
                }
            ],
        }


class FakeWebSocket:
    """websocket 测试桩。"""

    def __init__(self, incoming_messages: List[str]):
        self.incoming_messages = deque(incoming_messages)
        self.sent_messages: List[Dict[str, Any]] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, text: str):
        self.sent_messages.append(json.loads(text))

    async def receive_text(self) -> str:
        if self.incoming_messages:
            return self.incoming_messages.popleft()
        raise WebSocketDisconnect(code=1000)


@pytest.fixture(autouse=True)
def reset_modules_state():
    topology_routes.set_storage_and_builders(None, None, None)
    monitor_topology.set_storage_adapter(None)
    ws_api.topology_manager.active_connections.clear()
    ws_api.topology_manager._subscriptions.clear()
    ws_api.topology_manager._last_topology_data = None
    ws_api.topology_manager._last_topology_hash = None
    yield
    topology_routes.set_storage_and_builders(None, None, None)
    monitor_topology.set_storage_adapter(None)
    ws_api.topology_manager.active_connections.clear()
    ws_api.topology_manager._subscriptions.clear()
    ws_api.topology_manager._last_topology_data = None
    ws_api.topology_manager._last_topology_hash = None


@pytest.mark.asyncio
async def test_topology_hybrid_contract_fields():
    """hybrid 路由应输出核心 contract 字段。"""
    storage = FakeStorageForStats()
    hybrid_builder = FakeHybridBuilder()
    topology_routes.set_storage_and_builders(storage, hybrid_builder, FakeEnhancedBuilder())

    result = await topology_routes.get_hybrid_topology(
        time_window="1 HOUR",
        namespace="prod",
        confidence_threshold=0.4,
        inference_mode="hybrid_score",
    )

    assert "nodes" in result and "edges" in result and "metadata" in result
    assert result["metadata"]["contract_version"] == "topology-schema-v1"
    assert result["metadata"]["quality_version"] == "quality-score-v1"
    assert "issue_summary" in result["metadata"]
    assert isinstance(result["metadata"]["issue_summary"].get("top_problem_edges"), list)
    assert "problem_summary" in result["nodes"][0]
    assert "problem_summary" in result["edges"][0]
    assert hybrid_builder.calls[0]["namespace"] == "prod"
    assert hybrid_builder.calls[0]["inference_mode"] == "hybrid_score"


@pytest.mark.asyncio
async def test_topology_stats_contract_fields():
    """stats 路由应返回稳定核心字段。"""
    storage = FakeStorageForStats()
    topology_routes.set_storage_and_builders(storage, FakeHybridBuilder(), FakeEnhancedBuilder())

    result = await topology_routes.get_topology_stats(time_window="1 HOUR")

    # 当前 stats 路由优先复用 hybrid builder 口径
    assert result["total_nodes"] == 2
    assert result["total_edges"] == 1
    assert result["service_count"] == 2
    assert result["time_window"] == "1 HOUR"


@pytest.mark.asyncio
async def test_monitor_topology_contract_fields(monkeypatch: pytest.MonkeyPatch):
    """monitor 路由应返回前端可直接消费字段。"""
    monitor_topology.set_storage_adapter(object())
    monkeypatch.setattr(
        monitor_topology,
        "get_enhanced_topology_builder",
        lambda _storage: FakeEnhancedBuilder(),
    )

    result = await monitor_topology.get_monitor_topology(
        time_window="5 MINUTE",
        namespace=None,
        include_metrics=True,
        auto_refresh=False,
    )

    assert result["layout"]["type"] == "swimlane"
    assert isinstance(result["nodes"], list) and len(result["nodes"]) >= 1
    assert isinstance(result["edges"], list) and len(result["edges"]) >= 1
    node = result["nodes"][0]
    edge = result["edges"][0]
    assert {"id", "label", "layer", "status", "color", "metrics"}.issubset(node.keys())
    assert {"id", "source", "target", "width", "color", "metrics"}.issubset(edge.keys())
    assert "node_count" in result["metadata"] and "edge_count" in result["metadata"]


@pytest.mark.asyncio
async def test_ws_topology_contract_messages():
    """ws/topology 应支持 connected/pong/subscribed/topology_update 消息契约。"""
    builder = FakeHybridBuilder()
    ws = FakeWebSocket(
        incoming_messages=[
            json.dumps({"action": "ping"}),
            json.dumps({
                "action": "subscribe",
                "params": {
                    "time_window": "15 MINUTE",
                    "namespace": "prod",
                    "confidence_threshold": 0.6,
                    "inference_mode": "hybrid_score",
                },
            }),
            json.dumps({"action": "get"}),
        ]
    )

    await ws_api.topology_websocket_endpoint(ws, builder)

    assert ws.accepted is True
    message_types = [item.get("type") for item in ws.sent_messages]
    assert "connected" in message_types
    assert "pong" in message_types
    assert "subscribed" in message_types
    assert "topology_update" in message_types

    # subscribe 后 get 应使用新参数
    assert len(builder.calls) >= 1
    assert builder.calls[-1]["time_window"] == "15 MINUTE"
    assert builder.calls[-1]["namespace"] == "prod"
    assert float(builder.calls[-1]["confidence_threshold"]) == pytest.approx(0.6)
    assert builder.calls[-1]["inference_mode"] == "hybrid_score"

    # 断开后应清理连接状态
    assert len(ws_api.topology_manager.active_connections) == 0
