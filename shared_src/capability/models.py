"""
Capability 数据模型。

v15: 与 Expression 集成，替代字符串 precondition/postcondition。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class ParameterDef:
    """Capability 参数定义。"""
    name: str = ""
    type: str = "string"  # string, integer, boolean, select
    required: bool = False
    default: Any = None
    description: str = ""
    choices: List[str] = field(default_factory=list)


@dataclass
class Capability:
    """
    Capability — 可执行的操作能力。

    通过 Expression 表达前置/后置条件，ImpactModel 表达影响评估。
    """
    capability_id: str = ""
    provider: str = ""
    effects: List[str] = field(default_factory=list)
    base_risk: int = 0
    preconditions: List[Any] = field(default_factory=list)
    postconditions: List[Any] = field(default_factory=list)
    impact_model: Any = None
    rollback_capability: str = ""
    estimated_duration_ms: int = 0
    estimated_cost: float = 0.0
    parameters: List[ParameterDef] = field(default_factory=list)
    description: str = ""
