"""Planner 单元测试。"""
import pytest
from shared_src.goal.models import GoalNode, Goal
from semantic_engine.inference.finding import Finding
from semantic_engine.planner.planner import Planner
from semantic_engine.planner.result import PlannerResult
from semantic_engine.planner.goal_inferrer import GoalInferrer
from semantic_engine.planner.intent_generator import (
    RestartIntentGenerator, DiagnosticIntentGenerator,
)


class MockWorldView:
    class state:
        @staticmethod
        def get_state(entity_type, entity_name):
            return "ERROR" if entity_name != "healthy-svc" else "RUNNING"

        @staticmethod
        def resolve_field(fp, t, n):
            return "ERROR"

    class topology:
        @staticmethod
        def get_dependents(t, n): return []
        @staticmethod
        def get_dependencies(t, n): return []
        @staticmethod
        def get_impact_set(t, n, depth=3): return []


class TestPlanner:
    def test_planner_generates_intents(self):
        """Planner 从 Finding 生成 Intents（通过 Goal 树）"""
        planner = Planner(
            goal_inferrer=GoalInferrer(),
            generators=[RestartIntentGenerator(), DiagnosticIntentGenerator()],
            worldview=MockWorldView(),
        )
        finding = Finding(category="RabbitMQHeartbeatLost",
                          confidence=0.91,
                          affected_entities=["SERVICE:rabbitmq-prod"])
        result = planner.plan(finding, None)
        assert result.finding_id == finding.category
        assert len(result.intents) >= 1
        assert result.goal is not None

    def test_planner_traverses_goal_tree(self):
        """Planner 递归遍历 Goal Tree 的每个节点"""
        planner = Planner(
            goal_inferrer=GoalInferrer(),
            generators=[RestartIntentGenerator(), DiagnosticIntentGenerator()],
            worldview=MockWorldView(),
        )
        finding = Finding(category="RabbitMQHeartbeatLost",
                          confidence=0.91,
                          affected_entities=["SERVICE:rabbitmq-prod"])
        result = planner.plan(finding, None)
        for intent in result.intents:
            assert intent.action in ("restart_service", "collect_diagnostic", "failover")

    def test_planner_with_explicit_goal(self):
        """可以传入外部 Goal 覆盖 GoalInferrer 的推断"""
        planner = Planner(
            goal_inferrer=GoalInferrer(),
            generators=[DiagnosticIntentGenerator()],
            worldview=MockWorldView(),
        )
        explicit_goal = Goal(
            primary="collect_evidence",
            tree=GoalNode(goal_id="root", desired_state="evidence_collected",
                          entity_type="INSTANCE", entity_name="vm-1"),
        )
        finding = Finding(category="unknown", confidence=0.3)
        result = planner.plan(finding, None, goal=explicit_goal)
        assert result.goal.primary == "collect_evidence"

    def test_planner_result_includes_goal(self):
        planner = Planner(GoalInferrer(), [], MockWorldView())
        finding = Finding(confidence=0.5, affected_entities=["SERVICE:svc"])
        result = planner.plan(finding, None)
        assert result.goal is not None
        assert result.goal.tree.desired_state is not None

    def test_planner_empty_generators(self):
        """无 Generator 时返回空 intents"""
        planner = Planner(GoalInferrer(), [], MockWorldView())
        finding = Finding(confidence=0.5, affected_entities=["SERVICE:svc"])
        result = planner.plan(finding, None)
        assert result.intents == []
