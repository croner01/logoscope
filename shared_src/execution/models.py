"""Execution 数据模型。"""
from dataclasses import dataclass, field
from typing import Any, List, Optional
from shared_src.workflow.models import Workflow


@dataclass
class WorkflowCandidate:
    """工作流候选方案——被 PolicyEngine 评估的对象。

    注意：base_risk/final_risk/estimated_success_rate/estimated_duration_minutes
    均为安全默认值。实际值应由 RiskEngine.compute() 和 Planner 覆写，
    调用方不应依赖这些默认值做决策。
    """
    workflow: Workflow = field(default_factory=Workflow)
    estimated_success_rate: float = 0.0  # 由 Planner 覆写
    base_risk: int = 0                  # 由 RiskEngine.compute() 覆写
    final_risk: int = 0                 # 由 RiskEngine.compute() 覆写
    estimated_duration_minutes: int = 0  # 由 Planner 覆写
