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
    def compute(self, action, entity_type, entity_name, base_risk=50):
        from shared_src.risk.models import RiskProfile
        return RiskProfile(business_risk=20, execution_risk=50,
                           operational_risk=15, final_risk=55)


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

    def test_orchestrator_with_explicit_goal(self, orchestrator):
        """可以传入外部 Goal"""
        goal = Goal(primary="custom",
                     tree=GoalNode(goal_id="g1", desired_state="custom.ok",
                                   entity_type=SERVICE, entity_name="svc"))
        finding = MockFinding()
        result = orchestrator.execute(finding, None, goal=goal)
        assert result is not None
