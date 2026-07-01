import pytest
from shared_src.projection.experience_stats import ExperienceStats
from semantic_engine.projections.experience_graph import ExperienceGraphProjection


class TestExperienceStats:
    def test_stats_key(self):
        """ExperienceStats 按 (failure_pattern, capability, env) 索引"""
        stats = ExperienceStats(
            failure_pattern="RabbitMQHeartbeatLost",
            capability_id="ssh.restart_service",
            env_fingerprint="prod:rabbitmq",
        )
        assert "RabbitMQHeartbeatLost" in stats.key
        assert "ssh.restart_service" in stats.key

    def test_different_pattern_separate_keys(self):
        """不同 failure_pattern 的统计不混合"""
        p1 = ExperienceStats(failure_pattern="RabbitMQHeartbeatLost",
                              capability_id="ssh.restart_service", env_fingerprint="prod")
        p2 = ExperienceStats(failure_pattern="NovaOOM",
                              capability_id="ssh.restart_service", env_fingerprint="prod")
        assert p1.key != p2.key

    def test_success_rate(self):
        stats = ExperienceStats(total_executions=10, success_count=8)
        assert stats.success_rate == 0.8

    def test_zero_executions(self):
        stats = ExperienceStats()
        assert stats.success_rate == 0.0


class TestExperienceGraphProjection:
    def test_record_learning(self):
        """ExperienceGraphProjection 从 LearningEvent 构建统计"""
        projector = ExperienceGraphProjection(epoch="20260701")
        projector.record_learning(
            failure_pattern="RabbitMQHeartbeatLost",
            capability_id="ssh.restart_service",
            env_fingerprint="prod",
            outcome="success",
        )
        stats = projector.get_stats(
            "RabbitMQHeartbeatLost", "ssh.restart_service", "prod"
        )
        assert stats is not None
        assert stats.total_executions == 1
        assert stats.success_count == 1

    def test_multiple_records(self):
        projector = ExperienceGraphProjection()
        for i in range(5):
            projector.record_learning(
                failure_pattern="RabbitMQHeartbeatLost",
                capability_id="ssh.restart_service",
                env_fingerprint="prod",
                outcome="success",
            )
        for i in range(3):
            projector.record_learning(
                failure_pattern="RabbitMQHeartbeatLost",
                capability_id="ssh.restart_service",
                env_fingerprint="prod",
                outcome="failure",
            )
        stats = projector.get_stats(
            "RabbitMQHeartbeatLost", "ssh.restart_service", "prod"
        )
        assert stats.total_executions == 8
        assert stats.success_count == 5
        assert stats.failure_count == 3

    def test_separate_failure_patterns(self):
        """不同 failure_pattern 统计不混合"""
        projector = ExperienceGraphProjection()
        projector.record_learning("PatternA", "cap.a", "prod", "success")
        projector.record_learning("PatternA", "cap.a", "prod", "success")
        projector.record_learning("PatternB", "cap.a", "prod", "failure")

        stats_a = projector.get_stats("PatternA", "cap.a", "prod")
        stats_b = projector.get_stats("PatternB", "cap.a", "prod")
        assert stats_a.total_executions == 2
        assert stats_b.total_executions == 1

    def test_get_nonexistent(self):
        projector = ExperienceGraphProjection()
        assert projector.get_stats("Nonexistent", "cap", "env") is None

    def test_status(self):
        projector = ExperienceGraphProjection(epoch="20260701")
        projector.record_learning("PatternA", "cap.a", "prod", "success")
        status = projector.status()
        assert status.event_count >= 1
