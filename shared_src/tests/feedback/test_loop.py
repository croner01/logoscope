import pytest
from shared_src.feedback.loop import FeedbackLoop
from shared_src.feedback.events import EvaluationEvent, LearningEvent
from shared_src.event.bus import InMemoryEventBus
from shared_src.knowledge.store import KnowledgeMemoryStore
from shared_src.knowledge.memory import MemoryRecord
from shared_src.capability.stats import CapabilityStatistics


class MockStatsStore:
    def __init__(self):
        self._stats = {}

    def get(self, capability_id):
        return self._stats.get(capability_id)

    def save(self, stats):
        self._stats[stats.capability_id] = stats


class TestFeedbackLoop:
    def test_record_execution_writes_memory(self):
        """FeedbackLoop 写入记忆"""
        store = KnowledgeMemoryStore()
        loop = FeedbackLoop(knowledge_store=store, bus=InMemoryEventBus(),
                             capability_stats_store=MockStatsStore())
        loop.record_execution(
            episode_id="ep-001",
            capability_id="ssh.restart_service",
            outcome="success",
            duration_ms=5000,
        )
        memories = store.retrieve("restart")
        assert len(memories) >= 1

    def test_record_execution_publishes_events(self):
        """FeedbackLoop 发布 EvaluationEvent"""
        bus = InMemoryEventBus()
        loop = FeedbackLoop(knowledge_store=KnowledgeMemoryStore(),
                             bus=bus,
                             capability_stats_store=MockStatsStore())
        loop.record_execution(
            episode_id="ep-001",
            capability_id="ssh.restart_service",
            outcome="success",
            duration_ms=5000,
            failure_pattern="HeartbeatLost",
            env_fingerprint="prod",
        )
        # 检查 bus 是否有 platform.system 消息
        system_events = bus._history.get("platform.system", [])
        assert len(system_events) >= 2  # EvaluationEvent + LearningEvent

    def test_record_execution_updates_stats(self):
        """FeedbackLoop 更新 CapabilityStatistics"""
        stats_store = MockStatsStore()
        loop = FeedbackLoop(knowledge_store=KnowledgeMemoryStore(),
                             bus=InMemoryEventBus(),
                             capability_stats_store=stats_store)
        loop.record_execution(
            episode_id="ep-001",
            capability_id="ssh.restart_service",
            outcome="success",
            duration_ms=5000,
        )
        stats = stats_store.get("ssh.restart_service")
        assert stats.total_executions == 1
        assert stats.success_count == 1

    def test_multiple_failures_update_stats(self):
        stats_store = MockStatsStore()
        loop = FeedbackLoop(KnowledgeMemoryStore(), InMemoryEventBus(), stats_store)
        for i in range(3):
            loop.record_execution(episode_id=f"ep-{i}", capability_id="test.cap",
                                   outcome="failure", duration_ms=10000)
        stats = stats_store.get("test.cap")
        assert stats.total_executions == 3
        assert stats.failure_count == 3
