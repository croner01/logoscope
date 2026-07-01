import pytest
from shared_src.risk.models import RiskProfile
from shared_src.risk.engine import RiskEngine
from shared_src.expression.impact_model import ImpactModel
from shared_src.capability.models import Capability
from shared_src.capability.registry import CapabilityRegistry
from shared_src.expression.models import Expression
from shared_src.knowledge.constraint import Constraint
from shared_src.knowledge.models import KnowledgeDocument


class MockBlastAnalyzer:
    def analyze(self, cap, entity_type, entity_name):
        from shared_src.blast_radius.models import BlastRadiusReport
        return BlastRadiusReport(
            primary_target_type=entity_type,
            primary_target_name=entity_name,
            directly_affected=["svc-1", "svc-2", "svc-3"],
            estimated_vm_count=5,
            estimated_service_count=3,
            risk_level="medium",
            reasoning="Test analysis",
        )


class MockKnowledgeStore:
    def __init__(self, constraints=None):
        self._constraints = constraints or []

    def get_constraints(self, intent_action, context=None):
        return self._constraints

    def retrieve(self, query):
        return []


class TestRiskEngine:
    def test_three_risk_tiers(self):
        """RiskEngine 计算三层风险"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        profile = engine.compute("restart_service", "SERVICE", "rabbitmq",
                                 base_risk=50)
        assert hasattr(profile, "business_risk")
        assert hasattr(profile, "execution_risk")
        assert hasattr(profile, "operational_risk")
        assert profile.final_risk >= 0

    def test_final_risk_from_three_tiers(self):
        """final_risk 由三层风险加权计算 (business*0.3 + execution*0.3 + operational*0.4)"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        profile = engine.compute("restart_service", "SERVICE", "rabbitmq",
                                 base_risk=50)
        # restart_service → business=30, base_risk=50 → execution=50, medium blast → operational=10
        # final = int(30*0.3 + 50*0.3 + 10*0.4) = int(28) = 28
        assert profile.final_risk == 28
        assert profile.final_risk > profile.operational_risk

    def test_high_blast_increases_operational_risk(self):
        class CriticalBlastAnalyzer:
            def analyze(self, cap, entity_type, entity_name):
                from shared_src.blast_radius.models import BlastRadiusReport
                return BlastRadiusReport(risk_level="critical")

        engine = RiskEngine(blast_analyzer=CriticalBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        profile = engine.compute("delete_volume", "SERVICE", "critical-db",
                                 base_risk=80)
        assert profile.operational_risk >= 30

    def test_constraint_expression_check(self):
        """Constraint 检查使用 Expression"""
        constraint = Constraint(
            constraint_id="c-001",
            applies_to="restart_service",
            condition=Expression("resource.status", "==", "running"),
            restriction="Cannot restart a running service without approval",
            severity="error",
        )
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore(constraints=[constraint]))
        profile = engine.compute("restart_service", "SERVICE", "svc",
                                 base_risk=50)
        # error severity → operational_risk = 10 + 50 = 60
        # final = int(30*0.3 + 50*0.3 + 60*0.4) = int(48) = 48
        assert profile.operational_risk == 60
        assert profile.final_risk == 48

    def test_warning_constraint_no_addition(self):
        """warning 级别不增加风险分"""
        constraint = Constraint(
            constraint_id="c-002",
            applies_to="restart_service",
            condition=Expression("resource.status", "==", "running"),
            restriction="Consider scheduling during maintenance window",
            severity="warning",
        )
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore(constraints=[constraint]))
        profile = engine.compute("restart_service", "SERVICE", "svc",
                                 base_risk=50)
        # warning 不应显著加分
        assert profile.operational_risk <= 50

    def test_risk_profile_defaults(self):
        profile = RiskProfile()
        assert profile.business_risk == 0
        assert profile.execution_risk == 0
        assert profile.operational_risk == 0
        assert profile.final_risk == 0
