"""ExperienceGraphProjection — 基于 LearningEvent 的统计投影。"""
from typing import Dict, Optional, Any
from shared_src.projection.base import Projection, ProjectionStatus
from shared_src.projection.checkpoint import ProjectionCheckpoint
from shared_src.projection.experience_stats import ExperienceStats
from shared_src.event.envelope import EventEnvelope


class ExperienceGraphProjection(Projection):
    """
    经验图投影——从 LearningEvent 构建按 (failure_pattern, capability, env) 索引的统计。

    v15: key = failure_pattern|capability_id|env_fingerprint
    """

    name = "experience_graph"

    def __init__(self, epoch: str = ""):
        self.epoch = epoch
        self._stats: Dict[str, ExperienceStats] = {}
        self._event_count = 0
        self._checkpoint = ProjectionCheckpoint(
            projection="experience_graph", epoch=self.epoch
        )

    # --- Projection ABC 接口 ---

    def apply(self, envelope: EventEnvelope):
        """增量应用 EventEnvelope（标准 Projection 入口）。"""
        if envelope.event_type == "feedback.learning":
            from shared_src.feedback.events import deserialize_learning_event
            event = deserialize_learning_event(envelope)
            self._record_learning(
                failure_pattern=event.failure_pattern,
                capability_id=event.capability_id,
                env_fingerprint=event.env_fingerprint,
                outcome=event.outcome,
                duration_ms=getattr(event, "duration_ms", 0),
            )

    def rebuild(self, event_source: Any):
        """从事件源全量重建。"""
        self._stats.clear()
        self._event_count = 0
        self._checkpoint = ProjectionCheckpoint(
            projection="experience_graph", epoch=self.epoch)
        for envelope in event_source:
            self.apply(envelope)

    def checkpoint(self) -> ProjectionCheckpoint:
        """返回当前消费进度 checkpoint。"""
        return self._checkpoint

    def status(self) -> ProjectionStatus:
        """返回投影的运行时状态。"""
        return ProjectionStatus(
            projection_epoch=self.epoch,
            event_count=self._event_count,
            checkpoint=self._checkpoint,
        )

    # --- 便捷方法（兼容旧接口） ---

    def record_learning(self, failure_pattern: str, capability_id: str,
                        env_fingerprint: str, outcome: str,
                        duration_ms: float = 0) -> None:
        """从 LearningEvent 数据记录一次学习。"""
        self._record_learning(failure_pattern, capability_id,
                              env_fingerprint, outcome, duration_ms)

    def get_stats(self, failure_pattern: str, capability_id: str,
                  env_fingerprint: str) -> Optional[ExperienceStats]:
        key = ExperienceStats(
            failure_pattern=failure_pattern,
            capability_id=capability_id,
            env_fingerprint=env_fingerprint,
        ).key
        return self._stats.get(key)

    # --- 内部方法 ---

    def _record_learning(self, failure_pattern: str, capability_id: str,
                         env_fingerprint: str, outcome: str,
                         duration_ms: float = 0) -> None:
        """核心统计逻辑（被 record_learning 和 apply 共用）。"""
        self._event_count += 1
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
