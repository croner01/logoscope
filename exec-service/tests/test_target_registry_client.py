"""
Tests for exec-service target registry client.
"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.target_registry_client import evaluate_target_registry_gate, target_registry_mode


def test_target_registry_mode_defaults_disabled_under_pytest(monkeypatch):
    monkeypatch.delenv("EXEC_TARGET_REGISTRY_MODE", raising=False)
    assert target_registry_mode() == "disabled"


def test_target_registry_disabled_short_circuit(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "disabled")
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
    )
    assert result["enabled"] is False
    assert result["result"] == "allow"
    assert result["applied"] is False


def test_target_registry_enforced_lookup_failure_failsafe(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setattr(
        "core.target_registry_client._http_json",
        lambda **_: (False, {}, "unreachable:ai-service"),
    )
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
    )
    assert result["enabled"] is True
    assert result["applied"] is True
    assert result["result"] == "manual_required"
    assert "resolve unavailable" in str(result["reason"]).lower()


def test_target_registry_enforced_allow(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    def _fake_http_json(**kwargs):
        return (
            True,
            {
                "resolution": {
                    "target_id": "tgt-k8s-islap",
                    "registered": True,
                    "status": "active",
                    "result": "allow",
                    "reason": "matched",
                    "missing_capabilities": [],
                    "matched_capabilities": ["read_logs"],
                    "target": {"target_id": "tgt-k8s-islap"},
                }
            },
            "",
        )

    monkeypatch.setattr("core.target_registry_client._http_json", _fake_http_json)
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
        action_id="act-001",
    )
    assert result["result"] == "allow"
    assert result["registered"] is True
    assert result["target_id"] == "tgt-k8s-islap"
    assert result["matched_capabilities"] == ["read_logs"]


def test_target_registry_falls_back_to_legacy_lookup_when_identity_route_404(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    calls = {"count": 0}

    def _fake_http_json(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return False, {}, "http_404:not found"
        if calls["count"] == 2:
            return (
                True,
                {
                    "targets": [
                        {
                            "target_id": "tgt-k8s-islap",
                            "target_kind": "k8s_cluster",
                            "target_identity": "namespace:islap",
                            "status": "active",
                        }
                    ]
                },
                "",
            )
        return (
            True,
            {
                "resolution": {
                    "target_id": "tgt-k8s-islap",
                    "registered": True,
                    "status": "active",
                    "result": "allow",
                    "reason": "matched",
                    "missing_capabilities": [],
                    "matched_capabilities": ["read_logs"],
                    "target": {"target_id": "tgt-k8s-islap"},
                }
            },
            "",
        )

    monkeypatch.setattr("core.target_registry_client._http_json", _fake_http_json)
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
    )
    assert result["result"] == "allow"
    assert result["registered"] is True
    assert result["target_id"] == "tgt-k8s-islap"


def test_target_registry_identity_ambiguity_forces_manual(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")
    monkeypatch.setattr(
        "core.target_registry_client._http_json",
        lambda **_: (
            True,
            {
                "resolution": {
                    "target_id": "",
                    "registered": True,
                    "status": "ambiguous",
                    "result": "manual_required",
                    "reason": "target identity matched multiple targets",
                    "missing_capabilities": ["read_logs"],
                    "matched_capabilities": [],
                    "ambiguous_targets": ["tgt-k8s-a", "tgt-k8s-b"],
                }
            },
            "",
        ),
    )
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
    )
    assert result["result"] == "manual_required"
    assert result["status"] == "ambiguous"
    assert result["ambiguous_targets"] == ["tgt-k8s-a", "tgt-k8s-b"]


def test_target_registry_audit_mode_does_not_block(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "audit")
    monkeypatch.setattr(
        "core.target_registry_client._http_json",
        lambda **_: (False, {}, "unreachable:ai-service"),
    )
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="k8s_cluster",
        target_identity="namespace:islap",
        required_capabilities=["read_logs"],
    )
    assert result["enabled"] is True
    assert result["applied"] is False
    assert result["result"] == "allow"
    assert "audit mode" in str(result["reason"]).lower()


def test_target_registry_enforced_host_metadata_missing_forces_manual(monkeypatch):
    monkeypatch.setenv("EXEC_TARGET_REGISTRY_MODE", "enforced")

    def _fake_http_json(**_kwargs):
        return (
            True,
            {
                "resolution": {
                    "target_id": "tgt-host-01",
                    "target_kind": "host_node",
                    "target_identity": "host:worker-01",
                    "registered": True,
                    "status": "active",
                    "result": "allow",
                    "reason": "matched",
                    "missing_capabilities": [],
                    "matched_capabilities": ["read_host_state"],
                    "metadata_contract": {
                        "required_keys": [
                            "cluster_id",
                            "node_name",
                            "preferred_executor_profiles",
                            "risk_tier",
                        ],
                        "missing_required_keys": ["node_name"],
                        "metadata": {
                            "cluster_id": "cluster-dev",
                            "preferred_executor_profiles": ["toolbox-node-readonly"],
                            "risk_tier": "high",
                        },
                        "execution_scope": {
                            "cluster_id": "cluster-dev",
                            "target_kind": "host_node",
                            "target_identity": "host:worker-01",
                        },
                    },
                    "target": {"target_id": "tgt-host-01"},
                }
            },
            "",
        )

    monkeypatch.setattr("core.target_registry_client._http_json", _fake_http_json)
    result = evaluate_target_registry_gate(
        target_id="",
        target_kind="host_node",
        target_identity="host:worker-01",
        required_capabilities=["read_host_state"],
    )
    assert result["result"] == "manual_required"
    assert "metadata missing required fields" in str(result["reason"]).lower()
    assert result["metadata_contract"]["missing_required_keys"] == ["node_name"]
