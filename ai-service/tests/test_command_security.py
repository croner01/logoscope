"""Tests for command/security.py."""
from ai.command.security import (
    evaluate_command, SecurityDecision, SessionCostState,
    ALLOWED_HEADS, BLOCKED_OPERATORS,
)
from ai.command.spec import CommandSpec, ToolType, CommandType


class TestSecurityDecision:
    def test_allowed_simple_query(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods -n islap",
            target_kind="k8s_cluster",
        )
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.requires_elevation is False
        assert decision.command_type == CommandType.QUERY

    def test_blocked_head_not_in_allowlist(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="rm -rf /tmp/xxx",
        )
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is False
        assert "rm" in decision.reason.lower() or "not in" in decision.reason.lower()

    def test_blocked_operator_rejected(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods; cat /etc/passwd",
        )
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is False

    def test_write_command_requires_elevation(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl delete pod xxx",
            command_type=CommandType.REPAIR,
        )
        decision = evaluate_command(spec, session_cost=SessionCostState(), write_enabled=True)
        assert decision.requires_elevation is True
        assert decision.allowed is True

    def test_write_command_blocked_when_disabled(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl delete pod xxx",
            command_type=CommandType.REPAIR,
        )
        decision = evaluate_command(spec, session_cost=SessionCostState(), write_enabled=False)
        assert decision.allowed is False

    def test_all_namespaces_requires_approval(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods -A",
            target_kind="k8s_cluster",
        )
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.requires_approval is True
        assert decision.allowed is True

    def test_session_command_limit_reached(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods",
        )
        cost = SessionCostState(commands_executed=10, session_command_limit=10)
        decision = evaluate_command(spec, session_cost=cost)
        assert decision.requires_approval is True

    def test_head_normalization(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl logs pod-abc -n islap --tail=100",
        )
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is True

    def test_clickhouse_client_allowed(self):
        spec = CommandSpec(
            tool=ToolType.CLICKHOUSE_QUERY,
            command="clickhouse-client --query 'SELECT 1'",
            target_kind="clickhouse_cluster",
        )
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is True

    def test_cost_state_tracks_commands(self):
        cost = SessionCostState()
        assert cost.commands_executed == 0
        cost.commands_executed += 1
        assert cost.commands_executed == 1
