"""CapabilityStatistics — 能力执行统计。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CapabilityStatistics:
    """
    能力执行统计。

    - success_rate: 动态计算（success_count / total_executions）
    - avg_recovery_time_ms: 失败后的平均恢复时间
    """
    capability_id: str = ""
    total_executions: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_recovery_time_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_executions if self.total_executions else 0.0
