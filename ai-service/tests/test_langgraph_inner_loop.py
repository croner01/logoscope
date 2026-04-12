"""
Tests for the full LangGraph inner loop — plan → act → observe → replan → done.
"""
from unittest.mock import MagicMock, patch

import pytest

from ai.runtime_v4.langgraph.graph import _run_local_pipeline, run_inner_graph
from ai.runtime_v4.langgraph.nodes.acting import run_acting
from ai.runtime_v4.langgraph.nodes.observing import run_observing
from ai.runtime_v4.langgraph.nodes.planning import run_planning
from ai.runtime_v4.langgraph.nodes.replan import run_replan
from ai.runtime_v4.langgraph.state import InnerGraphState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> InnerGraphState:
    defaults = dict(
        run_id="test-run-loop",
        question="pod is crashing",
        max_iterations=4,
        skill_context={
            "question": "pod is crashing",
            "log_content": "CrashLoopBackOff",
            "component_type": "pod",
        },
    )
    defaults.update(kwargs)
    return InnerGraphState(**defaults)


def _make_pending_action(step_id: str = "step-1", skill_name: str = "mock_skill") -> dict:
    return {
        "skill_name": skill_name,
        "step_id": step_id,
        "title": "Mock step",
        "status": "pending",
        "command_spec": {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl describe pod test",
                "target_kind": "k8s_node",
                "target_identity": "node:local",
                "timeout_s": 10,
            },
        },
        "purpose": "Testing",
    }


def _make_mock_skill_with_steps(name: str, step_id: str = "step-1"):
    step = MagicMock()
    step.step_id = step_id
    step.to_action_dict.return_value = _make_pending_action(step_id, name)
    skill = MagicMock()
    skill.name = name
    skill.display_name = name
    skill.plan_steps.return_value = [step]
    return skill


# ---------------------------------------------------------------------------
# Unit tests: individual nodes
# ---------------------------------------------------------------------------

class TestPlanningNode:
    def test_planning_increments_iteration(self):
        state = _make_state()
        result = run_planning(state)
        assert result.iteration == 1

    def test_planning_done_on_max_iterations(self):
        state = _make_state(iteration=10, max_iterations=4)
        result = run_planning(state)
        assert result.done is True


class TestActingNode:
    def test_acting_marks_pending_action_dispatched(self):
        action = _make_pending_action()
        state = _make_state(actions=[action])
        with patch("ai.followup_command_spec.compile_followup_command_spec") as mock_compile:
            mock_compile.return_value = {"ok": True, "command": "kubectl describe pod test"}
            result = run_acting(state)
        dispatched = [a for a in result.actions if a.get("status") == "dispatched"]
        assert len(dispatched) >= 1

    def test_acting_sets_next_dispatch_in_reflection(self):
        action = _make_pending_action()
        state = _make_state(actions=[action])
        with patch("ai.followup_command_spec.compile_followup_command_spec") as mock_compile:
            mock_compile.return_value = {"ok": True, "command": "kubectl describe pod test"}
            result = run_acting(state)
        assert "next_dispatch" in result.reflection

    def test_acting_with_no_pending_actions_is_noop(self):
        state = _make_state(actions=[])
        result = run_acting(state)
        assert result.reflection.get("next_dispatch") is None


class TestObservingNode:
    def test_observing_extracts_evidence(self):
        action = _make_pending_action()
        action["status"] = "dispatched"
        observation = {
            "from_dispatch": True,
            "step_id": "step-1",
            "command_spec": action["command_spec"],
            "status": "completed",
            "stdout": "Ready    True\nPodScheduled  True",
            "stderr": "",
            "exit_code": 0,
        }
        state = _make_state(actions=[action], observations=[observation])
        result = run_observing(state)
        # Should have evidence appended
        assert isinstance(result.evidence, list)
        assert len(result.evidence) > 0

    def test_observing_marks_action_completed(self):
        action = _make_pending_action()
        action["status"] = "dispatched"
        observation = {
            "from_dispatch": True,
            "step_id": "step-1",
            "command_spec": action["command_spec"],
            "status": "completed",
            "stdout": "output data",
            "stderr": "",
            "exit_code": 0,
        }
        state = _make_state(actions=[action], observations=[observation])
        result = run_observing(state)
        completed = [a for a in result.actions if a.get("status") == "completed"]
        assert len(completed) >= 1


class TestReplanNode:
    def test_replan_sets_done_on_max_iterations(self):
        state = _make_state(iteration=5, max_iterations=4)
        result = run_replan(state)
        assert result.done is True

    def test_replan_continues_when_not_converged(self):
        action = _make_pending_action()
        action["status"] = "pending"
        state = _make_state(iteration=1, actions=[action])
        result = run_replan(state)
        assert result.done is False

    def test_replan_marks_done_when_all_completed_and_sufficient_evidence(self):
        action = _make_pending_action()
        action["status"] = "completed"
        state = _make_state(
            iteration=1,
            actions=[action],
            evidence=[{"step_id": "step-1", "snippet": "Pod running", "success": True}],
        )
        result = run_replan(state)
        assert result.done is True


# ---------------------------------------------------------------------------
# Integration tests: _run_local_pipeline
# ---------------------------------------------------------------------------

class TestRunLocalPipeline:
    def test_pipeline_terminates_when_no_skills_match(self):
        state = _make_state()
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[],
        ):
            result = _run_local_pipeline(state)
        assert result.done is True

    def test_pipeline_terminates_within_max_iterations(self):
        state = _make_state(max_iterations=2)
        mock_skill = _make_mock_skill_with_steps("k8s_pod")
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(mock_skill, 0.9)],
        ):
            result = _run_local_pipeline(state)
        assert result.done is True

    def test_pipeline_populates_actions_from_matched_skill(self):
        state = _make_state(max_iterations=1)
        mock_skill = _make_mock_skill_with_steps("k8s_pod")
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(mock_skill, 0.9)],
        ):
            result = _run_local_pipeline(state)
        assert len(result.actions) > 0

    def test_run_inner_graph_returns_state(self):
        state = _make_state()
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[],
        ):
            result = run_inner_graph(state)
        assert isinstance(result, InnerGraphState)
        assert result.done is True
