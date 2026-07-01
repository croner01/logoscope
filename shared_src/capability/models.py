"""Capability 数据模型。"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from shared_src.expression.models import Expression
from shared_src.expression.impact_model import ImpactModel


@dataclass
class ParameterDef:
    """Capability 参数定义。"""
    name: str
    type: str = "string"
    required: bool = False
    default: object = None
    description: str = ""


@dataclass
class Capability:
    """
    能力——描述一个可执行操作。

    v15:
    - preconditions/postconditions: List[Expression]（结构化，不是字符串）
    - impact_model: ImpactModel（Blast Radius 用）
    - effects: List[str]（tags，不是 Enum）
    - base_risk: int（环境无关的基准风险）
    """
    capability_id: str
    provider: str
    effects: List[str] = field(default_factory=list)
    base_risk: int = 50
    risk_reason: str = ""

    preconditions: List[Expression] = field(default_factory=list)
    postconditions: List[Expression] = field(default_factory=list)
    side_effects: List[str] = field(default_factory=list)
    impact_model: Optional[ImpactModel] = None
    rollback_capability: str = ""

    estimated_duration_ms: int = 30000
    estimated_cost: float = 1.0
    parameters: Dict[str, ParameterDef] = field(default_factory=dict)
    timeout_seconds: int = 30
    retry_count: int = 0
