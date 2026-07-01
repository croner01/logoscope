"""Execution 数据模型。"""
from dataclasses import dataclass, field
from typing import Optional
from shared_src.workflow.models import Workflow


@dataclass
class WorkflowCandidate:
    """
    工作流候选方案。

    - estimated_success_rate: 预估成功率（不同于 Finding.confidence）
    - base_risk: Capability 基础风险
    - final_risk: 动态调整后的风险（base + env + time + blast）
    """
    workflow: Optional[Workflow] = None
    estimated_success_rate: float = 0.5
    base_risk: int = 50
    final_risk: int = 50
    estimated_duration_minutes: int = 5
    blocked_reason: str = ""
