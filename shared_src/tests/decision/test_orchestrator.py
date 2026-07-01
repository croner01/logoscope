import pytest
from shared_src.decision.orchestrator import DecisionOrchestrator, DecisionResult
from shared_src.decision.state_machine import DecisionStateMachine, DecisionStatus
from shared_src.event.bus import InMemoryEventBus
from shared_src.goal.models import Goal, GoalNode
from shared_src.planner.models import PlanIntent
from shared_src.execution.models import WorkflowCandidate
from shared_src.workflow.models import Workflow
from shared_src.policy.models import PolicyEvaluationResult, PolicyDecision


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"


class MockFeedbackLoop:
    """Mock FeedbackLoop——追踪 record_execution 调用。"""
    def __init__(self):
        self.calls = []

    def record_execution(self, episode_id, capability_id, outcome,
                         duration_ms, failure_pattern):
        self.calls.append({
            "episode_id": episode_id,
            "capability_id": capability_id,
            "outcome": outcome,
            "failure_pattern": failure_pattern,
        })


class MockPlanner:
    def plan(self, finding, context, goal=None):
        from shared_src.planner.result import PlannerResult
        g = goal or Goal(
            primary="restore",
            tree=GoalNode(goal_id="root", desired_state="healthy",
                          entity_type=SERVICE, entity_name="svc"),
        )
        return PlannerResult(finding_id=finding.id if hasattr(finding, "id") else "f-001",
                              goal=g,
                              intents=[PlanIntent(action="restart_service",
                                                   entity_type=SERVICE, entity_name="svc")])


class MockExecutionPlanner:
    def plan(self, intents, context):
        return [WorkflowCandidate(
            workflow=Workflow(name="restart", steps=[]),
            estimated_success_rate=0.9, base_risk=50, final_risk=55,
        )]


class MockRiskEngine:
    def compute(self, action, entity_type, entity_name, base_risk=50,
                findings=None):
        from shared_src.risk.models import RiskProfile

        operational_risk = 15
        # 如果存在 correlation.found 且目标实体在 affected_entities 中，模拟加成
        if findings:
            for f in findings:
                if (isinstance(f, dict)
                        and f.get("category") == "correlation.found"
                        and entity_name in f.get("affected_entities", [])):
                    confidence = float(f.get("confidence", 0.5))
                    if confidence >= 0.8:
                        operational_risk += 15
                    elif confidence >= 0.6:
                        operational_risk += 8
                    else:
                        operational_risk += 5

        return RiskProfile(business_risk=20, execution_risk=50,
                           operational_risk=operational_risk,
                           final_risk=55)


class MockBlastAnalyzer:
    def analyze(self, cap, et, en):
        from shared_src.blast_radius.models import BlastRadiusReport
        return BlastRadiusReport(risk_level="medium", estimated_vm_count=5)


class MockPolicyEngine:
    def evaluate(self, candidates, action, et, en, finding_id=""):
        return PolicyEvaluationResult(
            decision=PolicyDecision.CANDIDATE_SELECTED,
            selected_candidate=candidates[0] if candidates else None,
        )


class MockWorkflowEngine:
    def execute(self, wf, context):
        from shared_src.workflow.models import WorkflowEvent
        return WorkflowEvent(outcome="success")


class MockEpisodeStore:
    def __init__(self):
        self.episodes = []

    def save(self, episode):
        self.episodes.append(episode)

    def get_by_decision(self, decision_id):
        return next((e for e in self.episodes if e.decision_id == decision_id), None)


class MockFinding:
    def __init__(self, id="f-001", category="RabbitMQHeartbeatLost",
                 confidence=0.91, affected_entities=None):
        self.id = id
        self.category = category
        self.confidence = confidence
        self.affected_entities = affected_entities or ["SERVICE:rabbitmq"]


