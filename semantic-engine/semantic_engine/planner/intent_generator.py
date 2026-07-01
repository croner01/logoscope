"""IntentGenerator — 将 GoalNode 转为 PlanIntent（what, not how）。"""
from abc import ABC, abstractmethod
from typing import Optional
from shared_src.goal.models import GoalNode
from .models import PlanIntent


class IntentGenerator(ABC):
    @abstractmethod
    def can_handle(self, finding, goal_node: GoalNode, worldview) -> bool: ...
    @abstractmethod
    def generate(self, finding, goal_node: GoalNode, worldview) -> Optional[PlanIntent]: ...


class RestartIntentGenerator(IntentGenerator):
    def can_handle(self, finding, goal_node: GoalNode, worldview) -> bool:
        desired = goal_node.desired_state.lower()
        return any(kw in desired for kw in ["healthy", "responding", "connected", "running"])

    def generate(self, finding, goal_node: GoalNode, worldview) -> Optional[PlanIntent]:
        state = worldview.state.get_state(goal_node.entity_type, goal_node.entity_name)
        if state == "ERROR":
            return PlanIntent(
                action="restart_service",
                entity_type=goal_node.entity_type,
                entity_name=goal_node.entity_name,
            )
        return None


class DiagnosticIntentGenerator(IntentGenerator):
    def can_handle(self, finding, goal_node: GoalNode, worldview) -> bool:
        desired = goal_node.desired_state.lower()
        return ("unknown" in desired or "diagnose" in desired or
                "evidence" in desired or finding.confidence < 0.5)

    def generate(self, finding, goal_node: GoalNode, worldview) -> Optional[PlanIntent]:
        return PlanIntent(
            action="collect_diagnostic",
            entity_type=goal_node.entity_type,
            entity_name=goal_node.entity_name,
        )


class FailoverIntentGenerator(IntentGenerator):
    def can_handle(self, finding, goal_node: GoalNode, worldview) -> bool:
        desired = goal_node.desired_state.lower()
        return "available" in desired or "failover" in desired

    def generate(self, finding, goal_node: GoalNode, worldview) -> Optional[PlanIntent]:
        return PlanIntent(
            action="failover",
            entity_type=goal_node.entity_type,
            entity_name=goal_node.entity_name,
        )
