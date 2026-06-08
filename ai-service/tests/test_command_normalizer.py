"""Tests for command/normalizer.py."""
from ai.command.normalizer import normalize_command_spec
from ai.command.spec import CommandSpec, ToolType, CommandType


class TestNormalizeCommandSpec:
    def test_normalizes_valid_generic_exec(self):
        raw = {
            "tool": "generic_exec",
            "command": "kubectl logs pod-abc -n islap --tail=100",
            "target_kind": "k8s_cluster",
            "target_identity": "pod:pod-abc/namespace:islap",
            "purpose": "查看日志",
        }
        spec = normalize_command_spec(raw)
        assert isinstance(spec, CommandSpec)
        assert spec.tool == ToolType.GENERIC_EXEC
        assert spec.command == "kubectl logs pod-abc -n islap --tail=100"
        assert spec.target_kind == "k8s_cluster"

    def test_normalizes_valid_clickhouse_query(self):
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT * FROM logs.events WHERE pod_name='x' LIMIT 10",
            "target_kind": "clickhouse_cluster",
            "target_identity": "database:logs",
            "purpose": "查询日志",
        }
        spec = normalize_command_spec(raw)
        assert spec.tool == ToolType.CLICKHOUSE_QUERY

    def test_infers_target_from_source_target(self):
        raw = {
            "tool": "generic_exec",
            "command": "kubectl logs -n islap --tail=100",
            "purpose": "查看目标 pod 日志",
        }
        source_target = {
            "pod_name": "api-gateway-abc123",
            "namespace": "islap",
            "service_name": "api-gateway",
        }
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_kind == "k8s_cluster"
        assert "api-gateway-abc123" in spec.target_identity or "islap" in spec.target_identity

    def test_infers_command_type_from_sql(self):
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT * FROM logs.events",
            "purpose": "查询",
        }
        spec = normalize_command_spec(raw)
        assert spec.command_type == CommandType.QUERY

    def test_rejects_invalid_tool(self):
        from pydantic import ValidationError
        import pytest
        raw = {"tool": "invalid_tool", "command": "ls"}
        with pytest.raises(ValidationError):
            normalize_command_spec(raw)

    def test_empty_dict_raises(self):
        from pydantic import ValidationError
        import pytest
        with pytest.raises(ValidationError):
            normalize_command_spec({})


class TestNormalizeCommandSpecClusterId:
    """target_cluster_id propagation from source_target."""

    def test_cluster_id_from_source_target(self):
        """cluster_id flows from source_target into CommandSpec."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
        }
        source_target = {"pod_name": "pod-0", "namespace": "test-ns", "cluster_id": "my-cluster"}
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_cluster_id == "my-cluster"

    def test_cluster_id_empty_when_not_in_source_target(self):
        """When source_target has no cluster_id, target_cluster_id stays empty."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
        }
        source_target = {"pod_name": "pod-0", "namespace": "test-ns"}
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_cluster_id == ""

    def test_cluster_id_empty_when_no_source_target(self):
        """When source_target is None, target_cluster_id stays empty."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
        }
        spec = normalize_command_spec(raw, source_target=None)
        assert spec.target_cluster_id == ""

    def test_raw_dict_overrides_source_target(self):
        """When raw dict has target_cluster_id, it takes priority."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
            "target_cluster_id": "from-llm",
        }
        source_target = {"pod_name": "pod-0", "namespace": "test-ns", "cluster_id": "from-source"}
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_cluster_id == "from-llm"
