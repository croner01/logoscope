"""
Observing node for runtime v4 inner graph.

Reads command output stored in the last observation, extracts key evidence
(errors, resource values, status codes) guided by parse_hints, and appends
a structured evidence entry to state.evidence.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)

_EVIDENCE_MAX_CHARS = 1200  # per observation snippet stored in state


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _extract_lines_matching(text: str, patterns: List[str]) -> List[str]:
    """Return lines from *text* that match any of the plain-text patterns (case-insensitive)."""
    lines = text.splitlines()
    matched: List[str] = []
    for line in lines:
        line_lower = line.lower()
        for pat in patterns:
            if pat.lower() in line_lower:
                matched.append(line.strip())
                break
    return matched[:20]  # cap at 20 lines to keep evidence compact


def _build_evidence_snippet(output: str, parse_hints: Dict[str, Any]) -> str:
    """
    Build a compact evidence snippet from command output.

    If parse_hints contains an "extract" list, filter to matching lines.
    Otherwise, return the first N characters of output.
    """
    safe_output = _as_str(output).strip()
    if not safe_output:
        return ""

    extract_keys: List[str] = []
    hints_extract = parse_hints.get("extract")
    if isinstance(hints_extract, list):
        extract_keys = [_as_str(k) for k in hints_extract if _as_str(k)]

    if extract_keys:
        matched = _extract_lines_matching(safe_output, extract_keys)
        if matched:
            return "\n".join(matched)[:_EVIDENCE_MAX_CHARS]

    return safe_output[:_EVIDENCE_MAX_CHARS]


def _find_last_dispatched_observation(state: InnerGraphState) -> Dict[str, Any]:
    """Find the most recent observation that came from a dispatched action."""
    for obs in reversed(state.observations):
        if isinstance(obs, dict) and obs.get("from_dispatch"):
            return obs
    return {}


def _find_dispatched_action(state: InnerGraphState, step_id: str) -> Dict[str, Any]:
    """Return the action dict for the given step_id."""
    for action in state.actions:
        if isinstance(action, dict) and _as_str(action.get("step_id")) == step_id:
            return action
    return {}


def run_observing(state: InnerGraphState) -> InnerGraphState:
    """
    Observing node.

    Reads the last observation that was populated by the outer execution
    loop after running the dispatched command. Extracts evidence and updates
    the action status to "completed".

    The outer loop is expected to append an entry to ``state.observations``
    with at least::

        {
            "from_dispatch": True,
            "step_id": "<step_id>",
            "stdout": "<output>",
            "stderr": "<stderr>",
            "exit_code": 0,
            "parse_hints": {...},
        }
    """
    state.phase = "observing"

    last_obs = _find_last_dispatched_observation(state)
    if not last_obs:
        # Nothing to observe yet (outer loop hasn't populated observations)
        return state

    step_id = _as_str(last_obs.get("step_id"))
    stdout = _as_str(last_obs.get("stdout"))
    stderr = _as_str(last_obs.get("stderr"))
    exit_code = int(last_obs.get("exit_code") or 0)
    parse_hints = last_obs.get("parse_hints") or {}

    combined_output = stdout or stderr or ""
    snippet = _build_evidence_snippet(combined_output, parse_hints)

    evidence_entry: Dict[str, Any] = {
        "step_id": step_id,
        "skill_name": _as_str(last_obs.get("skill_name")),
        "title": _as_str(last_obs.get("title")),
        "exit_code": exit_code,
        "success": exit_code == 0,
        "snippet": snippet,
        "has_output": bool(combined_output.strip()),
    }
    state.evidence.append(evidence_entry)

    # Mark observation as processed so it isn't double-counted
    last_obs["observed"] = True

    # Mark the corresponding action as completed
    if step_id:
        action = _find_dispatched_action(state, step_id)
        if action:
            action["status"] = "completed"
            action["exit_code"] = exit_code

    logger.debug(
        "Observing: processed step=%r exit_code=%d snippet_len=%d run_id=%s",
        step_id,
        exit_code,
        len(snippet),
        state.run_id,
    )
    return state
