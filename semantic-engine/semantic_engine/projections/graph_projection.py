"""GraphProjection — 拓扑关系图（纯结构，不含状态）。"""
from typing import Dict, List, Set, Optional, Any
from collections import defaultdict, deque


class GraphProjection:
    """
    拓扑关系图投影——只维护节点和边的拓扑结构，不包含任何状态信息。

    - apply_entity(type, name, instance): 添加节点
    - apply_interaction(source_key, target_key): 添加边
    - get_subgraph(key): 获取子图
    - get_downstream(key): 下游依赖
    - get_upstream(key): 上游依赖
    - bfs_downstream(key, depth): BFS 影响集
    - find_path(from_key, to_key): 路径查询
    """

    def __init__(self):
        self._nodes: Dict[str, dict] = {}
        self._edges: Dict[str, Set[str]] = defaultdict(set)  # source -> {targets}
        self._reverse_edges: Dict[str, Set[str]] = defaultdict(set)  # target -> {sources}

    def apply_entity(self, entity_type: str, entity_name: str,
                     entity_instance: str) -> str:
        key = f"{entity_type}:{entity_name}"
        self._nodes[key] = {
            "entity_type": entity_type,
            "entity_name": entity_name,
            "entity_instance": entity_instance,
            "key": key,
        }
        return key

    def apply_interaction(self, source_key: str, target_key: str) -> None:
        self._edges[source_key].add(target_key)
        self._reverse_edges[target_key].add(source_key)

    def get_subgraph(self, key: str, depth: int = 2) -> dict:
        """获取以 key 为中心的子图。"""
        visited = set()
        queue = deque([(key, 0)])
        sub_nodes = []
        sub_edges = []

        while queue:
            current, d = queue.popleft()
            if current in visited or d > depth:
                continue
            visited.add(current)
            if current in self._nodes:
                sub_nodes.append(self._nodes[current])

            for neighbor in self._edges.get(current, set()):
                sub_edges.append({"source": current, "target": neighbor})
                if neighbor not in visited:
                    queue.append((neighbor, d + 1))

            for neighbor in self._reverse_edges.get(current, set()):
                sub_edges.append({"source": neighbor, "target": current})
                if neighbor not in visited:
                    queue.append((neighbor, d + 1))

        return {"nodes": sub_nodes, "edges": sub_edges}

    def get_downstream(self, key: str) -> List[str]:
        """谁依赖此节点。"""
        return list(self._edges.get(key, set()))

    def get_upstream(self, key: str) -> List[str]:
        """此节点依赖谁。"""
        return list(self._reverse_edges.get(key, set()))

    def bfs_downstream(self, key: str, depth: int = 3) -> List[List[str]]:
        """按 BFS 层返回下游影响集合。"""
        visited = {key}
        current_layer = {key}
        layers = []

        for _ in range(depth):
            next_layer = set()
            for node in current_layer:
                for neighbor in self._edges.get(node, set()):
                    if neighbor not in visited:
                        next_layer.add(neighbor)
                        visited.add(neighbor)
            if not next_layer:
                break
            layers.append(list(next_layer))
            current_layer = next_layer

        return layers

    def find_path(self, from_key: str, to_key: str) -> List[str]:
        """BFS 查路径。"""
        if from_key not in self._nodes or to_key not in self._nodes:
            return []

        queue = deque([[from_key]])
        visited = {from_key}

        while queue:
            path = queue.popleft()
            current = path[-1]

            for neighbor in self._edges.get(current, set()):
                if neighbor == to_key:
                    return path + [neighbor]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(path + [neighbor])

        return []

    def estimate_vm_count(self, key: str, max_depth: int = 3) -> int:
        """估算影响范围包含多少 VM。"""
        affected = set()
        for layer in self.bfs_downstream(key, max_depth):
            for node_key in layer:
                if node_key.startswith("INSTANCE:"):
                    affected.add(node_key)
        return len(affected)
