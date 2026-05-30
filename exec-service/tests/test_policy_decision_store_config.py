"""
Tests for policy decision store backend safety switches.
"""

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import policy_decision_store as store


def _record_minimal_decision() -> None:
    store.record_policy_decision(
        session_id="sess-safety-001",
        message_id="msg-safety-001",
        action_id="act-safety-001",
        run_id="",
        command_run_id="",
        command="echo safety-check",
        purpose="validate backend safety switch",
        command_type="query",
        risk_level="low",
        command_family="shell",
        approval_policy="auto_execute",
        target_kind="host_node",
        target_identity="host:primary",
        executor_type="sandbox_pod",
        executor_profile="busybox-readonly",
        dispatch_backend="template_executor",
        dispatch_mode="remote_template",
        dispatch_reason="unit-test",
        dispatch_ready=True,
        dispatch_degraded=False,
        whitelist_match=True,
        whitelist_reason="unit-test",
        status="ok",
        result="allow",
        reason="unit-test",
        policy_engine="python-inline",
        policy_package="runtime.command.v1",
        input_payload={},
    )


def test_sqlite_backend_can_be_hard_disabled(monkeypatch):
    monkeypatch.setenv("EXEC_POLICY_DECISION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("EXEC_POLICY_DECISION_SQLITE_ENABLED", "false")

    with pytest.raises(RuntimeError, match="sqlite backend is disabled"):
        _record_minimal_decision()


def test_sqlite_disable_switch_does_not_break_memory_backend(monkeypatch):
    monkeypatch.setenv("EXEC_POLICY_DECISION_STORE_BACKEND", "memory")
    monkeypatch.setenv("EXEC_POLICY_DECISION_SQLITE_ENABLED", "false")

    _record_minimal_decision()
