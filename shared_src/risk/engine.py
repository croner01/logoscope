"""RiskEngine — 三层风险评估引擎。"""
from typing import Optional, List
from .models import RiskProfile
from ..expression.impact_model import ImpactModel
from ..capability.models import Capability
from ..knowledge.constraint import Constraint
from ..blast_radius.analyzer import BlastRadiusAnalyzer
from ..blast_radius.models import BlastRadiusReport


class RiskEngine:
    """
    三层风险评估引擎。

    1. Business Risk: 操作对业务的影响（基于 action 类型）
    2. Execution Risk: 操作执行失败的概率（基于 base_risk）
    3. Operational Risk: 环境、依赖、约束带来的额外风险
    - Blast Radius 调整
    - Constraint Expression 检查
    """

    def __init__(self, blast_analyzer: BlastRadiusAnalyzer,
                 knowledge_store):
        self.blast_analyzer = blast_analyzer
        self.knowledge_store = knowledge_store

    def compute(self, action: str, entity_type: str,
                entity_name: str, base_risk: int = 50) -> RiskProfile:
        business_risk = self._business_risk(action)
        execution_risk = self._execution_risk(base_risk)
        operational_risk = self._operational_risk(action, entity_type, entity_name)

        final_risk = min(100, (
            business_risk * 0.3 +
            execution_risk * 0.3 +
            operational_risk * 0.4
        ))

        return RiskProfile(
            business_risk=business_risk,
            execution_risk=execution_risk,
            operational_risk=operational_risk,
            final_risk=int(final_risk),
        )

    def _business_risk(self, action: str) -> int:
        """基于 action 类型的业务风险。"""
        high_risk_actions = {"delete_volume", "destroy_vm", "format_disk",
                              "reset_network", "reboot_host"}
        med_risk_actions = {"restart_service", "migrate_vm", "failover",
                            "update_config", "restart_cluster"}
        if action in high_risk_actions:
            return 60
        if action in med_risk_actions:
            return 30
        return 10

    def _execution_risk(self, base_risk: int) -> int:
        """基于 Capability base_risk 的执行风险。"""
        return min(100, base_risk)

    def _operational_risk(self, action: str, entity_type: str,
                          entity_name: str) -> int:
        """运维风险——Blast Radius + Constraint 检查。"""
        risk = 10

        # Blast Radius
        try:
            cap = Capability(capability_id=action, effects=[action],
                              base_risk=10, impact_model=ImpactModel())
            report = self.blast_analyzer.analyze(cap, entity_type, entity_name)
            if report.risk_level in ("critical", "high"):
                risk += 30 if report.risk_level == "critical" else 15
        except Exception:
            pass

        # Constraint 检查
        constraints = self.knowledge_store.get_constraints(action) if hasattr(
            self.knowledge_store, "get_constraints") else []
        for c in constraints:
            if c.condition and c.severity == "error":
                risk += 50

        return min(100, risk)
