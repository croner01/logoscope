"""Tests for CommandRouter."""
from __future__ import annotations

import pytest
from ai.agent_runtime.command_router import CommandRouter


class TestCommandRouter:
    @pytest.fixture
    def router(self):
        return CommandRouter()

    def test_routes_simple_clickhouse_select_to_local(self, router):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT * FROM logs.events WHERE service_name='api' LIMIT 10",
                "target_kind": "clickhouse_cluster",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "local"
        assert "simple" in reason.lower() or "query-service" in reason.lower()

    def test_routes_complex_clickhouse_to_remote(self, router):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT service_name, COUNT(*) FROM logs.events GROUP BY service_name",
                "target_kind": "clickhouse_cluster",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_routes_kubectl_to_remote(self, router):
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl logs some-pod -n islap --tail=50",
                "target_kind": "k8s_cluster",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_routes_shell_to_remote(self, router):
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": "cat /etc/hosts",
                "target_kind": "runtime_workspace",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_routes_host_control_to_remote(self, router):
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": "systemctl status kubelet",
                "target_kind": "host_node",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_unknown_tool_defaults_to_remote(self, router):
        spec = {"tool": "unknown_tool", "args": {}}
        channel, reason = router.route(spec)
        assert channel == "remote"
        assert "unknown" in reason.lower() or "default" in reason.lower()

    def test_empty_spec_defaults_to_remote(self, router):
        channel, reason = router.route({})
        assert channel == "remote"
