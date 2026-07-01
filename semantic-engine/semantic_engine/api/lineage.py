"""Lineage API — 追踪 Event 的血缘链。"""
from dataclasses import dataclass, field
from typing import List, Optional, Set, Dict
from shared_src.event.envelope import EventEnvelope
from shared_src.event.raw_event_store import RawEventStore


@dataclass
class LineageNode:
    """血缘节点——代表一个 Event。"""
    event_id: str
    event_type: str = ""
    producer: str = ""
    parent_event_ids: List[str] = field(default_factory=list)


@dataclass
class LineageEdge:
    """血缘边——parent → child。"""
    source_id: str
    target_id: str


@dataclass
class LineageGraph:
    """完整的血缘 DAG。"""
    root_event_id: str
    nodes: List[LineageNode]
    edges: List[LineageEdge]


class LineageAPI:
    """
    Lineage API——通过 parent_event_ids 追踪血缘链。

    - trace(event_id) → LineageGraph（完整的血缘 DAG）
    - trace_to_root(event_id) → 从事件到根事件的路径
    """

    def __init__(self, event_store: RawEventStore):
        self.event_store = event_store

    def trace(self, event_id: str) -> Optional[LineageGraph]:
        """追踪一个 Event 的完整血缘链。"""
        target = self.event_store.read(event_id)
        if not target:
            return None

        visited: Set[str] = set()
        nodes: Dict[str, LineageNode] = {}
        edges: List[LineageEdge] = []
        queue = [target]

        while queue:
            event = queue.pop(0)
            if event.event_id in visited:
                continue
            visited.add(event.event_id)

            nodes[event.event_id] = LineageNode(
                event_id=event.event_id,
                event_type=event.event_type,
                producer=event.producer,
                parent_event_ids=list(event.parent_event_ids),
            )

            for parent_id in event.parent_event_ids:
                edges.append(LineageEdge(
                    source_id=parent_id,
                    target_id=event.event_id,
                ))
                parent = self.event_store.read(parent_id)
                if parent and parent.event_id not in visited:
                    queue.append(parent)

        if not nodes:
            return None

        # 找根节点（没有 parent 的节点）
        all_parents = set()
        for e in edges:
            all_parents.add(e.source_id)
        children = set(nodes.keys())
        roots = all_parents - children
        root_id = list(roots)[0] if roots else event_id

        return LineageGraph(
            root_event_id=root_id,
            nodes=list(nodes.values()),
            edges=edges,
        )
