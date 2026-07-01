import pytest
from shared_src.expression.models import Expression
from shared_src.capability.models import Capability
from shared_src.capability.registry import CapabilityRegistry
from semantic_engine.execution.workflow_composer import WorkflowComposer
from semantic_engine.planner.models import PlanIntent


class MockWorldView:
    class state:
        _values = {"HOST:compute-01": "alive"}

        @staticmethod
        def get_state(entity_type, entity_name):
            return MockWorldView.state._values.get(f"{entity_type}:{entity_name}")

        @staticmethod
        def resolve_field(field_path, entity_type, entity_name):
            if field_path == "host.host_status":
                return "alive"
            if field_path == "service.exists":
                return True
            if field_path == "resource.status":
                return "ERROR"
            return None


class TestWorkflowComposer:
    @pytest.fixture
    def registry(self):
        r = CapabilityRegistry()
        r.register(Capability(
            capability_id="ssh.restart_service",
            provider="ssh-executor",
            effects=["service.restart"],
            base_risk=50,
            preconditions=[
                Expression("host.host_status", "==", "alive"),
                Expression("service.exists", "==", True),
            ],
            postconditions=[
                Expression("resource.status", "==", "running"),
            ],
            estimated_duration_ms=15000,
        ))
        r.register(Capability(
            capability_id="ssh.restart_missing_precond",
            provider="ssh-executor",
            effects=["service.restart"],
            base_risk=50,
            preconditions=[
                Expression("host.host_status", "==", "alive"),
                Expression("nonexistent.condition", "==", True),
            ],
        ))
        return r

    def test_check_preconditions_pass(self, registry):
        composer = WorkflowComposer(registry)
        cap = registry.get("ssh.restart_service")
        assert composer._check_preconditions(cap, MockWorldView()) == True

    def test_check_preconditions_fail(self, registry):
        composer = WorkflowComposer(registry)
        cap = registry.get("ssh.restart_missing_precond")
        # nonexistent.condition 不存在
        assert composer._check_preconditions(cap, MockWorldView()) == False

    def test_compose_intent_to_workflow(self, registry):
        composer = WorkflowComposer(registry)
        intent = PlanIntent(action="restart_service", entity_type="SERVICE",
                             entity_name="rabbitmq")
        wf = composer.compose(intent, MockWorldView())
        assert wf is not None
        assert len(wf.steps) >= 1

    def test_compose_unmatched_intent(self, registry):
        """不支持的 intent 返回 None"""
        composer = WorkflowComposer(registry)
        intent = PlanIntent(action="unsupported_action", entity_type="SERVICE",
                             entity_name="svc")
        wf = composer.compose(intent, MockWorldView())
        assert wf is None
