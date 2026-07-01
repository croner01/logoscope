"""TopologyQuery — 拓扑查询接口（WorldView 的查询组件之一）。"""
from typing import List, Optional


class TopologyQuery:
    """
    拓扑查询——查询资源的依赖关系和影响范围。

    - get_dependents: 谁依赖此资源
    - get_dependencies: 此资源依赖谁
    - get_impact_set: BFS 影响集（按层级返回）
    - query_path: 两个资源间的通路
    - estimate_vm_count: 影响范围内的 VM 数量估算
    """

    def __init__(self, graph_projection):
        self.graph = graph_projection

    def _key(self, entity_type: str, entity_name: str) -> str:
        return f"{entity_type}:{entity_name}"

    def get_dependents(self, entity_type: str, entity_name: str) -> List[str]:
        """谁依赖此资源。"""
        return self.graph.get_downstream(self._key(entity_type, entity_name))

    def get_dependencies(self, entity_type: str, entity_name: str) -> List[str]:
        """此资源依赖谁。"""
        return self.graph.get_upstream(self._key(entity_type, entity_name))

    def get_impact_set(self, entity_type: str, entity_name: str,
                       depth: int = 3) -> List[List[str]]:
        """按 BFS 层返回影响集合。"""
        return self.graph.bfs_downstream(
            self._key(entity_type, entity_name), depth
        )

    def query_path(self, from_type: str, from_name: str,
                   to_type: str, to_name: str) -> List[str]:
        """查询两个资源间的通路。"""
        return self.graph.find_path(
            self._key(from_type, from_name),
            self._key(to_type, to_name),
        )

    def estimate_vm_count(self, entity_type: str, entity_name: str,
                          max_depth: int = 3) -> int:
        """估算影响范围包含多少 VM。"""
        return self.graph.estimate_vm_count(
            self._key(entity_type, entity_name), max_depth
        )
