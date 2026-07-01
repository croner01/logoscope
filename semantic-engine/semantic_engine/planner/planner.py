"""Planner — Finding → Goal Tree → Intents。"""
from typing import List, Optional
from shared_src.goal.models import Goal, GoalNode
from .goal_inferrer import GoalInferrer
from .intent_generator import IntentGenerator
from .result import PlannerResult


class Planner:
    """
    Planner——规划器。

    流程:
    1. GoalInferrer: Finding → Goal Tree（目标状态树）
    2. 遍历 Goal Tree 每个节点 → IntentGenerator.can_handle()
    3. 匹配的 Generator 产出 PlanIntent（what, not how）
    4. ExecutionPlanner（Phase 6）将 PlanIntent 转为 WorkflowCandidate
    """

    def __init__(self, goal_inferrer: GoalInferrer,
                 generators: List[IntentGenerator],
                 worldview):
        self.goal_inferrer = goal_inferrer
        self.generators = generators
        self.worldview = worldview

    def plan(self, finding, context, goal: Optional[Goal] = None) -> PlannerResult:
        if not goal:
            goal = self.goal_inferrer.infer(finding, self.worldview)

        if not goal:
            return PlannerResult(finding_id=getattr(finding, "id", ""))

        intents = []
        self._plan_goal_node(goal.tree, finding, intents)

        return PlannerResult(
            finding_id=getattr(finding, "id", "") or getattr(finding, "category", ""),
            goal=goal,
            intents=intents,
        )

    def _plan_goal_node(self, node: GoalNode, finding,
                        intents: List) -> None:
        """递归处理 Goal 树中的每个节点。"""
        for gen in self.generators:
            if gen.can_handle(finding, node, self.worldview):
                intent = gen.generate(finding, node, self.worldview)
                if intent:
                    intents.append(intent)
        for child in node.children:
            self._plan_goal_node(child, finding, intents)
