import pytest
from shared_src.goal.models import GoalNode, Goal


class TestGoalNode:
    def test_goal_desired_state(self):
        """GoalNode 只描述目标状态"""
        node = GoalNode(
            goal_id="g1",
            desired_state="RabbitMQ.healthy",
            entity_type="SERVICE",
            entity_name="rabbitmq",
        )
        assert not hasattr(node, "action")       # v15: 不含 Workflow 概念
        assert not hasattr(node, "ordering")
        assert not hasattr(node, "completion_criteria")
        assert node.desired_state == "RabbitMQ.healthy"

    def test_goal_nested_tree(self):
        """Goal 支持目标状态树"""
        goal = Goal(
            primary="restore_messaging",
            tree=GoalNode(goal_id="root", desired_state="Cluster.healthy",
                          entity_type="CLUSTER", entity_name="rabbitmq-prod",
                          children=[
                              GoalNode(goal_id="mq", desired_state="RabbitMQ.healthy",
                                       entity_type="SERVICE", entity_name="rabbitmq-server"),
                          ]),
            priority=90,
        )
        assert len(goal.tree.children) == 1
        assert goal.tree.children[0].desired_state == "RabbitMQ.healthy"

    def test_goal_no_workflow_fields(self):
        """Goal 不含 Workflow 字段"""
        goal = Goal(primary="restore_messaging",
                     tree=GoalNode(goal_id="root", desired_state="healthy",
                                   entity_type="SERVICE", entity_name="svc"))
        assert not hasattr(goal, "steps")
        assert not hasattr(goal, "ordering")

    def test_goal_priority_default(self):
        goal = Goal(primary="test", tree=GoalNode(goal_id="g1", desired_state="ok",
                     entity_type="SERVICE", entity_name="svc"))
        assert goal.priority == 50

    def test_goal_reason(self):
        goal = Goal(primary="restore", tree=GoalNode(goal_id="g1", desired_state="ok",
                     entity_type="SERVICE", entity_name="svc"),
                     reason="RabbitMQ heartbeat lost")
        assert goal.reason == "RabbitMQ heartbeat lost"

    def test_goal_node_no_children_by_default(self):
        node = GoalNode(goal_id="g1", desired_state="healthy",
                         entity_type="SERVICE", entity_name="svc")
        assert node.children == []
