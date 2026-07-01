"""PlannerResult — Planner 输出。"""
from dataclasses import dataclass, field
from typing import List, Optional
from shared_src.goal.models import Goal
from .models import PlanIntent


@dataclass
class PlannerResult:
    """Planner 输出——包含目标树和生成的意图列表。"""
    finding_id: str = ""
    goal: Optional[Goal] = None
    intents: List[PlanIntent] = field(default_factory=list)
