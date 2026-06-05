"""Tests for CostPreflight."""
from __future__ import annotations

import pytest
from ai.agent_runtime.cost_preflight import CostPreflight, Decision


class TestCostPreflight:
    @pytest.fixture
    def preflight(self):
        return CostPreflight()

    def test_low_cost_command_returns_auto(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-a -n islap --tail=100", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 1, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.AUTO

    def test_all_namespaces_triggers_block(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl get pods -A", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_session_command_limit_triggers_block(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl get pods", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 11, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_large_time_window_triggers_block(self, preflight):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {"query": "SELECT * FROM logs.events WHERE timestamp > now() - INTERVAL 7 DAY", "target_kind": "clickhouse_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_normal_pod_command_within_limits_is_auto(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl describe pod my-pod -n islap", "target_kind": "k8s_cluster", "target_identity": "pod:my-pod/namespace:islap"},
        }
        tracker = {"commands_executed": 5, "estimated_rows_scanned": 1000, "targets_touched": {"pod": 3, "node": 1}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.AUTO

    def test_full_scan_count_triggers_block(self, preflight):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {"query": "SELECT COUNT(*) FROM logs.events", "target_kind": "clickhouse_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_describe_all_nodes_triggers_block(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl describe nodes", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 2, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK
