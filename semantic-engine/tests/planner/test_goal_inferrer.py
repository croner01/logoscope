import pytest
from shared_src.goal.models import GoalNode, Goal
from semantic_engine.planner.goal_inferrer import GoalInferrer
from semantic_engine.inference.finding import Finding


class MockWorldView:
    class state:
        @staticmethod
        def get_state(entity_type, entity_name):
            if entity_name == "rabbitmq-prod":
                return "ERROR"
            if entity_name == "nova-api":
                return "ACTIVE"
            return "UNKNOWN"


class TestGoalInferrer:
    def test_infer_produces_goal_tree(self):
        """GoalInferrer 产出目标状态树"""
        finding = Finding(category="RabbitMQHeartbeatLost",
                          affected_entities=["SERVICE:rabbitmq-prod"])
        inferrer = GoalInferrer()
        goal = inferrer.infer(finding, MockWorldView())
        assert goal is not None
        assert goal.tree.desired_state is not None

    def test_infer_rabbitmq_heartbeat_lost(self):
        """RabbitMQ 心跳丢失→恢复消息层"""
        finding = Finding(category="RabbitMQHeartbeatLost",
                          affected_entities=["SERVICE:rabbitmq-prod"])
        inferrer = GoalInferrer()
        goal = inferrer.infer(finding, MockWorldView())
        assert goal.primary == "restore_messaging"
        assert "healthy" in goal.tree.desired_state
        assert len(goal.tree.children) > 0

    def test_infer_low_confidence_default(self):
        """低置信度→收集证据"""
        finding = Finding(category="unknown", confidence=0.3,
                          affected_entities=["INSTANCE:vm-1"])
        inferrer = GoalInferrer()
        goal = inferrer.infer(finding, MockWorldView())
        assert goal.priority <= 50

    def test_goal_all_children_are_desired_states(self):
        """Goal 树所有节点都是目标状态（不是动作）"""
        finding = Finding(category="RabbitMQHeartbeatLost",
                          affected_entities=["SERVICE:rabbitmq-prod"])
        inferrer = GoalInferrer()
        goal = inferrer.infer(finding, MockWorldView())

        def check(node):
            assert not hasattr(node, "action")
            assert not hasattr(node, "ordering")
            assert node.desired_state is not None
            for child in node.children:
                check(child)

        check(goal.tree)
