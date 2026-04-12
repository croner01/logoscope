"""
Tests for the LangGraph planning node — run_planning().
"""
from unittest.mock import MagicMock, patch

import pytest

from ai.runtime_v4.langgraph.nodes.planning import run_planning
from ai.runtime_v4.langgraph.state import InnerGraphState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> InnerGraphState:
    defaults = dict(
        run_id="test-run",
        question="pod is crashing with CrashLoopBackOff",
        max_iterations=4,
        skill_context={
            "question": "pod is crashing with CrashLoopBackOff",
            "log_content": "CrashLoopBackOff restarting container",
            "component_type": "pod",
            "namespace": "islap",
        },
    )
    defaults.update(kwargs)
    return InnerGraphState(**defaults)


def _make_mock_step(step_id: str = "step-1"):
    step = MagicMock()
    step.step_id = step_id
    step.to_action_dict.return_value = {
        "skill_name": "mock_skill",
        "step_id": step_id,
        "title": "Mock step",
        "status": "pending",
        "command_spec": {"tool": "generic_exec", "args": {}},
        "purpose": "testing",
    }
    return step


def _make_mock_skill(name: str = "mock_skill", steps=None):
    skill = MagicMock()
    skill.name = name
    skill.display_name = name
    skill.plan_steps.return_value = steps or [_make_mock_step()]
    return skill


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRunPlanning:
    def test_increments_iteration(self):
        state = _make_state()
        new_state = run_planning(state)
        assert new_state.iteration == 1

    def test_sets_phase_to_planning(self):
        state = _make_state()
        new_state = run_planning(state)
        assert new_state.phase == "planning"

    def test_marks_done_when_max_iterations_exceeded(self):
        state = _make_state(iteration=5, max_iterations=4)
        new_state = run_planning(state)
        assert new_state.done is True

    def test_marks_done_when_no_skills_and_no_actions(self):
        state = _make_state()
        with patch("ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules", return_value=[]):
            new_state = run_planning(state)
        assert new_state.done is True

    def test_populates_actions_from_matched_skills(self):
        state = _make_state()
        mock_skill = _make_mock_skill()
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(mock_skill, 0.9)],
        ):
            new_state = run_planning(state)
        assert len(new_state.actions) > 0
        assert new_state.actions[0]["skill_name"] == "mock_skill"

    def test_does_not_duplicate_selected_skills(self):
        state = _make_state(selected_skills=["mock_skill"])
        mock_skill = _make_mock_skill(name="mock_skill")
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(mock_skill, 0.9)],
        ):
            new_state = run_planning(state)
        # Should not have added anything because skill already in selected_skills
        assert "mock_skill" in new_state.selected_skills
        assert len(new_state.actions) == 0

    def test_records_skill_selection_in_reflection(self):
        state = _make_state()
        mock_skill = _make_mock_skill(name="k8s_pod_diagnostics")
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(mock_skill, 0.8)],
        ):
            new_state = run_planning(state)
        assert "last_skill_selection" in new_state.reflection
        assert "k8s_pod_diagnostics" in new_state.reflection["last_skill_selection"]["selected"]

    def test_skips_new_skill_selection_when_pending_actions_exist(self):
        pending_action = {
            "skill_name": "existing_skill",
            "step_id": "existing-step",
            "status": "pending",
        }
        state = _make_state(actions=[pending_action])
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
        ) as mock_match:
            new_state = run_planning(state)
        # Should not have called skill matching
        mock_match.assert_not_called()
        # Existing action should still be there
        assert len(new_state.actions) == 1

    def test_multiple_skills_multiple_steps(self):
        state = _make_state()
        skill_a = _make_mock_skill("skill_a", steps=[_make_mock_step("a-1"), _make_mock_step("a-2")])
        skill_b = _make_mock_skill("skill_b", steps=[_make_mock_step("b-1")])
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(skill_a, 0.9), (skill_b, 0.7)],
        ):
            new_state = run_planning(state)
        assert len(new_state.actions) == 3
        assert "skill_a" in new_state.selected_skills
        assert "skill_b" in new_state.selected_skills

    def test_plan_steps_exception_does_not_crash(self):
        state = _make_state()
        skill = _make_mock_skill()
        skill.plan_steps.side_effect = RuntimeError("plan failed")
        with patch(
            "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
            return_value=[(skill, 0.9)],
        ):
            # Should not raise
            new_state = run_planning(state)
        assert new_state is not None
