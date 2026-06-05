"""End-to-end integration tests for metadata-driven execution pipeline."""
from __future__ import annotations

from ai.agent_runtime.command_router import CommandRouter
from ai.agent_runtime.cost_preflight import CostPreflight, Decision
from ai.agent_runtime.execution_journal import ExecutionJournal
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

    def test_router_journal_preflight_pipeline(self):
        """Simulate the full command execution pipeline."""
        router = CommandRouter()
        journal = ExecutionJournal()
        preflight = CostPreflight()

        # Step 1: Route a simple log query
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT * FROM logs.events WHERE pod_name='api-gateway-abc123' LIMIT 50",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "local"

        # Step 2: Check dedup (first time → no cache)
        fp = journal.fingerprint(spec)
        assert journal.lookup(fp) is None

        # Step 3: Record execution
        journal.record(fp, "SELECT ... LIMIT 50", "clickhouse_cluster", "database:logs", 0, "找到 12 条相关日志", "...")

        # Step 4: Cost preflight should pass (small query)
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 12, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.AUTO

        # Step 5: Second attempt → dedup hit
        cached = journal.lookup(fp)
        assert cached is not None
        assert cached["summary"] == "找到 12 条相关日志"

        # Step 6: Remote command (kubectl logs on target pod)
        remote_spec = {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl logs api-gateway-abc123 -n islap --tail=200",
                "target_kind": "k8s_cluster",
                "target_identity": "pod:api-gateway-abc123/namespace:islap",
            },
        }
        channel2, _ = router.route(remote_spec)
        assert channel2 == "remote"

        # Step 7: -A flag triggers cost gate block
        wide_spec = {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -A",
                "target_kind": "k8s_cluster",
            },
        }
        result2 = preflight.evaluate(wide_spec, tracker)
        assert result2.decision == Decision.BLOCK

    def test_journal_llm_context_includes_source_target_info(self):
        journal = ExecutionJournal()
        journal.record("fp1", "kubectl logs api-gateway-abc123 -n islap --tail=100", "k8s_cluster", "pod:api-gateway-abc123/namespace:islap", 0, "发现 3 条连接超时错误", "2026-06-05T10:28:15Z ERROR connection timeout...")
        ctx = journal.context_for_llm()
        assert "kubectl logs api-gateway-abc123" in ctx
        assert "连接超时" in ctx

    def test_source_target_routing_to_target_identity(self):
        """Verify that source_target metadata can be used to construct precise target_identity."""
        source_target = {
            "pod_name": "semantic-engine-abc123",
            "namespace": "islap",
            "node_name": "node-3",
            "service_name": "semantic-engine",
        }

        # Simulate building a kubectl command spec targeting the source pod
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": f"kubectl logs {source_target['pod_name']} -n {source_target['namespace']} --tail=200",
                "target_kind": "k8s_cluster",
                "target_identity": f"pod:{source_target['pod_name']}/namespace:{source_target['namespace']}",
            },
        }

        router = CommandRouter()
        channel, _ = router.route(spec)
        assert channel == "remote"

        journal = ExecutionJournal()
        fp = journal.fingerprint(spec)
        assert fp  # should produce a valid fingerprint
        assert len(fp) == 16  # truncated sha1 hex
