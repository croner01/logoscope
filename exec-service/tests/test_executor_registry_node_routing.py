"""
Tests for executor registry node-aware routing behavior.
"""

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.executor_registry import resolve_executor


@pytest.fixture(autouse=True)
def _configure_executor_templates(monkeypatch):
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_NODE_READONLY",
        "echo profile={executor_profile} node={target_node_name} cluster={target_cluster_id}",
    )
    monkeypatch.setenv(
        "EXEC_EXECUTOR_TEMPLATE__HOST_SSH_READONLY",
        "echo profile={executor_profile} target={target_identity}",
    )


def test_host_node_without_resolved_node_name_is_blocked():
    dispatch = resolve_executor(
        command="uname -a",
        executor_type="ssh_gateway",
        executor_profile="host-ssh-readonly",
        target_kind="host_node",
        target_identity="host:unknown",
        resolved_target_context={
            "execution_scope": {
                "target_kind": "host_node",
                "target_identity": "host:unknown",
                "node_name": "unknown",
            }
        },
    )
    assert dispatch["dispatch_backend"] == "target_resolution_blocked"
    assert dispatch["dispatch_ready"] is False
    assert "requires resolved node_name" in str(dispatch["dispatch_reason"]).lower()


def test_host_node_uses_preferred_node_profile_with_execution_scope():
    dispatch = resolve_executor(
        command="kubectl -n islap get pods",
        executor_type="ssh_gateway",
        executor_profile="host-ssh-readonly",
        target_kind="host_node",
        target_identity="host:worker-01",
        resolved_target_context={
            "target_id": "tgt-host-worker-01",
            "target_kind": "host_node",
            "target_identity": "host:worker-01",
            "metadata": {
                "cluster_id": "cluster-dev",
                "node_name": "worker-01",
                "preferred_executor_profiles": ["toolbox-node-readonly"],
                "risk_tier": "high",
            },
            "execution_scope": {
                "cluster_id": "cluster-dev",
                "node_name": "worker-01",
                "target_kind": "host_node",
                "target_identity": "host:worker-01",
            },
        },
    )
    assert dispatch["dispatch_backend"] == "template_executor"
    assert dispatch["dispatch_ready"] is True
    assert dispatch["requested_executor_profile"] == "host-ssh-readonly"
    assert dispatch["effective_executor_profile"] == "toolbox-node-readonly"
    assert dispatch["effective_executor_type"] == "sandbox_pod"
    assert dispatch["target_node_name"] == "worker-01"
    assert dispatch["target_cluster_id"] == "cluster-dev"
    assert "profile=toolbox-node-readonly" in str(dispatch["resolved_command"])
    assert "node=worker-01" in str(dispatch["resolved_command"])
