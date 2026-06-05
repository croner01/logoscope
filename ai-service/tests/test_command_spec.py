"""Tests for command/spec.py."""
from ai.command.spec import (
    CommandSpec, CompiledCommand, ToolType, RiskLevel, CommandType,
)


class TestCommandSpec:
    def test_valid_generic_exec_spec(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl logs pod-abc -n islap --tail=100",
            target_kind="k8s_cluster",
            target_identity="pod:pod-abc/namespace:islap",
            purpose="查看 pod 最近日志",
            risk_level=RiskLevel.LOW,
            command_type=CommandType.QUERY,
            timeout_seconds=20,
        )
        assert spec.tool == ToolType.GENERIC_EXEC
        assert spec.risk_level == RiskLevel.LOW
        assert spec.command_type == CommandType.QUERY

    def test_valid_clickhouse_spec(self):
        spec = CommandSpec(
            tool=ToolType.CLICKHOUSE_QUERY,
            command="SELECT * FROM logs.events WHERE service_name='api' LIMIT 10",
            target_kind="clickhouse_cluster",
            target_identity="database:logs",
            purpose="查询 api 服务错误日志",
        )
        assert spec.tool == ToolType.CLICKHOUSE_QUERY
        assert spec.timeout_seconds == 20  # default

    def test_defaults(self):
        spec = CommandSpec(tool=ToolType.GENERIC_EXEC, command="ls")
        assert spec.risk_level == RiskLevel.LOW
        assert spec.command_type == CommandType.QUERY
        assert spec.target_kind == ""
        assert spec.target_identity == ""
        assert spec.purpose == ""
        assert spec.timeout_seconds == 20

    def test_timeout_bounds(self):
        from pydantic import ValidationError
        import pytest
        with pytest.raises(ValidationError):
            CommandSpec(tool=ToolType.GENERIC_EXEC, command="ls", timeout_seconds=0)
        with pytest.raises(ValidationError):
            CommandSpec(tool=ToolType.GENERIC_EXEC, command="ls", timeout_seconds=121)

    def test_compiled_command_model(self):
        spec = CommandSpec(tool=ToolType.GENERIC_EXEC, command="kubectl get pods")
        compiled = CompiledCommand(
            spec=spec,
            shell_command="kubectl get pods -n islap",
            route="remote",
            executor_profile="toolbox-k8s-readonly",
        )
        assert compiled.route == "remote"
        assert compiled.shell_command == "kubectl get pods -n islap"
        assert compiled.spec is spec

    def test_serialization(self):
        spec = CommandSpec(
            tool=ToolType.CLICKHOUSE_QUERY,
            command="SELECT 1",
            purpose="test",
        )
        d = spec.model_dump()
        assert d["tool"] == "clickhouse_query"
        assert d["command"] == "SELECT 1"
        # Round-trip
        spec2 = CommandSpec.model_validate(d)
        assert spec2.tool == spec.tool
        assert spec2.command == spec.command
