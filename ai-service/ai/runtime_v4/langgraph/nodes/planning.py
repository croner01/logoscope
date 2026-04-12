"""
Planning node for runtime v4 inner graph.

Selects diagnostic skills based on context (rule-based match + optional LLM),
then converts each skill's steps into pending actions in state.actions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)

_PLANNING_MAX_SKILLS = 3
_PLANNING_MIN_SCORE = 0.1


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _pending_action_count(state: InnerGraphState) -> int:
    return sum(
        1 for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) == "pending"
    )


def _build_skill_context_from_state(state: InnerGraphState):
    """Build a SkillContext from the inner graph state."""
    from ai.skills.base import SkillContext

    ctx_data: Dict[str, Any] = dict(state.skill_context)
    ctx_data.setdefault("question", state.question)
    return SkillContext.from_dict(ctx_data)


def _select_skills_by_rules(state: InnerGraphState) -> List[Any]:
    """Run rule-based skill matching and return (skill, score) pairs."""
    try:
        from ai.skills.matcher import match_skills_by_rules

        context = _build_skill_context_from_state(state)
        return match_skills_by_rules(
            context,
            min_score=_PLANNING_MIN_SCORE,
            max_skills=_PLANNING_MAX_SKILLS,
        )
    except Exception:
        logger.exception("Rule-based skill matching failed")
        return []


def _populate_actions_from_skills(
    state: InnerGraphState,
    skill_score_pairs: List[Any],
) -> None:
    """Expand each selected skill into SkillStep action dicts."""
    try:
        context = _build_skill_context_from_state(state)
    except Exception:
        return

    for skill, _score in skill_score_pairs:
        if skill.name in state.selected_skills:
            continue  # avoid duplicates across iterations
        try:
            steps = skill.plan_steps(context)
        except Exception:
            logger.exception("plan_steps failed for skill %r", skill.name)
            continue

        state.selected_skills.append(skill.name)
        for step in steps:
            try:
                action = step.to_action_dict(skill.name)
                state.actions.append(action)
            except Exception:
                logger.exception(
                    "Failed to serialize step %r from skill %r", step.step_id, skill.name
                )


def run_planning(state: InnerGraphState) -> InnerGraphState:
    """
    Planning node.

    Iteration 1: match skills from context, populate state.actions with their
    steps (rule-based fast path). Subsequent iterations: if pending actions
    remain, pass through; if none, mark done.
    """
    state.phase = "planning"
    state.iteration += 1

    if state.iteration > state.max_iterations:
        state.done = True
        logger.debug(
            "Planning: max iterations %d reached, done=True", state.max_iterations
        )
        return state

    # If there are already pending actions, let the acting node handle them
    if _pending_action_count(state) > 0:
        return state

    # First iteration or all previous actions consumed: select skills
    matched = _select_skills_by_rules(state)

    if matched:
        skill_names = [s.name for s, _ in matched]
        logger.info(
            "Planning: matched skills %s for run_id=%s iter=%d",
            skill_names,
            state.run_id,
            state.iteration,
        )
        _populate_actions_from_skills(state, matched)
        state.reflection["last_skill_selection"] = {
            "iteration": state.iteration,
            "selected": skill_names,
        }
    else:
        logger.info(
            "Planning: no skills matched, run_id=%s iter=%d", state.run_id, state.iteration
        )
        if not state.actions:
            state.done = True

    return state
