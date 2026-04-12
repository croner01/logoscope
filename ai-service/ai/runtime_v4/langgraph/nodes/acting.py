"""
Acting node for runtime v4 inner graph.

Takes the first pending action from state.actions, validates and compiles
its command_spec, and marks it as "ready" for the outer execution layer.
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
    """Return the first action with status 'pending', respecting depends_on."""
    completed_ids = {
        _as_str(a.get("step_id"))
        for a in state.actions
        if isinstance(a, dict) and _as_str(a.get("status")) in {"completed", "skipped"}
    }

    for action in state.actions:
        if not isinstance(action, dict):
            continue
        if _as_str(action.get("status")) != "pending":
            continue
        depends_on = action.get("depends_on") or []
        if all(_as_str(dep) in completed_ids for dep in depends_on):
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

    Picks the next pending action (respecting depends_on), compiles its
    command_spec, and marks it as "dispatched" so the outer execution
    loop (agent_runtime/service.py) can pick it up.

    The actual shell execution is NOT performed here — this node only
    prepares the action for dispatch. The outer loop reads
    ``state.reflection["next_dispatch"]`` and runs the command via
    ``execute_command_tool()``.
    """
    state.phase = "acting"

    action = _find_next_pending_action(state)
    if action is None:
        # All actions consumed — nothing left to dispatch
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
    }

    logger.debug(
        "Acting: dispatching step=%r skill=%r run_id=%s",
        action.get("step_id"),
        action.get("skill_name"),
        state.run_id,
    )
    return state
