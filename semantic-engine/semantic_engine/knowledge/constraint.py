"""Constraint -- 使用 Expression 表达条件的约束知识。"""
from dataclasses import dataclass, field
from typing import Optional
from shared_src.expression.models import Expression


@dataclass
class Constraint:
    """
    约束——使用 Expression 表达适用条件。

    - applies_to: 应用于哪个操作（"restart_service", "migrate_vm", "*"）
    - condition: Expression 条件（None = 始终适用）
    - restriction: 限制内容
    - severity: "error"（拒绝）/ "warning"（提示）
    """
    constraint_id: str
    applies_to: str = "*"
    condition: Optional[Expression] = None
    restriction: str = ""
    severity: str = "error"
    policy_hint: str = ""
