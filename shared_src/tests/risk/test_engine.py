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

    # ── correlation.found 集成测试 ──

    def test_correlation_finding_high_confidence_adds_risk(self):
        """高置信度 correlation.found 增加 operational_risk"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        findings = [{
            "category": "correlation.found",
            "confidence": 0.85,
            "affected_entities": ["svc-1", "svc-2"],
            "evidence": ["interaction_frequency=12", "time_window=1 HOUR"],
        }]
        # entity_name="svc-1" 在 affected_entities 中 → 触发风险加成
        profile = engine.compute("restart_service", "SERVICE", "svc-1",
                                 base_risk=50, findings=findings)
        # 基础 operational=10 + 高置信度加成 15 = 25
        assert profile.operational_risk >= 25

    def test_correlation_finding_unrelated_entity_no_effect(self):
        """不相关的实体不受 correlation.found 影响"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        findings = [{
            "category": "correlation.found",
            "confidence": 0.9,
            "affected_entities": ["svc-A", "svc-B"],
            "evidence": ["interaction_frequency=20"],
        }]
        # entity_name="rabbitmq" 不在 affected_entities 中 → 无加成
        profile = engine.compute("restart_service", "SERVICE", "rabbitmq",
                                 base_risk=50, findings=findings)
        # 基础 operational=10, 无加成
        assert profile.operational_risk == 10

    def test_correlation_finding_confidence_scales_risk(self):
        """不同置信度等级产生不同的风险加成"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())

        # 高置信度 (0.85)
        high = engine.compute("restart_service", "SERVICE", "svc", base_risk=50,
                               findings=[{
                                   "category": "correlation.found",
                                   "confidence": 0.85,
                                   "affected_entities": ["svc"],
                                   "evidence": ["interaction_frequency=10"],
                               }])

        # 中置信度 (0.65)
        med = engine.compute("restart_service", "SERVICE", "svc", base_risk=50,
                              findings=[{
                                  "category": "correlation.found",
                                  "confidence": 0.65,
                                  "affected_entities": ["svc"],
                                  "evidence": ["interaction_frequency=6"],
                              }])

        # 低置信度 (0.55)
        low = engine.compute("restart_service", "SERVICE", "svc", base_risk=50,
                              findings=[{
                                  "category": "correlation.found",
                                  "confidence": 0.55,
                                  "affected_entities": ["svc"],
                                  "evidence": ["interaction_frequency=5"],
                              }])

        assert high.operational_risk > med.operational_risk > low.operational_risk
        assert high.operational_risk >= 25  # 10 + 15
        assert med.operational_risk >= 18  # 10 + 8
        assert low.operational_risk >= 15  # 10 + 5

    def test_correlation_finding_no_findings_no_effect(self):
        """不传入 findings 时不影响风险分"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        profile_without = engine.compute("restart_service", "SERVICE", "svc",
                                          base_risk=50)
        profile_with = engine.compute("restart_service", "SERVICE", "svc",
                                       base_risk=50, findings=None)
        assert profile_without.operational_risk == profile_with.operational_risk

    def test_correlation_finding_non_correlation_ignored(self):
        """非 correlation.found 类型的 finding 被忽略"""
        engine = RiskEngine(blast_analyzer=MockBlastAnalyzer(),
                             knowledge_store=MockKnowledgeStore())
        findings = [{
            "category": "anomaly.detected",
            "confidence": 0.95,
            "affected_entities": ["svc"],
        }]
        profile = engine.compute("restart_service", "SERVICE", "svc",
                                 base_risk=50, findings=findings)
        # 不应受非 correlation 类型影响
        assert profile.operational_risk == 10
