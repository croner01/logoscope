"""
Replan node for runtime v4 inner graph.

Evaluates whether collected evidence is sufficient to answer the original
question. If so, marks state.done = True. Otherwise, checks if more pending
actions are available and loops back to planning.

New in this revision:
  - ``_cancel_actions_blocked_by_failed_deps()``: any pending action whose
    depends_on chain includes a failed step is auto-cancelled (status=skipped)
    to avoid acting from waiting forever on a dependency that will never pass.
  - ``_build_failure_hints_for_planning()``: scans evidence for failed steps
    and writes structured hints to state.reflection["failure_hints"] so that
    planning.py can generate concrete alternative actions.
  - Failed skills are removed from state.selected_skills so that planning can
    re-attempt them with a different strategy on the next iteration.
  - Convergence heuristic: we require at least _MIN_SUCCESSFUL_OBSERVATIONS
    successful evidence entries AND no unresolved failure_hints to declare done.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)

# Minimum successful observations before we can declare evidence sufficient
_MIN_SUCCESSFUL_OBSERVATIONS = 1

# Failure categories that are worth retrying with an alternative strategy.
# (unknown_failure is excluded because we don't have a reliable alternative.)
_RETRIABLE_FAILURE_CATEGORIES = {
    "resource_not_found",
    "permission_denied",
    "command_syntax_error",
    "connection_failure",
    "resource_not_ready",
    "empty_output",
    "timeout",
}


# ──────────────────────────────────────────────────────────────────────────────
# Small utilities
# ──────────────────────────────────────────────────────────────────────────────

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
    return _count_dispatched_actions(state) > 0


def _get_failed_step_ids(state: InnerGraphState) -> Set[str]:
    """Return step_ids of all actions marked as 'failed'."""
    return {
        _as_str(a.get("step_id"))
        for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) == "failed"
        and _as_str(a.get("step_id"))
    }


def _get_completed_or_skipped_step_ids(state: InnerGraphState) -> Set[str]:
    return {
        _as_str(a.get("step_id"))
        for a in state.actions
        if isinstance(a, dict)
        and _as_str(a.get("status")) in {"completed", "skipped", "failed"}
        and _as_str(a.get("step_id"))
    }


# ──────────────────────────────────────────────────────────────────────────────
# Cancel pending actions whose dependency chain is broken by a failure
# ──────────────────────────────────────────────────────────────────────────────

def _cancel_actions_blocked_by_failed_deps(state: InnerGraphState) -> int:
    """
    Any pending action whose depends_on list contains a failed step_id is
    unreachable — cancel it (status = "skipped") so the acting node never
    waits on it.

    Returns the number of actions cancelled.
    """
    failed_ids = _get_failed_step_ids(state)
    if not failed_ids:
        return 0

    cancelled = 0
    for action in state.actions:
        if not isinstance(action, dict):
            continue
        if _as_str(action.get("status")) != "pending":
            continue
        depends_on = action.get("depends_on") or []
        if any(_as_str(dep) in failed_ids for dep in depends_on):
            action["status"] = "skipped"
            action["skip_reason"] = "dependency_failed"
            cancelled += 1
            logger.debug(
                "Replan: cancelled action %r (depends on failed step(s) %s) run_id=%s",
                _as_str(action.get("step_id")),
                [d for d in depends_on if _as_str(d) in failed_ids],
                state.run_id,
            )

    return cancelled


# ──────────────────────────────────────────────────────────────────────────────
# Build failure_hints for the planning node
# ──────────────────────────────────────────────────────────────────────────────

def _build_failure_hints_for_planning(state: InnerGraphState) -> List[Dict[str, Any]]:
    """
    Scan evidence entries for failures and build structured hint dicts that
    planning.py will consume to generate alternative actions.

    A hint dict contains:
      step_id, skill_name, title, command, purpose,
      command_spec, parse_hints,
      failure_category, alternative_strategy
    """
    # Collect the step_ids for which we already have an alternative
    # (marked is_alternative=True) to avoid generating duplicates
    existing_alt_for: Set[str] = {
        _as_str(a.get("replaces_step_id"))
        for a in state.actions
        if isinstance(a, dict) and a.get("is_alternative")
        and _as_str(a.get("replaces_step_id"))
    }

    hints: List[Dict[str, Any]] = []

    for entry in state.evidence:
        if not isinstance(entry, dict):
            continue
        if entry.get("success"):
            continue  # not a failure

        failure_category = _as_str(entry.get("failure_category"))
        alternative_strategy = _as_str(entry.get("alternative_strategy"))
        step_id = _as_str(entry.get("step_id"))

        if not failure_category or not step_id:
            continue
        if failure_category not in _RETRIABLE_FAILURE_CATEGORIES:
            continue
        if step_id in existing_alt_for:
            continue  # already have an alternative for this step

        hint: Dict[str, Any] = {
            "step_id": step_id,
            "skill_name": _as_str(entry.get("skill_name")),
            "title": _as_str(entry.get("title")),
            "command": _as_str(entry.get("command")),
            "purpose": _as_str(entry.get("purpose")),
            "command_spec": entry.get("command_spec"),
            "parse_hints": entry.get("parse_hints") or {},
            "failure_category": failure_category,
            "alternative_strategy": alternative_strategy,
        }
        hints.append(hint)

    return hints


# ──────────────────────────────────────────────────────────────────────────────
# Reset failed skills so planning can re-attempt them
# ──────────────────────────────────────────────────────────────────────────────

def _reset_failed_skills_from_selected(state: InnerGraphState) -> None:
    """
    Remove skills from state.selected_skills that had ALL their steps fail,
    so planning can re-select them with a different configuration.

    A skill is considered "completely failed" if every one of its steps has
    status == "failed" (none succeeded or is still pending).
    """
    # Map skill_name → [step statuses]
    skill_step_statuses: Dict[str, List[str]] = {}
    for action in state.actions:
        if not isinstance(action, dict):
            continue
        sname = _as_str(action.get("skill_name"))
        sstatus = _as_str(action.get("status"))
        if sname:
            skill_step_statuses.setdefault(sname, []).append(sstatus)

    fully_failed_skills = [
        sname
        for sname, statuses in skill_step_statuses.items()
        if statuses and all(s in {"failed", "skipped"} for s in statuses)
    ]

    for sname in fully_failed_skills:
        if sname in state.selected_skills:
            state.selected_skills.remove(sname)
            logger.info(
                "Replan: removed fully-failed skill %r from selected_skills "
                "to allow re-planning run_id=%s",
                sname,
                state.run_id,
            )


# ──────────────────────────────────────────────────────────────────────────────
# Summary helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_replan_summary(state: InnerGraphState) -> str:
    lines: List[str] = []
    for entry in state.evidence[-6:]:
        if not isinstance(entry, dict):
            continue
        step = _as_str(entry.get("step_id"))
        ok = "OK" if entry.get("success") else f"FAIL[{entry.get('failure_category', '?')}]"
        snippet = _as_str(entry.get("snippet", ""))[:120].replace("\n", " ")
        lines.append(f"[{ok}] {step}: {snippet}")
    return "\n".join(lines) if lines else "no evidence yet"


# ──────────────────────────────────────────────────────────────────────────────
# Main replan node
# ──────────────────────────────────────────────────────────────────────────────

def run_replan(state: InnerGraphState) -> InnerGraphState:
    """
    Replan node.

    Decision logic (in order):
    1. If max_iterations reached → done.
    2. If a dispatched action hasn't been observed yet → wait.
    3. Cancel pending actions whose dependency chain is broken.
    4. Build failure_hints for planning and write to state.reflection.
    5. Reset fully-failed skills from selected_skills.
    6. If pending actions remain → continue (planning/acting handles them).
    7. If we have sufficient evidence AND no unresolved retriable failures → done.
    8. Otherwise → loop back to planning (it will use failure_hints).
    """
    state.phase = "replan"

    # 1. Guard: max iterations
    if state.iteration >= state.max_iterations:
        state.done = True
        logger.debug("Replan: max iterations reached, done=True run_id=%s", state.run_id)
        return state

    # 2. If there's an in-flight dispatched action that hasn't been observed,
    #    wait for the outer loop to call observing first
    if _has_unobserved_dispatched_action(state):
        logger.debug(
            "Replan: dispatched action pending observation, continuing run_id=%s",
            state.run_id,
        )
        return state

    # 3. Cancel actions blocked by failed dependencies
    cancelled = _cancel_actions_blocked_by_failed_deps(state)
    if cancelled:
        logger.info(
            "Replan: cancelled %d action(s) blocked by failed deps run_id=%s",
            cancelled,
            state.run_id,
        )

    # 4. Build failure hints and persist in reflection for planning
    failure_hints = _build_failure_hints_for_planning(state)
    if failure_hints:
        state.reflection["failure_hints"] = failure_hints
        logger.info(
            "Replan: wrote %d failure_hints to reflection run_id=%s",
            len(failure_hints),
            state.run_id,
        )
    else:
        # Clear stale hints from previous iterations
        state.reflection.pop("failure_hints", None)

    # 5. Reset fully-failed skills so planning can re-attempt them
    _reset_failed_skills_from_selected(state)

    # 6. Pending actions still in queue — planning/acting will handle them
    if _count_pending_actions(state) > 0:
        logger.debug(
            "Replan: %d pending actions remain, continuing run_id=%s",
            _count_pending_actions(state),
            state.run_id,
        )
        return state

    # 7. Evaluate evidence sufficiency
    successful = _count_successful_evidence(state)
    retriable_hints = [
        h for h in failure_hints
        if _as_str(h.get("failure_category")) in _RETRIABLE_FAILURE_CATEGORIES
    ]

    if successful >= _MIN_SUCCESSFUL_OBSERVATIONS and not retriable_hints:
        state.done = True
        summary = _build_replan_summary(state)
        state.reflection["replan_conclusion"] = (
            f"Collected {successful} successful observation(s); "
            f"no retriable failures remain. Converged.\n{summary}"
        )
        logger.info(
            "Replan: converged with %d successful observations, done=True run_id=%s",
            successful,
            state.run_id,
        )
    elif successful >= _MIN_SUCCESSFUL_OBSERVATIONS and retriable_hints:
        # We have some evidence, but failures exist that planning can retry
        state.reflection["replan_conclusion"] = (
            f"{successful} successful observation(s) but "
            f"{len(retriable_hints)} retriable failure(s); re-entering planning."
        )
        logger.info(
            "Replan: %d successes + %d retriable failures — re-entering planning run_id=%s",
            successful,
            len(retriable_hints),
            state.run_id,
        )
    else:
        # Not enough evidence and no pending actions — planning will try more skills
        state.reflection["replan_conclusion"] = (
            f"Only {successful} successful observation(s); "
            f"{len(retriable_hints)} retriable failure(s); seeking more evidence."
        )
        logger.info(
            "Replan: insufficient evidence (%d successful, %d retriable hints), "
            "re-entering planning run_id=%s",
            successful,
            len(retriable_hints),
            state.run_id,
        )

    return state