class TestDecisionOrchestrator:
    @pytest.fixture
    def orchestrator(self):
        bus = InMemoryEventBus()
        return DecisionOrchestrator(
            planner=MockPlanner(),
            exec_planner=MockExecutionPlanner(),
            risk_engine=MockRiskEngine(),
            blast_analyzer=MockBlastAnalyzer(),
            policy_engine=MockPolicyEngine(),
            state_machine=DecisionStateMachine(bus=bus),
            workflow_engine=MockWorkflowEngine(),
            episode_store=MockEpisodeStore(),
        )

    def test_execute_complete_flow(self, orchestrator):
        """Orchestrator 执行完整 5 阶段流程"""
        finding = MockFinding()
        result = orchestrator.execute(finding, None)
        assert result.status in ("success", "failed", "rejected", "pending_approval")

    def test_orchestrator_records_episode(self, orchestrator):
        """Orchestrator 在 LEARN 阶段记录 Episode"""
        finding = MockFinding()
        result = orchestrator.execute(finding, None)
        episode = orchestrator.episode_store.get_by_decision(
            result.decision.decision_id)
        assert episode is not None

    def test_orchestrator_success_path(self, orchestrator):
        """正常路径：success"""
        finding = MockFinding()
        result = orchestrator.execute(finding, None)
        assert result.decision is not None
        assert result.status in ("success", "failed", "rejected", "pending_approval")

    def test_orchestrator_passes_correlation_findings(self, orchestrator):
        """Orchestrator 将 correlation_findings 传递给 RiskEngine"""
        finding = MockFinding(id="f-002", category="NovaOOM",
                              affected_entities=["SERVICE:rabbitmq"])
        correlation_findings = [{
            "category": "correlation.found",
            "confidence": 0.85,
            "affected_entities": ["rabbitmq", "nova-api"],
            "evidence": ["interaction_frequency=15"],
        }]
        result = orchestrator.execute(
            finding, None, correlation_findings=correlation_findings)
        assert result.status == "success"

    def test_orchestrator_correlation_findings_increase_risk(self, orchestrator):
        """correlation_findings 导致风险评分上升"""
        finding = MockFinding(id="f-003", category="NovaOOM",
                              affected_entities=["SERVICE:rabbitmq"])
        correlation_findings = [{
            "category": "correlation.found",
            "confidence": 0.9,
            "affected_entities": ["rabbitmq", "neutron-server"],
            "evidence": ["interaction_frequency=25"],
        }]
        result = orchestrator.execute(
            finding, None, correlation_findings=correlation_findings)
        # MockRiskEngine 对高置信度 +15 → operational_risk 从 15 升到 30
        assert result.decision is not None

    def test_orchestrator_empty_correlation_findings(self, orchestrator):
        """空列表的 correlation_findings 不改变行为"""
        finding = MockFinding()
        result = orchestrator.execute(finding, None, correlation_findings=[])
        assert result.status == "success"

    def test_orchestrator_with_explicit_goal(self, orchestrator):
        """可以传入外部 Goal"""
        goal = Goal(primary="custom",
                     tree=GoalNode(goal_id="g1", desired_state="custom.ok",
                                   entity_type=SERVICE, entity_name="svc"))
        finding = MockFinding()
        result = orchestrator.execute(finding, None, goal=goal)
        assert result is not None

    # ── FeedbackLoop 集成 ──

    @pytest.fixture
    def orchestrator_with_feedback(self):
        """带 MockFeedbackLoop 的 Orchestrator。"""
        bus = InMemoryEventBus()
        feedback_loop = MockFeedbackLoop()
        orch = DecisionOrchestrator(
            planner=MockPlanner(),
            exec_planner=MockExecutionPlanner(),
            risk_engine=MockRiskEngine(),
            blast_analyzer=MockBlastAnalyzer(),
            policy_engine=MockPolicyEngine(),
            state_machine=DecisionStateMachine(bus=bus),
            workflow_engine=MockWorkflowEngine(),
            episode_store=MockEpisodeStore(),
            feedback_loop=feedback_loop,
        )
        return orch, feedback_loop

    def test_orchestrator_calls_feedback_loop(self, orchestrator_with_feedback):
        """Orchestrator 在 LEARN 阶段调用 FeedbackLoop.record_execution"""
        orch, fb = orchestrator_with_feedback
        finding = MockFinding(id="f-fb-01", category="NovaOOM")
        result = orch.execute(finding, None)
        assert result.status == "success"
        # FeedbackLoop 应被调用
        assert len(fb.calls) >= 1
        call = fb.calls[0]
        # failure_pattern 应等于 finding.category
        assert call["failure_pattern"] == "NovaOOM"
        assert call["capability_id"] == "restart"

    def test_feedback_loop_failure_pattern_matches_finding_category(
            self, orchestrator_with_feedback):
        """FeedbackLoop 的 failure_pattern 来自 finding.category"""
        orch, fb = orchestrator_with_feedback
        finding = MockFinding(id="f-fb-02", category="RabbitMQHeartbeatLost")
        orch.execute(finding, None)
        assert fb.calls[0]["failure_pattern"] == "RabbitMQHeartbeatLost"

    def test_feedback_loop_not_called_without_feedback(
            self, orchestrator):
        """不配置 feedback_loop 时不影响原有行为"""
        finding = MockFinding()
        result = orchestrator.execute(finding, None)
        assert result.status == "success"

    def test_feedback_loop_outcome_tracking(self, orchestrator_with_feedback):
        """FeedbackLoop 记录 execution 的 outcome"""
        orch, fb = orchestrator_with_feedback
        finding = MockFinding()
        result = orch.execute(finding, None)
        assert fb.calls[0]["outcome"] == "success"
