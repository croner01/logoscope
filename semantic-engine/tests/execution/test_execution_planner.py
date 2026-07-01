import pytest
from shared_src.capability.models import Capability
from shared_src.capability.registry import CapabilityRegistry
from shared_src.expression.models import Expression
from shared_src.goal.models import Goal, GoalNode
from semantic_engine.execution.planner import ExecutionPlanner
from semantic_engine.execution.models import WorkflowCandidate
from semantic_engine.planner.models import PlanIntent
from semantic_engine.planner.result import PlannerResult


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"


class MockWorldView:
    class state:
        @staticmethod
        def get_state(t, n):
            return "ERROR"
        @staticmethod
        def resolve_field(fp, t, n):
            if fp == "host.host_status":
                return "alive"
            return None
    class topology:
        @staticmethod
        def get_dependents(t, n): return []
        @staticmethod
        def get_dependencies(t, n): return []
        @staticmethod
        def get_impact_set(t, n, depth=3): return []


class TestExecutionPlanner:
    @pytest.fixture
    def registry(self):
        r = CapabilityRegistry()
        r.register(Capability(
            capability_id="ssh.restart_service",
            provider="ssh-executor",
            effects=["service.restart"],
            base_risk=50,
            preconditions=[Expression("host.host_status", "==", "alive")],
            estimated_duration_ms=15000,
        ))
        return r

    def test_plans_intent_to_candidate(self, registry):
        planner = ExecutionPlanner(registry, MockWorldView())
        intent = PlanIntent(action="restart_service", entity_type=SERVICE,
                             entity_name="rabbitmq")
        candidates = planner.plan([intent], None)
        assert len(candidates) >= 1
        assert all(isinstance(c, WorkflowCandidate) for c in candidates)

    def test_candidate_has_estimated_success_rate(self, registry):
        planner = ExecutionPlanner(registry, MockWorldView())
        intent = PlanIntent(action="restart_service", entity_type=SERVICE,
                             entity_name="rabbitmq")
        candidates = planner.plan([intent], None)
        for c in candidates:
            assert 0.0 <= c.estimated_success_rate <= 1.0
            assert c.base_risk >= 0

    def test_candidate_final_risk_geq_base_risk(self, registry):
        """final_risk >= base_risk（环境因素调整）"""
        planner = ExecutionPlanner(registry, MockWorldView())
        intent = PlanIntent(action="restart_service", entity_type=SERVICE,
                             entity_name="rabbitmq")
        candidates = planner.plan([intent], None)
        for c in candidates:
            assert c.final_risk >= c.base_risk

    def test_empty_intents(self, registry):
        planner = ExecutionPlanner(registry, MockWorldView())
        candidates = planner.plan([], None)
        assert candidates == []

    def test_candidate_from_planner_result(self, registry):
        """从 PlannerResult 生成候选"""
        planner = ExecutionPlanner(registry, MockWorldView())
        result = PlannerResult(intents=[
            PlanIntent(action="restart_service", entity_type=SERVICE, entity_name="svc"),
        ])
        candidates = planner.plan(result.intents, None)
        assert len(candidates) >= 1
