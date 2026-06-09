"""End-to-end integration tests for metadata-driven execution pipeline."""
from __future__ import annotations

from ai.command.compiler import compile_command
from ai.command.normalizer import normalize_command_spec
from ai.command.security import evaluate_command, SessionCostState, SecurityDecision
from ai.command.spec import CommandSpec, ToolType, CommandType
from ai.runtime.memory import SessionMemory
from ai.skills.base import SkillContext


class TestMetadataDrivenE2E:
    """Integration tests covering the full pipeline from context → route → dedup → gate."""

    def test_skill_context_receives_source_target(self):
        ctx = SkillContext.from_dict({
            "service_name": "api-gateway",
            "namespace": "islap",
            "source_target": {
                "pod_name": "api-gateway-abc123",
                "namespace": "islap",
                "node_name": "node-2",
                "host_ip": "10.0.1.10",
                "container_name": "api-gateway",
                "labels": {"app": "api-gateway", "version": "v1.2"},
                "service_name": "api-gateway",
            },
        })
        assert ctx.source_target["pod_name"] == "api-gateway-abc123"
        assert ctx.source_target["namespace"] == "islap"
        assert ctx.source_target["node_name"] == "node-2"
        assert ctx.source_target["labels"]["app"] == "api-gateway"
        assert "api-gateway-abc123" in ctx.source_target_text()
        assert "node-2" in ctx.source_target_text()

    def test_skill_context_without_source_target_is_empty(self):
        ctx = SkillContext.from_dict({
            "service_name": "api-gateway",
            "namespace": "islap",
        })
        assert ctx.source_target == {}
        assert ctx.source_target_text() == ""

    def test_pipeline_route_dedup_gate(self):
        """Simulate the full command execution pipeline with unified modules."""
        memory = SessionMemory()

        # Step 1: Normalize + route a simple log query
        spec = normalize_command_spec({
            "tool": "clickhouse_query",
            "command": "SELECT * FROM logs.events WHERE pod_name='api-gateway-abc123' LIMIT 50",
            "target_kind": "clickhouse_cluster",
            "target_identity": "database:logs",
        })
        assert spec.tool == ToolType.CLICKHOUSE_QUERY

        compiled = compile_command(spec)
        assert compiled.route == "remote"  # all CH queries → remote

        # Step 2: Security check should pass
        decision = evaluate_command(spec, session_cost=SessionCostState())
        assert decision.allowed is True

        # Step 3: Check dedup (first time → no cache)
        assert memory.is_duplicate(spec) is False

        # Step 4: Record execution
        memory.record(spec, exit_code=0, summary="找到 12 条相关日志", output_preview="...")

        # Step 5: Second attempt → dedup hit
        assert memory.is_duplicate(spec) is True

        # Step 6: Remote command (kubectl logs on target pod)
        remote_spec = normalize_command_spec({
            "tool": "generic_exec",
            "command": "kubectl logs api-gateway-abc123 -n islap --tail=200",
            "target_kind": "k8s_cluster",
            "target_identity": "pod:api-gateway-abc123/namespace:islap",
        })
        compiled2 = compile_command(remote_spec)
        assert compiled2.route == "remote"

        # Step 7: -A flag triggers cost gate block
        wide_spec = normalize_command_spec({
            "tool": "generic_exec",
            "command": "kubectl get pods -A",
            "target_kind": "k8s_cluster",
        })
        decision2 = evaluate_command(wide_spec, session_cost=SessionCostState())
        assert decision2.requires_approval is True
        assert decision2.allowed is True  # allowed but needs approval

    def test_journal_llm_context_includes_source_target_info(self):
        memory = SessionMemory()
        spec = normalize_command_spec({
            "tool": "generic_exec",
            "command": "kubectl logs api-gateway-abc123 -n islap --tail=100",
            "target_kind": "k8s_cluster",
            "target_identity": "pod:api-gateway-abc123/namespace:islap",
        })
        memory.record(spec, exit_code=0, summary="发现 3 条连接超时错误", output_preview="2026-06-05T10:28:15Z ERROR connection timeout...")
        ctx = memory.context_for_llm()
        assert "kubectl logs api-gateway-abc123" in ctx
        assert "连接超时" in ctx

    def test_source_target_routing_to_target_identity(self):
        """Verify that source_target metadata is used for target inference."""
        source_target = {
            "pod_name": "semantic-engine-abc123",
            "namespace": "islap",
            "node_name": "node-3",
            "service_name": "semantic-engine",
        }

        spec = normalize_command_spec(
            {
                "tool": "generic_exec",
                "command": "kubectl logs -n islap --tail=200",
                "purpose": "check logs",
            },
            source_target=source_target,
        )
        assert spec.target_kind == "k8s_cluster"
        assert "semantic-engine-abc123" in spec.target_identity or "islap" in spec.target_identity

        compiled = compile_command(spec)
        assert compiled.route == "remote"

        fp = memory = SessionMemory()
        fp_val = fp.fingerprint(spec)
        assert len(fp_val) == 16
