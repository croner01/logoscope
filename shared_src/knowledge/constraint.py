"""Constraint — 约束规则模型（Task 5.5）。"""
from dataclasses import dataclass
from typing import Optional
from ..expression.models import Expression


@dataclass
class Constraint:
    """
    约束规则——结合 Expression 检查的执行约束。

    - applies_to: 适用范围（action 名称）
    - condition: 触发条件（Expression）
    - restriction: 约束描述
    - severity: error / warning
    """
    constraint_id: str = ""
    applies_to: str = ""
    condition: Optional[Expression] = None
    restriction: str = ""
    severity: str = "error"
