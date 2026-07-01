"""BlastRadiusAnalyzer — 综合 Capability + Dependency + State 分析影响范围。"""
from typing import Optional
from ..expression.impact_model import ImpactModel
from ..capability.models import Capability
from .models import BlastRadiusReport


class BlastRadiusAnalyzer:
    """
    影响范围分析器——综合 Capability + Dependency + State。

    v15:
    - Capability.impact_model → 影响类型 + 持续时间
    - Dependency Graph → 谁受影响
    - Current State → 影响程度调整
    """

    # 默认阈值——可通过 constructor 注入定制
    DEFAULT_HIGH_DEPENDENTS = 10        # temporary + dependents > N → high
    DEFAULT_HIGH_ERROR_DEPENDENTS = 5   # ERROR state + dependents > N → high
    DEFAULT_MEDIUM_DEPENDENTS = 2       # dependents > N → medium else low

    def __init__(self, topology, state,
                 high_dependents: int = None,
                 high_error_dependents: int = None,
                 medium_dependents: int = None):
        self.topology = topology
        self.state = state
        self.high_dependents = high_dependents or self.DEFAULT_HIGH_DEPENDENTS
        self.high_error_dependents = high_error_dependents or self.DEFAULT_HIGH_ERROR_DEPENDENTS
        self.medium_dependents = medium_dependents or self.DEFAULT_MEDIUM_DEPENDENTS

    def analyze(self, capability: Capability, entity_type: str,
                entity_name: str) -> BlastRadiusReport:
        impact = capability.impact_model or ImpactModel()

        # 1. Dependency Graph
        impact_sets = self.topology.get_impact_set(entity_type, entity_name, depth=5)
        directly = impact_sets[0] if impact_sets else []
        indirectly = []
        if len(impact_sets) > 1:
            for layer in impact_sets[1:]:
                indirectly.extend(layer)

        # 2. Current State
        current_state = self.state.get_state(entity_type, entity_name)

        # 3. Risk Level
        risk_level = self._assess_risk_level(impact, current_state, directly)

        # 4. Reasoning
        reasoning = self._build_reasoning(impact, current_state, risk_level, directly)

        return BlastRadiusReport(
            primary_target_type=entity_type,
            primary_target_name=entity_name,
            directly_affected=directly,
            indirectly_affected=indirectly,
            estimated_vm_count=self.topology.estimate_vm_count(entity_type, entity_name),
            estimated_service_count=len(directly),
            risk_level=risk_level,
            reasoning=reasoning,
        )

    def _assess_risk_level(self, impact: ImpactModel, current_state: str,
                            dependents: list) -> str:
        if impact.severity == "permanent":
            return "critical"
        if impact.severity == "temporary" and len(dependents) > self.high_dependents:
            return "high"
        if current_state == "ERROR" and len(dependents) > self.high_error_dependents:
            return "high"
        return "medium" if len(dependents) > self.medium_dependents else "low"

    def _build_reasoning(self, impact: ImpactModel, current_state: str,
                          risk_level: str, dependents: list) -> str:
        parts = [f"Impact: {impact.severity}/{impact.duration}/{impact.scope}"]
        parts.append(f"State: {current_state}")
        parts.append(f"Dependents: {len(dependents)}")
        parts.append(f"Risk: {risk_level}")
        return "; ".join(parts)
