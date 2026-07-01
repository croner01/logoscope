"""Execution 数据模型。"""
from dataclasses import dataclass, field
from typing import Any, List, Optional
from shared_src.workflow.models import Workflow


@dataclass
class WorkflowCandidate:
    """工作流候选方案——被 PolicyEngine 评估的对象。"""
    workflow: Workflow = field(default_factory=Workflow)
    estimated_success_rate: float = 0.5
    base_risk: int = 50
    final_risk: int = 50
    estimated_duration_minutes: int = 5
