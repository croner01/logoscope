"""ImpactModel — 影响模型（Blast Radius 用）。"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class ImpactModel:
    """
    影响模型——描述 Capability 执行的影响类型。

    severity: "temporary", "permanent", "degradation"
    duration: "30s", "5min", "permanent"
    scope: "service", "instance", "data", "network"
    """
    severity: str = "temporary"
    duration: str = "30s"
    scope: str = "service"
