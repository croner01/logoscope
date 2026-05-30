"""
Tests for OPA policy mode hardening.
"""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import policy_opa_client


def test_policy_mode_defaults_to_opa_enforced_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("EXEC_POLICY_DECISION_MODE", raising=False)
    monkeypatch.delenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES", raising=False)

    assert policy_opa_client.policy_mode() == "opa_enforced"


def test_policy_mode_rejects_local_when_non_enforced_modes_disabled(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "local")
    monkeypatch.setenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES", "false")

    assert policy_opa_client.policy_mode() == "opa_enforced"


def test_policy_mode_allows_local_when_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "local")
    monkeypatch.setenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES", "true")

    assert policy_opa_client.policy_mode() == "local"


def test_enforced_mode_keeps_local_elevate_when_opa_returns_allow(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("EXEC_POLICY_DECISION_MODE", "opa_enforced")
    monkeypatch.setenv("EXEC_POLICY_ALLOW_NON_ENFORCED_MODES", "false")
    monkeypatch.delenv("EXEC_POLICY_ENFORCE_STRICTEST_WITH_LOCAL", raising=False)

    monkeypatch.setattr(
        policy_opa_client,
        "_query_opa_decision",
        lambda _input_payload: (
            True,
            {
                "result": "allow",
                "reason": "opa allow",
                "package": "runtime.command.v1",
            },
            "",
        ),
    )

    decision = policy_opa_client.evaluate_policy_decision(
        local_result="elevate",
        local_reason="write command requires elevation",
        input_payload={
            "classification": {"command_type": "repair", "requires_write_permission": True},
        },
    )
    assert decision["result"] == "elevate"
    assert decision["engine"] == "opa"
    assert decision["source"] == "opa"
    assert decision["opa_available"] is True
    assert decision["opa_result"] == "allow"
    assert decision["local_result"] == "elevate"
