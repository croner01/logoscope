"""FeedbackLoop — 将执行结果反馈到记忆、事件和统计。"""
import json
import uuid
from datetime import datetime
from typing import Optional
from shared_src.event.bus import EventBus
from shared_src.event.envelope import EventEnvelope
from shared_src.knowledge.store import KnowledgeMemoryStore
from shared_src.knowledge.memory import MemoryRecord
from shared_src.capability.stats import CapabilityStatistics
from .events import EvaluationEvent, LearningEvent


class FeedbackLoop:
    """
    Feedback Loop——将执行结果反馈到知识、事件和统计。

    - record_execution(): episode -> EvaluationEvent + LearningEvent + Memory
    """

    def __init__(self, knowledge_store: KnowledgeMemoryStore,
                 bus: EventBus,
                 capability_stats_store=None):
        self.knowledge_store = knowledge_store
        self.bus = bus
        self.capability_stats_store = capability_stats_store

    def record_execution(self, episode_id: str, capability_id: str,
                         outcome: str, duration_ms: int,
                         failure_pattern: str = "",
                         env_fingerprint: str = "",
                         error_message: str = "") -> None:
        """记录一次执行结果。"""
        # 1. 创建 EvaluationEvent
        eval_event = EvaluationEvent(
            event_id=uuid.uuid4().hex,
            episode_id=episode_id,
            capability_id=capability_id,
            outcome=outcome,
            duration_ms=duration_ms,
            error_message=error_message,
        )
        self._publish_event("feedback.evaluation", eval_event)

        # 2. 创建 LearningEvent
        learn_event = LearningEvent(
            event_id=uuid.uuid4().hex,
            episode_id=episode_id,
            failure_pattern=failure_pattern,
            capability_id=capability_id,
            env_fingerprint=env_fingerprint,
            outcome=outcome,
        )
        self._publish_event("feedback.learning", learn_event)

        # 3. 写入记忆
        memory = MemoryRecord(
            record_id=uuid.uuid4().hex,
            record_type="repair",
            outcome=outcome,
            action_taken=capability_id,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        self.knowledge_store.add_memory(memory)

        # 4. 更新统计
        if self.capability_stats_store:
            stats = self.capability_stats_store.get(capability_id)
            if not stats:
                stats = CapabilityStatistics(capability_id=capability_id)
                self.capability_stats_store.save(stats)
            stats.total_executions += 1
            if outcome == "success":
                stats.success_count += 1
            else:
                stats.failure_count += 1
                if stats.failure_count == 1:
                    stats.avg_recovery_time_ms = float(duration_ms)
                else:
                    stats.avg_recovery_time_ms = (
                        stats.avg_recovery_time_ms * (stats.failure_count - 1) + duration_ms
                    ) / stats.failure_count

    def _publish_event(self, event_type: str, event) -> None:
        """将事件发布到 platform.system topic。"""
        env = EventEnvelope(
            event_type=event_type,
            producer="feedback-loop",
            event_id=uuid.uuid4().hex,
            payload=json.dumps({
                k: str(v) if not isinstance(v, (str, int, float, bool, list, dict)) else v
                for k, v in event.__dict__.items()
            }, default=str).encode("utf-8"),
        )
        self.bus.publish("platform.system", env)
