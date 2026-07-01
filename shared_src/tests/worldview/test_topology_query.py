import pytest
from shared_src.worldview.topology_query import TopologyQuery


class MockGraphProjection:
    """模拟 GraphProjection"""
    def __init__(self):
        self._nodes = {}
        self._edges = {}

    def apply_entity(self, type_, name, instance=""):
        key = f"{type_}:{name}"
        self._nodes[key] = {"entity_type": type_, "entity_name": name, "key": key}
        return key

    def apply_interaction(self, source, target):
        if source not in self._edges:
            self._edges[source] = set()
        self._edges[source].add(target)

    def get_downstream(self, key):
        return list(self._edges.get(key, set()))

    def get_upstream(self, key):
        upstream = set()
        for src, targets in self._edges.items():
            if key in targets:
                upstream.add(src)
        return list(upstream)

    def bfs_downstream(self, key, depth=3):
        visited = {key}
        current = {key}
        layers = []
        for _ in range(depth):
            next_layer = set()
            for node in current:
                for nbr in self._edges.get(node, set()):
                    if nbr not in visited:
                        next_layer.add(nbr)
                        visited.add(nbr)
            if not next_layer:
                break
            layers.append(list(next_layer))
            current = next_layer
        return layers

    def find_path(self, from_key, to_key):
        return [from_key, "HOST:compute-01", to_key] if from_key and to_key else []

    def estimate_vm_count(self, key, max_depth=3):
        return 5


class TestTopologyQuery:
    def test_get_dependents(self):
        graph = MockGraphProjection()
        graph.apply_entity("SERVICE", "rabbitmq")
        graph.apply_entity("SERVICE", "nova-api")
        graph.apply_interaction("SERVICE:nova-api", "SERVICE:rabbitmq")

        tq = TopologyQuery(graph)
        deps = tq.get_dependents("SERVICE", "nova-api")
        assert "SERVICE:rabbitmq" in deps

    def test_get_dependencies(self):
        graph = MockGraphProjection()
        graph.apply_entity("SERVICE", "nova-api")
        graph.apply_entity("SERVICE", "rabbitmq")
        graph.apply_interaction("SERVICE:nova-api", "SERVICE:rabbitmq")

        tq = TopologyQuery(graph)
        deps = tq.get_dependencies("SERVICE", "rabbitmq")
        assert "SERVICE:nova-api" in deps

    def test_get_impact_set_bfs_layers(self):
        graph = MockGraphProjection()
        graph.apply_entity("SERVICE", "A")
        graph.apply_entity("SERVICE", "B")
        graph.apply_entity("SERVICE", "C")
        graph.apply_interaction("SERVICE:A", "SERVICE:B")
        graph.apply_interaction("SERVICE:B", "SERVICE:C")

        tq = TopologyQuery(graph)
        layers = tq.get_impact_set("SERVICE", "A", depth=3)
        assert len(layers) >= 1
        assert len(layers) <= 3

    def test_query_path(self):
        graph = MockGraphProjection()
        tq = TopologyQuery(graph)
        path = tq.query_path("INSTANCE", "vm-1", "HOST", "compute-01")
        assert isinstance(path, list)

    def test_estimate_vm_count(self):
        graph = MockGraphProjection()
        tq = TopologyQuery(graph)
        count = tq.estimate_vm_count("SERVICE", "rabbitmq")
        assert count >= 0

    def test_topology_query_interface(self):
        """TopologyQuery 只包含拓扑查询方法"""
        tq = TopologyQuery(MockGraphProjection())
        assert hasattr(tq, "get_dependents")
        assert hasattr(tq, "get_dependencies")
        assert hasattr(tq, "get_impact_set")
        assert hasattr(tq, "query_path")
        # 不包含状态查询方法
        assert not hasattr(tq, "get_state")
        assert not hasattr(tq, "get_timeline")
