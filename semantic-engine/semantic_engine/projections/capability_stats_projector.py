"""CapabilityStatsProjector — 从 EvaluationEvent 构建 Capability 统计。"""
from typing import Dict, Optional
from shared_src.capability.stats import CapabilityStatistics


class CapabilityStatsProjector:
    """
    Capability 统计投影——从执行结果构建统计。

    - record_execution(capability_id, outcome, duration_ms)
    - get_stats(capability_id) -> CapabilityStatistics
    """

    def __init__(self):
        self._stats: Dict[str, CapabilityStatistics] = {}

    def record_execution(self, capability_id: str, outcome: str,
                         duration_ms: int) -> None:
        """记录一次执行结果。"""
        if capability_id not in self._stats:
            self._stats[capability_id] = CapabilityStatistics(
                capability_id=capability_id,
            )
        stats = self._stats[capability_id]
        stats.total_executions += 1
        if outcome == "success":
            stats.success_count += 1
        else:
            stats.failure_count += 1
            # 更新平均恢复时间
            if stats.failure_count == 1:
                stats.avg_recovery_time_ms = float(duration_ms)
            else:
                stats.avg_recovery_time_ms = (
                    stats.avg_recovery_time_ms * (stats.failure_count - 1) + duration_ms
                ) / stats.failure_count

    def get_stats(self, capability_id: str) -> Optional[CapabilityStatistics]:
        return self._stats.get(capability_id)
