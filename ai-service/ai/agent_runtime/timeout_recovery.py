"""
Deterministic timeout recovery helpers for low-risk structured commands.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from ai.followup_command_spec import (
    build_followup_command_spec_match_key,
    compile_followup_command_spec,
    normalize_followup_command_spec,
)


_LIMIT_PATTERN = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)
_INTERVAL_PATTERN = re.compile(r"\bINTERVAL\s+(\d+)\s+(MINUTE|HOUR|DAY)\b", re.IGNORECASE)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_history_keys(history: Any) -> set[str]:
    keys: set[str] = set()
    for item in _as_list(history):
        if not isinstance(item, dict):
            continue
        match_key = _as_str(item.get("match_key")).strip()
        if match_key:
            keys.add(match_key)
    return keys


def _replace_query(spec: Dict[str, Any], query: str) -> Dict[str, Any]:
    next_spec = copy.deepcopy(spec)
    args = next_spec.get("args") if isinstance(next_spec.get("args"), dict) else {}
    args["query"] = query
    next_spec["args"] = args
    next_spec["query"] = query
    next_spec["sql"] = query
    next_spec["display_sql"] = query
    return normalize_followup_command_spec(next_spec)


def _rewrite_limit(query: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    match = _LIMIT_PATTERN.search(query)
    if not match:
        return query, None
    current_limit = _as_int(match.group(1), 0)
    if current_limit <= 0:
        return query, None
    next_limit = 0
    if current_limit > 200:
        next_limit = 200
    elif current_limit > 50:
        next_limit = 50
    if next_limit <= 0 or next_limit >= current_limit:
        return query, None
    rewritten = _LIMIT_PATTERN.sub(f"LIMIT {next_limit}", query, count=1)
    return rewritten, {
        "strategy": "shrink_limit",
        "message": f"将 LIMIT 从 {current_limit} 收敛到 {next_limit}",
    }


def _rewrite_interval(query: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    match = _INTERVAL_PATTERN.search(query)
    if not match:
        return query, None
    current_value = _as_int(match.group(1), 0)
    unit = _as_str(match.group(2)).upper()
    if current_value <= 0:
        return query, None
    minimum = {
        "MINUTE": 5,
        "HOUR": 1,
        "DAY": 1,
    }.get(unit, 1)
    divisor = {
        "MINUTE": 4,
        "HOUR": 4,
        "DAY": 2,
    }.get(unit, 2)
    next_value = max(minimum, current_value // divisor)
    if next_value >= current_value:
        return query, None
    rewritten = _INTERVAL_PATTERN.sub(f"INTERVAL {next_value} {unit}", query, count=1)
    return rewritten, {
        "strategy": "shrink_time_window",
        "message": f"将时间窗口从 INTERVAL {current_value} {unit} 收敛到 INTERVAL {next_value} {unit}",
    }


def _rewrite_explain(query: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    safe_query = _as_str(query).strip()
    if not safe_query or safe_query.upper().startswith("EXPLAIN "):
        return safe_query, None
    if not safe_query.upper().startswith("SELECT "):
        return safe_query, None
    return f"EXPLAIN {safe_query}", {
        "strategy": "switch_to_explain",
        "message": "将原始查询切换为 EXPLAIN 以先获取执行计划",
    }


def _candidate_specs(spec: Dict[str, Any], purpose: str) -> List[Dict[str, Any]]:
    query = _as_str(
        ((spec.get("args") or {}) if isinstance(spec.get("args"), dict) else {}).get("query")
        or spec.get("query")
        or spec.get("sql")
    ).strip()
    if not query:
        return []

    candidates: List[Dict[str, Any]] = []
    for rewrite in (_rewrite_interval, _rewrite_limit, _rewrite_explain):
        next_query, detail = rewrite(query)
        if not detail or next_query == query:
            continue
        candidate_spec = _replace_query(spec, next_query)
        candidates.append(
            {
                "strategy": _as_str(detail.get("strategy")).strip(),
                "message": _as_str(detail.get("message")).strip(),
                "purpose": _as_str(purpose).strip(),
                "command_spec": candidate_spec,
            }
        )
    return candidates


def attempt_timeout_recovery(
    *,
    command: str,
    command_spec: Optional[Dict[str, Any]] = None,
    purpose: str = "",
    recovery_history: Any = None,
    max_rounds: int = 2,
) -> Dict[str, Any]:
    safe_command = _as_str(command).strip()
    safe_purpose = _as_str(purpose).strip()
    safe_spec = normalize_followup_command_spec(command_spec)
    safe_rounds = max(1, min(4, _as_int(max_rounds, 2)))
    attempts: List[Dict[str, Any]] = []
    known_keys = _normalize_history_keys(recovery_history)

    if not safe_spec:
        return {
            "status": "ask_user",
            "failure_code": "command_timed_out",
            "failure_message": "command timed out without structured command_spec",
            "recovery_attempts": attempts,
        }

    candidates = _candidate_specs(safe_spec, safe_purpose)
    if not candidates:
        return {
            "status": "ask_user",
            "failure_code": "command_timed_out",
            "failure_message": "no safe timeout recovery variant available",
            "recovery_attempts": attempts,
        }

    for index, candidate in enumerate(candidates[:safe_rounds], start=1):
        candidate_spec = (
            candidate.get("command_spec")
            if isinstance(candidate.get("command_spec"), dict)
            else {}
        )
        match_key = build_followup_command_spec_match_key(candidate_spec)
        if not match_key or match_key in known_keys:
            attempts.append(
                {
                    "round": index,
                    "strategy": _as_str(candidate.get("strategy")).strip() or "timeout_recovery",
                    "ok": False,
                    "failure_code": "timeout_variant_already_tried",
                    "failure_message": _as_str(candidate.get("message")).strip() or "variant already tried",
                    "match_key": match_key,
                }
            )
            continue
        compile_result = compile_followup_command_spec(candidate_spec, run_sql_preflight=False)
        attempts.append(
            {
                "round": index,
                "strategy": _as_str(candidate.get("strategy")).strip() or "timeout_recovery",
                "ok": bool(compile_result.get("ok")),
                "failure_code": _as_str(compile_result.get("reason")).strip(),
                "failure_message": _as_str(compile_result.get("detail") or candidate.get("message")).strip(),
                "match_key": match_key,
            }
        )
        if not bool(compile_result.get("ok")):
            continue
        compiled_spec = (
            compile_result.get("command_spec")
            if isinstance(compile_result.get("command_spec"), dict)
            else candidate_spec
        )
        history_entry = {
            "strategy": _as_str(candidate.get("strategy")).strip() or "timeout_recovery",
            "message": _as_str(candidate.get("message")).strip(),
            "match_key": match_key,
            "query": _as_str(compiled_spec.get("query") or compiled_spec.get("sql")).strip(),
        }
        return {
            "status": "recovered",
            "command": _as_str(compile_result.get("command")).strip() or safe_command,
            "command_spec": compiled_spec,
            "recovery_attempts": attempts,
            "recovery_kind": _as_str(candidate.get("strategy")).strip() or "timeout_recovery",
            "history_entry": history_entry,
            "failure_code": "command_timed_out",
            "failure_message": _as_str(candidate.get("message")).strip(),
        }

    return {
        "status": "ask_user",
        "failure_code": "command_timed_out",
        "failure_message": "timeout recovery exhausted safe variants",
        "recovery_attempts": attempts,
    }
