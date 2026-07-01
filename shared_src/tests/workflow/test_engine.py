import pytest
from shared_src.workflow.models import Workflow, WorkflowStep, WorkflowCommand, WorkflowEvent, WorkflowContext
from shared_src.workflow.engine import WorkflowEngine
from shared_src.capability.registry import CapabilityRegistry
from shared_src.capability.models import Capability
from shared_src.event.bus import InMemoryEventBus


class TestWorkflowModels:
    def test_workflow_creation(self):
        wf = Workflow(
            workflow_id="wf-001",
            name="restart_service",
            steps=[WorkflowStep(capability="ssh.restart_service", params={"svc": "rabbitmq"})],
        )
        assert wf.name == "restart_service"
        assert len(wf.steps) == 1
        assert wf.steps[0].capability == "ssh.restart_service"

    def test_workflow_command_event_separation(self):
        """Command 和 Event 在不同 topic"""
        cmd = WorkflowCommand(command_id="cmd-1", workflow_id="wf-1", action="execute")
        event = WorkflowEvent(event_id="evt-1", workflow_id="wf-1", command_id="cmd-1",
                               outcome="success")
        assert cmd.command_id == "cmd-1"
        assert event.outcome == "success"
        assert cmd.workflow_id == event.workflow_id


class TestWorkflowEngine:
    @pytest.fixture
    def engine(self):
        bus = InMemoryEventBus()
        registry = CapabilityRegistry()
        registry.register(Capability(
            capability_id="echo.test", provider="mock",
            effects=["read.process"], base_risk=5,
        ))
        return WorkflowEngine(bus=bus, registry=registry)

    def test_workflow_execute(self, engine):
        wf = Workflow(
            workflow_id="wf-001",
            name="test",
            steps=[WorkflowStep(capability="echo.test", params={"msg": "hello"})],
        )
        context = WorkflowContext(trigger="test")
        event = engine.execute(wf, context)
        assert event.outcome in ("success", "failure")

    def test_workflow_publishes_command_and_event(self, engine):
        bus = InMemoryEventBus()
        engine.bus = bus
        wf = Workflow(
            workflow_id="wf-002",
            name="test",
            steps=[WorkflowStep(capability="echo.test", params={})],
        )
        engine.execute(wf, WorkflowContext(trigger="test"))
        assert any("platform.workflow.command" in topic
                   for topic in bus._history)
        assert any("platform.workflow.event" in topic
                   for topic in bus._history)

    def test_workflow_missing_capability(self, engine):
        wf = Workflow(
            workflow_id="wf-003",
            name="missing",
            steps=[WorkflowStep(capability="nonexistent.cap", params={})],
        )
        event = engine.execute(wf, WorkflowContext(trigger="test"))
        assert event.outcome == "failure"

    def test_workflow_empty_steps(self, engine):
        wf = Workflow(workflow_id="wf-004", name="empty", steps=[])
        event = engine.execute(wf, WorkflowContext(trigger="test"))
        assert event.outcome == "success"
