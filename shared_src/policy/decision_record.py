"""DecisionRecord — 决策记录。"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from ..decision.state_machine import DecisionStatus


@dataclass
class DecisionRecord:
    """
    决策记录——记录完整的决策路径。

    包含 Finding → Planner → Policy → Workflow 审计链。
    """
    decision_id: str = ""
    finding_id: str = ""
    context_hash: str = ""
    goal: Any = None
    intents: List[Any] = field(default_factory=list)
    candidates: List[Any] = field(default_factory=list)
    selected_candidate: Any = None
    policy_rules_matched: List[str] = field(default_factory=list)
    rejected_candidates: List[str] = field(default_factory=list)
    execution_id: str = ""
    approver: str = "auto"
    created_at: datetime = field(default_factory=datetime.utcnow)
    status: DecisionStatus = DecisionStatus.CREATED
    status_history: List[Tuple[DecisionStatus, datetime]] = field(default_factory=list)
    completed_at: Optional[datetime] = None


class DecisionRecordStore:
    """DecisionRecord 存储。"""

    def __init__(self):
        self._records: Dict[str, DecisionRecord] = {}

    def save(self, record: DecisionRecord):
        self._records[record.decision_id] = record

    def get(self, decision_id: str) -> Optional[DecisionRecord]:
        return self._records.get(decision_id)

    def get_by_finding(self, finding_id: str) -> List[DecisionRecord]:
        return [r for r in self._records.values() if r.finding_id == finding_id]
