import pytest
from shared_src.goal.models import GoalNode
from shared_src.expression.models import Expression
from semantic_engine.inference.finding import Finding
from semantic_engine.planner.intent_generator import (
    RestartIntentGenerator, DiagnosticIntentGenerator,
    FailoverIntentGenerator, PlanIntent,
)


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"


class MockWorldView:
    class state:
        @staticmethod
        def get_state(entity_type, entity_name):
            if entity_name == "rabbitmq-prod" or entity_name == "nova-api":
                return "ERROR"
            if entity_name == "healthy-svc":
                return "RUNNING"
            return "UNKNOWN"
        @staticmethod
        def resolve_field(field_path, entity_type, entity_name):
            return "ERROR"

    class topology:
        @staticmethod
        def get_dependents(t, n): return []
        @staticmethod
        def get_dependencies(t, n): return []
        @staticmethod
        def get_impact_set(t, n, depth=3): return []


class TestRestartIntentGenerator:
    def test_can_handle_service_healthy(self):
        gen = RestartIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="RabbitMQ.healthy",
                         entity_type=SERVICE, entity_name="rabbitmq")
        finding = Finding(category="HeartbeatLost", confidence=0.9)
        assert gen.can_handle(finding, node, MockWorldView())

    def test_can_handle_responding(self):
        gen = RestartIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="NovaAPI.responding",
                         entity_type=SERVICE, entity_name="nova-api")
        finding = Finding(category="APIError", confidence=0.8)
        assert gen.can_handle(finding, node, MockWorldView())

    def test_generates_intent(self):
        gen = RestartIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="RabbitMQ.healthy",
                         entity_type=SERVICE, entity_name="rabbitmq-prod")
        intent = gen.generate(Finding(category="HeartbeatLost"), node, MockWorldView())
        assert intent is not None
        assert intent.action == "restart_service"
        assert intent.entity_name == "rabbitmq-prod"
        # PlanIntent 只包含 what，不包含 how
        assert not hasattr(intent, "steps")

    def test_skips_healthy_service(self):
        gen = RestartIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="Svc.running",
                         entity_type=SERVICE, entity_name="healthy-svc")
        intent = gen.generate(Finding(), node, MockWorldView())
        assert intent is None  # 已经是 RUNNING，不需要 restart


class TestDiagnosticIntentGenerator:
    def test_can_handle_low_confidence(self):
        gen = DiagnosticIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="evidence_collected",
                         entity_type=INSTANCE, entity_name="vm-1")
        finding = Finding(category="unknown", confidence=0.3)
        assert gen.can_handle(finding, node, MockWorldView())

    def test_generates_diagnostic_intent(self):
        gen = DiagnosticIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="evidence_collected",
                         entity_type=INSTANCE, entity_name="vm-1")
        intent = gen.generate(Finding(category="unknown", confidence=0.3),
                               node, MockWorldView())
        assert intent is not None
        assert intent.action == "collect_diagnostic"


class TestFailoverIntentGenerator:
    def test_can_handle_available(self):
        gen = FailoverIntentGenerator()
        node = GoalNode(goal_id="g1", desired_state="Svc.available",
                         entity_type=SERVICE, entity_name="svc-1")
        assert gen.can_handle(Finding(), node, MockWorldView())


class TestPlanIntent:
    def test_intent_is_what_not_how(self):
        intent = PlanIntent(action="restart_service", entity_type=SERVICE,
                             entity_name="rabbitmq")
        assert intent.action == "restart_service"
        assert not hasattr(intent, "steps")
        assert not hasattr(intent, "workflow")
