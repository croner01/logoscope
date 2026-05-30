"""
Acting node for runtime v4 inner graph.

Takes the first pending action from state.actions, validates and compiles
its command_spec, and marks it as "ready" for the outer execution layer.

New in this revision:
  - ``_find_next_pending_action()`` now skips actions whose depends_on list
    contains a *failed* step_id (in addition to still-incomplete ones).
    This closes the loop on the "AI waiting forever for a dependency that will
    never complete" bug without requiring replan to cancel them first.
  - Skips actions already marked "skipped" (cancelled by replan).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _find_next_pending_action(state: InnerGraphState) -> Optional[Dict[str, Any]]:
    """
    Return the first action with status 'pending' whose depends_on chain is
    fully satisfied.

    An action's dependency is satisfied when the dependency step has status
    'completed' OR 'skipped'.  A dependency that is 'failed' means the chain
    is broken — we skip the action (mark it skipped) and move on.

    This ensures the acting node never gets stuck dispatching a command that
    can never succeed because its prerequisite already failed.
    """
    completed_or_skipped_ids = {
        _as_str(a.get("step_id"))
        for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) in {"completed", "skipped"}
    }
    failed_ids = {
        _as_str(a.get("step_id"))
        for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) == "failed"
    }

    for action in state.actions:
        if not isinstance(action, dict):
            continue
        if _as_str(action.get("status")) != "pending":
            continue

        depends_on = action.get("depends_on") or []

        # Check if any dependency has failed — if so, auto-skip this action
        failed_deps = [
            _as_str(dep) for dep in depends_on
            if _as_str(dep) in failed_ids
        ]
        if failed_deps:
            # Auto-cancel so we don't loop indefinitely
            action["status"] = "skipped"
            action["skip_reason"] = f"dependency_failed: {failed_deps}"
            logger.debug(
                "Acting: auto-skipped %r because dependencies %s failed run_id=%s",
                _as_str(action.get("step_id")),
                failed_deps,
                state.run_id,
            )
            continue  # look for the next pending action

        # Check if all dependencies are satisfied (completed or skipped)
        if all(_as_str(dep) in completed_or_skipped_ids for dep in depends_on):
            return action

    return None


def _compile_command_spec(command_spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and normalise the command_spec via the existing compiler.
    Falls back to the raw spec if the compiler is unavailable.
    """
    try:
        from ai.followup_command_spec import (
            compile_followup_command_spec,
            normalize_followup_command_spec,
        )

        normalized = normalize_followup_command_spec(command_spec)
        compiled = compile_followup_command_spec(normalized)
        return compiled if isinstance(compiled, dict) else command_spec
    except Exception:
        logger.debug("compile_followup_command_spec unavailable, using raw spec")
        return command_spec


def run_acting(state: InnerGraphState) -> InnerGraphState:
    """
    Acting node.

    Picks the next pending action (respecting depends_on and skipping those
    whose dependencies failed), compiles its command_spec, and marks it as
    "dispatched" so the outer execution loop (agent_runtime/service.py) can
    pick it up.

    The actual shell execution is NOT performed here — this node only
    prepares the action for dispatch.  The outer loop reads
    ``state.reflection["next_dispatch"]`` and runs the command via
    ``execute_command_tool()``.
    """
    state.phase = "acting"

    action = _find_next_pending_action(state)
    if action is None:
        # All actions consumed (or blocked) — nothing left to dispatch
        state.reflection.pop("next_dispatch", None)
        return state

    raw_spec = action.get("command_spec") or {}
    compiled_spec = _compile_command_spec(raw_spec)

    # Mark action as in-flight
    action["status"] = "dispatched"
    action["compiled_command_spec"] = compiled_spec

    # Signal to the outer loop which action to execute next
    state.reflection["next_dispatch"] = {
        "step_id": _as_str(action.get("step_id")),
        "skill_name": _as_str(action.get("skill_name")),
        "title": _as_str(action.get("title")),
        "purpose": _as_str(action.get("purpose")),
        "command_spec": compiled_spec,
        "parse_hints": action.get("parse_hints") or {},
        # Forward alternative metadata so outer loop can log it
        "is_alternative": bool(action.get("is_alternative")),
        "replaces_step_id": _as_str(action.get("replaces_step_id")),
        "failure_category": _as_str(action.get("failure_category")),
    }

    logger.debug(
        "Acting: dispatching step=%r skill=%r alt=%s run_id=%s",
        action.get("step_id"),
        action.get("skill_name"),
        action.get("is_alternative", False),
        state.run_id,
    )
    return state
