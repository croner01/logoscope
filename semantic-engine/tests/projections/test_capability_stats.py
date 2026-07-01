import pytest
from shared_src.capability.stats import CapabilityStatistics
from semantic_engine.projections.capability_stats_projector import CapabilityStatsProjector


class TestCapabilityStatistics:
    def test_success_rate(self):
        stats = CapabilityStatistics(
            capability_id="ssh.restart_service",
            total_executions=10, success_count=8, failure_count=2,
            avg_recovery_time_ms=120000,
        )
        assert stats.success_rate == 0.8

    def test_zero_executions(self):
        stats = CapabilityStatistics(capability_id="test")
        assert stats.success_rate == 0.0

    def test_all_failures(self):
        stats = CapabilityStatistics(
            capability_id="test", total_executions=5,
            success_count=0, failure_count=5,
        )
        assert stats.success_rate == 0.0


class TestCapabilityStatsProjector:
    def test_record_execution(self):
        projector = CapabilityStatsProjector()
        projector.record_execution("ssh.restart_service", "success", 5000)
        stats = projector.get_stats("ssh.restart_service")
        assert stats.total_executions == 1
        assert stats.success_count == 1

    def test_multiple_executions(self):
        projector = CapabilityStatsProjector()
        for i in range(10):
            projector.record_execution("echo.test", "success", 1000)
        stats = projector.get_stats("echo.test")
        assert stats.total_executions == 10
        assert stats.success_count == 10

    def test_mixed_outcomes(self):
        projector = CapabilityStatsProjector()
        projector.record_execution("ssh.restart", "success", 5000)
        projector.record_execution("ssh.restart", "failure", 30000)
        projector.record_execution("ssh.restart", "success", 6000)
        stats = projector.get_stats("ssh.restart")
        assert stats.total_executions == 3
        assert stats.success_count == 2
        assert stats.failure_count == 1

    def test_get_nonexistent(self):
        projector = CapabilityStatsProjector()
        assert projector.get_stats("nonexistent") is None

    def test_average_recovery_time(self):
        projector = CapabilityStatsProjector()
        projector.record_execution("cap.a", "failure", 10000)
        projector.record_execution("cap.a", "failure", 20000)
        projector.record_execution("cap.a", "success", 5000)
        stats = projector.get_stats("cap.a")
        assert stats.avg_recovery_time_ms > 0
