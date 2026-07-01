"""Finding — 推理结果（v15: 不含 recommended_action）。"""
from dataclasses import dataclass, field
from typing import List, Tuple, Optional


@dataclass
class Finding:
    """
    推理结果——统一结构。

    v15: 不含 recommended_action（由 Planner 从 Goal 推导）。
    """
    category: str = ""
    hypothesis: str = ""
    confidence: float = 0.0
    severity: str = "info"
    context_hash: str = ""
    knowledge_refs: List[Tuple[str, str]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    affected_entities: List[str] = field(default_factory=list)
    # 不含: recommended_action
