"""ExecutionPlanner — PlanIntent → WorkflowCandidate。"""
from typing import List
from .models import WorkflowCandidate
from .workflow_composer import WorkflowComposer
from shared_src.capability.registry import CapabilityRegistry


class ExecutionPlanner:
    """
    Execution Planner——将 PlanIntent 转为可执行的 WorkflowCandidate。

    - 通过 WorkflowComposer 将每个 Intent 转为 Workflow
    - 计算 estimated_success_rate 和 final_risk
    - 产出 WorkflowCandidate 列表供 PolicyEngine 排序
    """

    def __init__(self, capability_registry: CapabilityRegistry, worldview):
        self.composer = WorkflowComposer(capability_registry)
        self.worldview = worldview

    def plan(self, intents: List, context) -> List[WorkflowCandidate]:
        candidates = []
        for intent in intents:
            wf = self.composer.compose(intent, self.worldview)
            if wf:
                candidates.append(self._to_candidate(wf, intent))
        return candidates

    def _to_candidate(self, wf, intent) -> WorkflowCandidate:
        return WorkflowCandidate(
            workflow=wf,
            estimated_success_rate=0.85,
            base_risk=50,
            final_risk=55,
            estimated_duration_minutes=5,
        )
