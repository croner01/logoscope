from typing import Dict, List, Optional, Any
import random
from .base import Projection


class VersionedProjectionRegistry:
    """
    Projection 版本注册表——支持灰度发布（traffic split）和版本比较。

    - add_version(projection, traffic=0.0): 添加版本
    - route(event): 根据流量分配选择版本
    - promote(version_name): 推广某版本到 100%
    - compare_results(): 比较各版本输出
    """

    def __init__(self, projection_type: str):
        self.projection_type = projection_type
        self._versions: Dict[str, Projection] = {}
        self._traffic: Dict[str, float] = {}
        self._results: Dict[str, List[Any]] = {}

    def add_version(self, projection: Projection, traffic: float = 0.0):
        self._versions[projection.name] = projection
        self._traffic[projection.name] = traffic
        self._results[projection.name] = []

    def route(self, event: Any) -> Optional[Projection]:
        """根据流量分配选择版本。"""
        if not self._versions:
            return None
        r = random.random() * 100
        cumulative = 0.0
        for name, traffic in sorted(self._traffic.items()):
            cumulative += traffic * 100
            if r < cumulative:
                return self._versions[name]
        return list(self._versions.values())[-1]

    def promote(self, version_name: str):
        """推广某版本到 100% 流量。"""
        for name in self._traffic:
            self._traffic[name] = 0.0
        self._traffic[version_name] = 1.0

    def apply_all(self, event: Any):
        """将 Event 应用到所有版本（用于结果比较）。"""
        for name, proj in self._versions.items():
            proj.apply(event)
            self._results[name].append(event)

    def compare_results(self) -> Dict[str, Any]:
        """返回各版本的输出结果供比较。"""
        return {
            name: {
                "status": proj.status(),
                "events": list(self._results[name]),
            }
            for name, proj in self._versions.items()
        }

    def get_version(self, name: str) -> Optional[Projection]:
        return self._versions.get(name)

    @property
    def active_versions(self) -> List[str]:
        return list(self._versions.keys())
