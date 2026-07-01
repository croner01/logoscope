import pytest
from shared_src.feedback.events import EvaluationEvent, LearningEvent


class TestEvaluationEvent:
    def test_evaluation_event(self):
        event = EvaluationEvent(
            event_id="ev-001", episode_id="ep-001",
            capability_id="ssh.restart_service",
            outcome="success", duration_ms=5000,
        )
        assert event.outcome == "success"
        assert event.capability_id == "ssh.restart_service"

    def test_evaluation_event_defaults(self):
        event = EvaluationEvent(event_id="ev-1", episode_id="ep-1")
        assert event.outcome == "unknown"
        assert event.duration_ms == 0

    def test_evaluation_event_failure(self):
        event = EvaluationEvent(
            event_id="ev-002", episode_id="ep-002",
            capability_id="delete_volume",
            outcome="failure", duration_ms=1000,
            error_message="Timeout after 30s",
        )
        assert event.error_message == "Timeout after 30s"


class TestLearningEvent:
    def test_learning_event(self):
        event = LearningEvent(
            event_id="le-001", episode_id="ep-001",
            failure_pattern="RabbitMQHeartbeatLost",
            capability_id="ssh.restart_service",
            env_fingerprint="prod:rabbitmq",
            outcome="success",
        )
        assert event.failure_pattern == "RabbitMQHeartbeatLost"

    def test_learning_event_defaults(self):
        event = LearningEvent(event_id="le-1", episode_id="ep-1")
        assert event.failure_pattern == ""

    def test_event_relationship(self):
        """LearningEvent 引用 EvaluationEvent"""
        eval_event = EvaluationEvent(event_id="ev-1", episode_id="ep-1",
                                       capability_id="restart", outcome="success",
                                       duration_ms=5000)
        learn_event = LearningEvent(
            event_id="le-1", episode_id="ep-1",
            failure_pattern="HeartbeatLost",
            capability_id=eval_event.capability_id,
            env_fingerprint="prod",
            outcome=eval_event.outcome,
        )
        assert learn_event.capability_id == eval_event.capability_id
        assert learn_event.outcome == eval_event.outcome
