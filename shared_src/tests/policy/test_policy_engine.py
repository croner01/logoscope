import pytest
from shared_src.policy.models import PolicyEvaluationResult, PolicyDecision, UtilityWeights
from shared_src.policy.engine import PolicyEngine
from shared_src.policy.decision_record import DecisionRecordStore
from shared_src.capability.models import Capability
from shared_src.workflow.models import Workflow, WorkflowStep
from shared_src.execution.models import WorkflowCandidate
from shared_src.blast_radius.models import BlastRadiusReport
from shared_src.risk.models import RiskProfile


class MockBlastAnalyzer:
    def analyze(self, cap, et, en):
        return BlastRadiusReport(risk_level="medium", estimated_vm_count=5)


class MockRiskEngine:
    def compute(self, action, et, en, base_risk=50):
        return RiskProfile(business_risk=20, execution_risk=base_risk,
                           operational_risk=10, final_risk=min(100, base_risk))


class TestPolicyEngine:
    @pytest.fixture
    def engine(self):
        return PolicyEngine(
            weights=UtilityWeights(),
            blast_analyzer=MockBlastAnalyzer(),
            risk_engine=MockRiskEngine(),
        )

    def test_utility_computation(self, engine):
        """Utility 计算公式"""
        wf = Workflow(name="restart", steps=[])
        c = WorkflowCandidate(workflow=wf, estimated_success_rate=0.9,
                              base_risk=50, final_risk=55,
                              estimated_duration_minutes=5)
        utility = engine._compute_utility(c, "SERVICE", "rabbitmq")
        # U = 0.9*100*0.5 - 55*0.3 - 5*0.1 - 5*0.05
        expected = 0.9 * 100 * 0.5 - 55 * 0.3 - 5 * 0.1 - 5 * 0.05
        assert abs(utility - expected) < 0.1

    def test_rank_by_utility(self, engine):
        """排序按 utility 降序"""
        candidates = [
            WorkflowCandidate(workflow=Workflow(name="A"), estimated_success_rate=0.9,
                              base_risk=50, final_risk=55),
            WorkflowCandidate(workflow=Workflow(name="B"), estimated_success_rate=0.6,
                              base_risk=20, final_risk=25),
        ]
        ranked = engine._rank(candidates, "SERVICE", "svc")
        assert len(ranked) == 2
        assert ranked[0].workflow.name == "A"  # 高 success_rate 应排前

    def test_evaluate_selects_candidate(self, engine):
        """evaluate 选择最佳候选"""
        candidates = [
            WorkflowCandidate(workflow=Workflow(name="A"), estimated_success_rate=0.95,
                              base_risk=30, final_risk=35),
            WorkflowCandidate(workflow=Workflow(name="B"), estimated_success_rate=0.6,
                              base_risk=70, final_risk=80),
        ]
        result = engine.evaluate(candidates, "restart", "SERVICE", "svc")
        assert result.selected_candidate is not None
        assert result.decision in (
            PolicyDecision.CANDIDATE_SELECTED,
            PolicyDecision.DENY,
            PolicyDecision.PENDING_APPROVAL,
        )

    def test_different_weights_different_ranking(self):
        """不同权重产生不同排序"""
        wf1 = Workflow(name="high_risk_high_reward")
        wf2 = Workflow(name="low_risk_low_reward")
        candidates = [
            WorkflowCandidate(workflow=wf1, estimated_success_rate=0.95, final_risk=70),
            WorkflowCandidate(workflow=wf2, estimated_success_rate=0.60, final_risk=20),
        ]

        engine_a = PolicyEngine(
            weights=UtilityWeights(success=0.6, risk=0.1, cost=0.2, blast=0.1),
            blast_analyzer=MockBlastAnalyzer(), risk_engine=MockRiskEngine(),
        )
        engine_b = PolicyEngine(
            weights=UtilityWeights(success=0.1, risk=0.6, cost=0.2, blast=0.1),
            blast_analyzer=MockBlastAnalyzer(), risk_engine=MockRiskEngine(),
        )
        ranked_a = engine_a._rank(candidates, "SERVICE", "svc")
        ranked_b = engine_b._rank(candidates, "SERVICE", "svc")
        # 低风险偏好时，低风险候选应排前
        assert ranked_a[0].workflow.name == "high_risk_high_reward"
        assert ranked_b[1].workflow.name == "high_risk_high_reward"

    def test_decision_record_auto_created(self, engine):
        """PolicyEngine.evaluate 自动创建 DecisionRecord"""
        store = DecisionRecordStore()
        engine.decision_store = store
        candidates = [
            WorkflowCandidate(workflow=Workflow(name="A"), estimated_success_rate=0.9, final_risk=35),
        ]
        result = engine.evaluate(candidates, "restart", "SERVICE", "svc", finding_id="f-001")
        records = store.get_by_finding("f-001")
        assert len(records) >= 1

    def test_deny_high_risk(self, engine):
        """final_risk >= 80 自动拒绝"""
        candidates = [
            WorkflowCandidate(workflow=Workflow(name="Risky"), final_risk=85),
        ]
        result = engine.evaluate(candidates, "delete", "SERVICE", "critical-db")
        assert result.decision == PolicyDecision.DENY
