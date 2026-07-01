"""Planner 数据模型。"""
from dataclasses import dataclass, field
from typing import List, Optional
from shared_src.goal.models import Goal


@dataclass
class PlanIntent:
    """
    计划意图——描述"做什么"（what），不是"怎么做"（how）。

    action: "restart_service", "collect_diagnostic", "failover"
    entity_type/entity_name: 操作目标
    """
    action: str = ""
    entity_type: str = ""
    entity_name: str = ""
    params: dict = field(default_factory=dict)
