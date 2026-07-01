import pytest
import json
from shared_src.event.envelope import EventEnvelope
from semantic_engine.projections.graph_projection import GraphProjection


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"
HOST = "HOST"


class TestGraphProjection:
    def test_graph_no_state(self):
        """Graph 只包含拓扑关系，不包含状态"""
        graph = GraphProjection()
        graph.apply_entity("SERVICE", "nova-api", "abc-123")
        graph.apply_interaction("SERVICE:nova-api", "HOST:compute-01")

        subgraph = graph.get_subgraph("SERVICE:nova-api")
        assert len(subgraph["nodes"]) >= 1
        # 没有 status 属性
        for node in subgraph["nodes"]:
            assert "status" not in node

    def test_get_dependents_depth_1(self):
        graph = GraphProjection()
        graph.apply_entity("SERVICE", "rabbitmq", "mq-01")
        graph.apply_interaction("SERVICE:nova-api", "SERVICE:rabbitmq")
        graph.apply_interaction("SERVICE:neutron-server", "SERVICE:rabbitmq")

        deps = graph.get_downstream("SERVICE:nova-api")
        assert "SERVICE:rabbitmq" in deps

    def test_get_upstream(self):
        """查询谁依赖此资源"""
        graph = GraphProjection()
        graph.apply_entity("SERVICE", "rabbitmq", "mq-01")
        graph.apply_interaction("SERVICE:nova-api", "SERVICE:rabbitmq")
        graph.apply_interaction("SERVICE:neutron-server", "SERVICE:rabbitmq")

        upstream = graph.get_upstream("SERVICE:rabbitmq")
        assert "SERVICE:nova-api" in upstream
        assert "SERVICE:neutron-server" in upstream

    def test_bfs_downstream(self):
        """BFS 影响集查询"""
        graph = GraphProjection()
        entities = [("SERVICE", "svc-A"), ("SERVICE", "svc-B"), ("SERVICE", "svc-C"), ("SERVICE", "svc-D")]
        for t, n in entities:
            graph.apply_entity(t, n, "")
        graph.apply_interaction("SERVICE:svc-A", "SERVICE:svc-B")
        graph.apply_interaction("SERVICE:svc-B", "SERVICE:svc-C")
        graph.apply_interaction("SERVICE:svc-C", "SERVICE:svc-D")

        layers = graph.bfs_downstream("SERVICE:svc-A", depth=3)
        # 应该看到 B, C, D
        assert "SERVICE:svc-B" in layers[0] if len(layers) > 0 else False

    def test_find_path(self):
        graph = GraphProjection()
        graph.apply_entity("SERVICE", "A", "")
        graph.apply_entity("SERVICE", "B", "")
        graph.apply_entity("SERVICE", "C", "")
        graph.apply_interaction("SERVICE:A", "SERVICE:B")
        graph.apply_interaction("SERVICE:B", "SERVICE:C")

        path = graph.find_path("SERVICE:A", "SERVICE:C")
        assert len(path) == 3  # A -> B -> C

    def test_graph_no_status_in_nodes(self):
        """强制验证：Graph 不维护状态"""
        graph = GraphProjection()
        graph.apply_entity("SERVICE", "svc", "i1")
        subgraph = graph.get_subgraph("SERVICE:svc")
        for node in subgraph["nodes"]:
            for attr in ["status", "state", "health"]:
                assert attr not in node
