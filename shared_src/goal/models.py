"""Goal — 目标状态模型。

v15: Goal 描述目标状态（desired_state），不描述执行步骤。
     - GoalNode 没有 action/ordering/completion_criteria/status
     - "怎么做"是 IntentGenerator 和 ExecutionPlanner 的职责
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class GoalNode:
    """
    目标状态节点——只描述什么状态应该达到。

    desired_state: 目标状态，如 "RabbitMQ.healthy", "NovaAPI.responding"
    entity_type/entity_name: 哪个资源要达到该状态
    children: 子目标（与目标有 AND 关系——全部达到才算完成）
    """
    goal_id: str
    desired_state: str
    entity_type: str
    entity_name: str
    children: List['GoalNode'] = field(default_factory=list)
    # 不含: action, ordering, completion_criteria, status


@dataclass
class Goal:
    """顶层目标。"""
    primary: str
    tree: 'GoalNode'
    priority: int = 50
    reason: str = ""
