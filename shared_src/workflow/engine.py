"""WorkflowEngine — 工作流执行引擎。"""
import uuid
from typing import Optional
from datetime import datetime
from shared_src.event.bus import EventBus
from shared_src.event.envelope import EventEnvelope
from shared_src.capability.registry import CapabilityRegistry
from .models import Workflow, WorkflowStep, WorkflowCommand, WorkflowEvent, WorkflowContext
import json


class WorkflowEngine:
    """
    工作流执行引擎。
    
    - execute(workflow, context) → WorkflowEvent
    - 每一步通过 CapabilityRegistry 执行
    - 发布 Command 到 platform.workflow.command
    - 发布 Event 到 platform.workflow.event
    """

    def __init__(self, bus: EventBus, registry: CapabilityRegistry):
        self.bus = bus
        self.registry = registry

    def execute(self, workflow: Workflow, context: WorkflowContext) -> WorkflowEvent:
        command_id = uuid.uuid4().hex
        event_id = uuid.uuid4().hex

        # 1. 发布 Command
        cmd = WorkflowCommand(
            command_id=command_id,
            workflow_id=workflow.workflow_id,
            action="execute",
            params={"steps": len(workflow.steps)},
        )
        self._publish("platform.workflow.command", cmd, command_id, [])

        # 2. 执行每一步
        results = []
        error_message = ""

        for step in workflow.steps:
            result = self.registry.execute(step.capability, step.params)
            if result is None:
                error_message = f"Capability {step.capability} not found"
                break
            results.append(result)

        # 3. 发布 Event
        outcome = "failure" if error_message else "success"
        event = WorkflowEvent(
            event_id=event_id,
            workflow_id=workflow.workflow_id,
            command_id=command_id,
            outcome=outcome,
            result={"step_count": len(workflow.steps), "results": results},
            error_message=error_message,
        )
        self._publish("platform.workflow.event", event, event_id, [command_id])

        return event

    def _publish(self, topic: str, data, event_id: str, parent_ids: list):
        import json as j
        env = EventEnvelope(
            event_type=topic.split(".")[-1],
            producer="workflow-engine",
            event_id=event_id,
            parent_event_ids=parent_ids,
            payload=j.dumps({
                k: str(v) if not isinstance(v, (str, int, float, bool, list, dict)) else v
                for k, v in data.__dict__.items()
            }, default=str).encode("utf-8"),
        )
        self.bus.publish(topic, env)
