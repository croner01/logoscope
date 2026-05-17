"""
Observing node for runtime v4 inner graph.

Reads command output stored in the last observation, extracts key evidence
(errors, resource values, status codes) guided by parse_hints, and appends
a structured evidence entry to state.evidence.

New in this revision:
  - ``_classify_failure()``: 8-category failure classifier that turns raw
    exit_code / stderr / stdout into a structured failure_category + an
    alternative_strategy hint consumed by replan → planning.
  - Failed commands are recorded with failure_category and alternative_strategy
    so that planning.py can generate concrete alternative actions.
  - Observation of data_flow signal: when parse_hints["populate_data_flow"]=True
    the observing node attempts to parse the call-chain output and store
    structured rows back into skill_context["data_flow"].
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ai.runtime_v4.langgraph.state import InnerGraphState

logger = logging.getLogger(__name__)

_EVIDENCE_MAX_CHARS = 1200  # per observation snippet stored in state


# ──────────────────────────────────────────────────────────────────────────────
# Failure classification — 8 categories
# ──────────────────────────────────────────────────────────────────────────────

# Patterns that indicate a specific failure category.
# Evaluated in order; first match wins.
_FAILURE_PATTERNS: List[Tuple[str, List[str], str]] = [
    # (category, [regex patterns on stderr+stdout], alternative_strategy)
    (
        "resource_not_found",
        [
            r"not found",
            r"does not exist",
            r"no such",
            r"error.*not found",
            r"resource.*not found",
            r"unable to get",
            r"no resources found",
            r"Error from server.*NotFound",
        ],
        "broaden_scope_remove_namespace_filter",
    ),
    (
        "permission_denied",
        [
            r"permission denied",
            r"forbidden",
            r"access denied",
            r"unauthorized",
            r"Error from server.*Forbidden",
            r"RBAC.*denied",
            r"cannot.*verbs",
        ],
        "use_readonly_alternative_command",
    ),
    (
        "command_syntax_error",
        [
            r"unknown flag",
            r"invalid.*flag",
            r"unrecognized.*flag",
            r"unexpected.*argument",
            r"error.*parsing",
            r"flag provided but not defined",
            r"unknown command",
            r"invalid syntax",
            r"syntax error",
            r"Unrecognized option",
        ],
        "remove_invalid_flags_use_simpler_syntax",
    ),
    (
        "connection_failure",
        [
            r"connection refused",
            r"connection timed out",
            r"no route to host",
            r"network unreachable",
            r"dial tcp.*refused",
            r"i/o timeout",
            r"EOF",
            r"connection reset",
            r"unable to connect",
        ],
        "try_different_endpoint_or_pod_selector",
    ),
    (
        "resource_not_ready",
        [
            r"pod.*not running",
            r"container.*not ready",
            r"crashloopbackoff",
            r"imagepullbackoff",
            r"pending.*pod",
            r"unschedulable",
            r"0/\d+ nodes available",
        ],
        "check_pod_status_before_log_query",
    ),
    (
        "timeout",
        [
            r"timed? out",
            r"deadline exceeded",
            r"context deadline",
            r"request timeout",
            r"query.*timed? out",
            r"execution.*timed? out",
            r"read timeout",
        ],
        "reduce_result_limit_or_shorten_time_window",
    ),
    (
        "empty_output",
        # Detected separately by _is_empty_output(); kept here for explicit
        # stderr pattern matching (some CLIs print "0 rows" to stderr)
        [
            r"0 rows in set",
            r"no matching resources found",
            r"empty result",
        ],
        "widen_time_window_or_remove_service_filter",
    ),
]


def _classify_failure(
    exit_code: int,
    stdout: str,
    stderr: str,
    command: str = "",
) -> Tuple[str, str]:
    """
    Classify a command failure into one of 8 categories.

    Returns (failure_category, alternative_strategy).

    Categories:
      resource_not_found   – kubectl/API reports the target doesn't exist
      permission_denied    – RBAC or OS permission error
      command_syntax_error – wrong flags, bad argument order
      connection_failure   – network-level error reaching the target
      resource_not_ready   – pod/container not in Running state
      empty_output         – command succeeded but returned no useful data
      timeout              – execution timed out
      unknown_failure      – none of the above
    """
    combined = f"{stderr}\n{stdout}".lower()

    # Empty output is a soft failure — exit_code may be 0
    if _is_empty_output(exit_code, stdout, stderr):
        return "empty_output", "widen_time_window_or_remove_service_filter"

    # Only classify as failure if exit_code != 0
    if exit_code == 0:
        return "", ""  # success — no failure category

    for category, patterns, strategy in _FAILURE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, combined, re.IGNORECASE):
                return category, strategy

    return "unknown_failure", "use_generic_diagnostic_fallback"


def _is_empty_output(exit_code: int, stdout: str, stderr: str) -> bool:
    """
    Detect meaningfully empty output even when exit_code==0.

    Rules:
      - exit_code == 0 AND stdout is blank/whitespace
      - stdout contains only ClickHouse "0 rows in set" / empty table lines
      - stdout is only header lines (PrettyCompact header with no data rows)
    """
    if exit_code != 0:
        return False
    s = stdout.strip()
    if not s:
        return True
    if re.search(r"^0 rows in set", s, re.MULTILINE | re.IGNORECASE):
        return True
    # PrettyCompact empty: only separators and header, no data rows
    lines = [ln for ln in s.splitlines() if ln.strip()]
    data_lines = [
        ln for ln in lines
        if not re.match(r"^[┌─┬┐├─┼┤└─┴┘│\+\-]+$", ln.strip())
        and not re.match(r"^\s*\w[\w\s]+\s*$", ln)  # header
    ]
    if not data_lines:
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Evidence helpers
# ──────────────────────────────────────────────────────────────────────────────

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
    return matched[:20]


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


# ──────────────────────────────────────────────────────────────────────────────
# Data-flow population (Phase-1 signal)
# ──────────────────────────────────────────────────────────────────────────────

def _try_populate_data_flow(state: InnerGraphState, stdout: str) -> None:
    """
    Parse Phase-1 ClickHouse call-chain output into structured data_flow rows
    and store them back in skill_context["data_flow"].

    Expects PrettyCompact output with columns:
      timestamp, service_name, level, trace_id, request_id, message

    We parse the non-separator, non-header rows and extract fields by position.
    """
    if not stdout.strip():
        return

    # Only parse PrettyCompact output (has │ separators)
    rows: List[Dict[str, Any]] = []
    header_seen = False
    col_names: List[str] = []

    for line in stdout.splitlines():
        stripped = line.strip()
        # Skip pure separator lines
        if re.match(r"^[┌─┬┐├─┼┤└─┴┘│\+\-=]+$", stripped):
            continue

        # Detect header line (contains column names separated by │)
        if "│" in stripped and not header_seen:
            parts = [p.strip() for p in stripped.split("│") if p.strip()]
            if any(name in parts for name in ["timestamp", "service_name", "level"]):
                col_names = parts
                header_seen = True
                continue

        # Data rows
        if header_seen and "│" in stripped and col_names:
            values = [p.strip() for p in stripped.split("│") if p.strip() or True]
            # Re-split more carefully
            raw_values = stripped.split("│")
            # Remove leading/trailing empty from the split
            raw_values = raw_values[1:-1] if raw_values[0] == "" else raw_values
            values = [v.strip() for v in raw_values]
            if len(values) >= len(col_names):
                row = {col_names[i]: values[i] for i in range(len(col_names))}
                rows.append(row)

    if rows:
        existing = list(state.skill_context.get("data_flow") or [])
        existing.extend(rows)
        state.skill_context["data_flow"] = existing[:500]  # cap at 500 rows
        logger.debug(
            "Observing: populated data_flow with %d rows (total=%d) run_id=%s",
            len(rows),
            len(state.skill_context["data_flow"]),
            state.run_id,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main observing node
# ──────────────────────────────────────────────────────────────────────────────

def run_observing(state: InnerGraphState) -> InnerGraphState:
    """
    Observing node.

    Reads the last observation populated by the outer execution loop after
    running the dispatched command.  Extracts evidence, classifies failures,
    and updates action status.

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
        return state

    step_id = _as_str(last_obs.get("step_id"))
    stdout = _as_str(last_obs.get("stdout"))
    stderr = _as_str(last_obs.get("stderr"))
    exit_code = int(last_obs.get("exit_code") or 0)
    parse_hints = last_obs.get("parse_hints") or {}
    command = _as_str(last_obs.get("command") or last_obs.get("title"))
    purpose = _as_str(last_obs.get("purpose"))
    skill_name = _as_str(last_obs.get("skill_name"))
    title = _as_str(last_obs.get("title"))

    combined_output = stdout or stderr or ""
    snippet = _build_evidence_snippet(combined_output, parse_hints)

    # ── Failure classification ────────────────────────────────────────────────
    failure_category, alternative_strategy = _classify_failure(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        command=command,
    )

    is_success = exit_code == 0 and not failure_category

    # ── Build evidence entry ──────────────────────────────────────────────────
    evidence_entry: Dict[str, Any] = {
        "step_id": step_id,
        "skill_name": skill_name,
        "title": title,
        "exit_code": exit_code,
        "success": is_success,
        "snippet": snippet,
        "has_output": bool(combined_output.strip()),
        # Failure metadata (empty strings when successful)
        "failure_category": failure_category,
        "alternative_strategy": alternative_strategy,
        # For replan: include command context so it can build failure_hints
        "command": command,
        "purpose": purpose,
        "command_spec": last_obs.get("command_spec"),
        "parse_hints": dict(parse_hints),
    }
    state.evidence.append(evidence_entry)

    # Mark observation as processed so it isn't double-counted
    last_obs["observed"] = True

    # ── Mark the corresponding action ─────────────────────────────────────────
    if step_id:
        action = _find_dispatched_action(state, step_id)
        if action:
            if is_success:
                action["status"] = "completed"
            else:
                action["status"] = "failed"
                action["failure_category"] = failure_category
                action["alternative_strategy"] = alternative_strategy
            action["exit_code"] = exit_code

    # ── Phase-1 data_flow population ─────────────────────────────────────────
    if parse_hints.get("populate_data_flow") and is_success:
        _try_populate_data_flow(state, stdout)

    logger.debug(
        "Observing: step=%r exit_code=%d success=%s failure=%r snippet_len=%d run_id=%s",
        step_id,
        exit_code,
        is_success,
        failure_category or "—",
        len(snippet),
        state.run_id,
    )
    return state
