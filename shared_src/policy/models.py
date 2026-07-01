"""Policy 数据模型。"""
from dataclasses import dataclass, field
from typing import List, Optional, Any
from enum import Enum


class PolicyDecision(Enum):
    CANDIDATE_SELECTED = "candidate_selected"
    DENY = "deny"
    PENDING_APPROVAL = "pending_approval"


@dataclass
class UtilityWeights:
    """Utility 权重配置——可被 OPA 或 Config 覆盖。"""
    success: float = 0.5
    risk: float = 0.3
    cost: float = 0.1
    blast: float = 0.05


@dataclass
class PolicyEvaluationResult:
    decision: PolicyDecision = PolicyDecision.DENY
    selected_candidate: Any = None
    matched_rules: List[str] = field(default_factory=list)
    utility_scores: dict = field(default_factory=dict)
