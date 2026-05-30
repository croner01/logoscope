"""Backward-compatible topology manager shim for legacy tests."""

import time
import uuid
from typing import Any, Dict, Optional

from graph.hybrid_topology import get_hybrid_topology_builder


class TopologyManager:
    """兼容旧接口的拓扑管理器。"""

    def __init__(self, storage_adapter: Any = None):
        self.storage = storage_adapter

    async def get_hybrid_topology(
        self,
        time_window: str = "1 HOUR",
        namespace: Optional[str] = None,
        confidence_threshold: float = 0.3,
    ) -> Dict[str, Any]:
        builder = get_hybrid_topology_builder(self.storage)
        if builder is None:
            return {"nodes": [], "edges": [], "metadata": {}}
        topology = builder.build_topology(
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
        )
        if not isinstance(topology, dict):
            return {"nodes": [], "edges": [], "metadata": {}}
        topology.setdefault("nodes", [])
        topology.setdefault("edges", [])
        topology.setdefault("metadata", {})
        return topology

    async def create_snapshot(
        self,
        name: Optional[str] = None,
        time_window: str = "1 HOUR",
        namespace: Optional[str] = None,
        confidence_threshold: float = 0.3,
    ) -> str:
        # 兼容历史行为：即使无持久化后端，也返回可追踪的 snapshot id。
        _ = await self.get_hybrid_topology(
            time_window=time_window,
            namespace=namespace,
            confidence_threshold=confidence_threshold,
        )
        if name:
            return str(name)
        return f"snap_{int(time.time())}_{uuid.uuid4().hex[:8]}"

