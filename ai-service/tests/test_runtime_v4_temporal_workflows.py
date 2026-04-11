"""
Tests for runtime v4 Temporal workflow state helpers.
"""

from ai.runtime_v4.temporal.workflows import RunWorkflowState


def test_run_workflow_state_defaults_are_deterministic():
    state = RunWorkflowState(
        workflow_id="wf-001",
        thread_id="thread-001",
        run_id="run-001",
    )

    assert state.created_at == ""
    assert state.updated_at == ""
    assert state.last_signal_at == ""
    assert state.signals == []


def test_run_workflow_state_append_signal_uses_observed_timestamp():
    state = RunWorkflowState(
        workflow_id="wf-002",
        thread_id="thread-002",
        run_id="run-002",
    )

    state.append_signal(
        "approval",
        {"approval_id": "ap-001"},
        observed_at="2026-03-24T00:00:01Z",
    )

    assert state.last_signal_at == "2026-03-24T00:00:01Z"
    assert state.updated_at == "2026-03-24T00:00:01Z"
    assert state.signals == [
        {
            "signal_type": "approval",
            "payload": {"approval_id": "ap-001"},
            "observed_at": "2026-03-24T00:00:01Z",
        }
    ]


def test_run_workflow_state_apply_status_uses_observed_timestamp():
    state = RunWorkflowState(
        workflow_id="wf-003",
        thread_id="thread-003",
        run_id="run-003",
        status="running",
    )

    state.apply_run_status("waiting_approval", observed_at="2026-03-24T00:00:02Z")

    assert state.status == "waiting_approval"
    assert state.updated_at == "2026-03-24T00:00:02Z"
