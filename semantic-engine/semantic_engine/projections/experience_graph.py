"""ExperienceGraphProjection — 基于 LearningEvent 的统计投影。"""
from typing import Dict, Optional
from shared_src.projection.experience_stats import ExperienceStats


class ExperienceGraphProjection:
    """
    经验图投影——从 LearningEvent 构建按 (failure_pattern, capability, env) 索引的统计。

    v15: key = failure_pattern|capability_id|env_fingerprint
    """

    def __init__(self, epoch: str = ""):
        self.epoch = epoch
        self._stats: Dict[str, ExperienceStats] = {}

    def record_learning(self, failure_pattern: str, capability_id: str,
                        env_fingerprint: str, outcome: str,
                        duration_ms: float = 0) -> None:
        """从 LearningEvent 数据记录一次学习。"""
        key = ExperienceStats(
            failure_pattern=failure_pattern,
            capability_id=capability_id,
            env_fingerprint=env_fingerprint,
        ).key

        if key not in self._stats:
            self._stats[key] = ExperienceStats(
                failure_pattern=failure_pattern,
                capability_id=capability_id,
                env_fingerprint=env_fingerprint,
            )

        stats = self._stats[key]
        stats.total_executions += 1
        if outcome == "success":
            stats.success_count += 1
        else:
            stats.failure_count += 1

        if stats.total_executions == 1:
            stats.avg_duration_ms = duration_ms
        else:
            stats.avg_duration_ms = (
                stats.avg_duration_ms * (stats.total_executions - 1) + duration_ms
            ) / stats.total_executions

    def get_stats(self, failure_pattern: str, capability_id: str,
                  env_fingerprint: str) -> Optional[ExperienceStats]:
        key = ExperienceStats(
            failure_pattern=failure_pattern,
            capability_id=capability_id,
            env_fingerprint=env_fingerprint,
        ).key
        return self._stats.get(key)

    def status(self) -> dict:
        return {
            "epoch": self.epoch,
            "stats_count": len(self._stats),
            "keys": list(self._stats.keys()),
        }
