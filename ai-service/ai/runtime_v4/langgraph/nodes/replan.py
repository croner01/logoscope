"""
Replan node for runtime v4 inner graph.

Evaluates whether collected evidence is sufficient to answer the original
question. If so, marks state.done = True. Otherwise, checks if more pending
actions are available and loops back to planning.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)

# Minimum number of successful observations before we consider evidence sufficient
_MIN_SUCCESSFUL_OBSERVATIONS = 1


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _count_pending_actions(state: InnerGraphState) -> int:
    return sum(
        1 for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) == "pending"
    )


def _count_dispatched_actions(state: InnerGraphState) -> int:
    return sum(
        1 for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) == "dispatched"
    )


def _count_successful_evidence(state: InnerGraphState) -> int:
    return sum(1 for e in state.evidence if isinstance(e, dict) and e.get("success"))


def _has_unobserved_dispatched_action(state: InnerGraphState) -> bool:
    """True if there's a dispatched action whose output has not yet been observed."""
    return _count_dispatched_actions(state) > 0


def _build_replan_summary(state: InnerGraphState) -> str:
    """Produce a short text summary of evidence collected so far."""
    lines: List[str] = []
    for entry in state.evidence[-4:]:  # show last 4 evidence entries
        if not isinstance(entry, dict):
            continue
        step = _as_str(entry.get("step_id"))
        ok = "OK" if entry.get("success") else "FAIL"
        snippet = _as_str(entry.get("snippet", ""))[:120].replace("\n", " ")
        lines.append(f"[{ok}] {step}: {snippet}")
    return "\n".join(lines) if lines else "no evidence yet"


def run_replan(state: InnerGraphState) -> InnerGraphState:
    """
    Replan node.

    Decision logic (in order):
    1. If max_iterations reached → done.
    2. If a dispatched action hasn't been observed yet → wait (don't done,
       don't add new actions; the outer loop will re-enter after observation).
    3. If pending actions remain → continue (planning node will handle them).
    4. If we have sufficient evidence (≥ MIN_SUCCESSFUL_OBSERVATIONS) → done.
    5. Otherwise → not done, planning node will try to match more skills.
    """
    state.phase = "replan"

    # Guard: max iterations
    if state.iteration >= state.max_iterations:
        state.done = True
        logger.debug("Replan: max iterations reached, done=True run_id=%s", state.run_id)
        return state

    # If there's an in-flight dispatched action that hasn't been observed,
    # we mustn't mark done — the outer loop will call observing next
    if _has_unobserved_dispatched_action(state):
        logger.debug("Replan: dispatched action pending observation, continuing run_id=%s", state.run_id)
        return state

    # Pending actions still in queue — planning/acting will handle them
    if _count_pending_actions(state) > 0:
        logger.debug(
            "Replan: %d pending actions remain, continuing run_id=%s",
            _count_pending_actions(state),
            state.run_id,
        )
        return state

    # Evaluate evidence sufficiency
    successful = _count_successful_evidence(state)
    if successful >= _MIN_SUCCESSFUL_OBSERVATIONS:
        state.done = True
        summary = _build_replan_summary(state)
        state.reflection["replan_conclusion"] = (
            f"Collected {successful} successful observation(s); converged.\n{summary}"
        )
        logger.info(
            "Replan: converged with %d successful observations, done=True run_id=%s",
            successful,
            state.run_id,
        )
    else:
        # Not enough evidence and no pending actions — planning will try more skills
        state.reflection["replan_conclusion"] = (
            f"Only {successful} successful observation(s); seeking more evidence."
        )
        logger.info(
            "Replan: insufficient evidence (%d successful), re-entering planning run_id=%s",
            successful,
            state.run_id,
        )

    return state
