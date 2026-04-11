"""
Deterministic recovery helpers for runtime command execution.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai.followup_command import (
    _normalize_followup_command_line,
    _repair_clickhouse_query_text,
    _resolve_followup_command_meta,
)
from ai.followup_command_spec import compile_followup_command_spec, normalize_followup_command_spec

_NON_RECOVERABLE_STRUCTURED_FAILURE_CODES = {
    "glued_sql_tokens",
    "unsupported_clickhouse_readonly_query",
    "clickhouse_multi_statement_not_allowed",
    "pod_selector_requires_shell",
}


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


def _normalize_diagnosis_contract(raw: Any) -> Dict[str, Any]:
    safe = raw if isinstance(raw, dict) else {}
    return {
        "fault_summary": _as_str(safe.get("fault_summary")).strip(),
        "evidence_gaps": [
            _as_str(item).strip()
            for item in _as_list(safe.get("evidence_gaps"))
            if _as_str(item).strip()
        ][:8],
        "execution_plan": [
            _as_str(item).strip()
            for item in _as_list(safe.get("execution_plan"))
            if _as_str(item).strip()
        ][:8],
        "why_command_needed": _as_str(safe.get("why_command_needed")).strip(),
    }


def _diagnosis_contract_missing_fields(contract: Any) -> List[str]:
    safe = _normalize_diagnosis_contract(contract)
    missing: List[str] = []
    if not safe["fault_summary"]:
        missing.append("fault_summary")
    if not safe["evidence_gaps"]:
        missing.append("evidence_gaps")
    if not safe["execution_plan"]:
        missing.append("execution_plan")
    if not safe["why_command_needed"]:
        missing.append("why_command_needed")
    return missing


def _compile_structured_spec(spec: Dict[str, Any], *, run_sql_preflight: bool) -> Dict[str, Any]:
    compile_result = compile_followup_command_spec(spec, run_sql_preflight=run_sql_preflight)
    if bool(compile_result.get("ok")):
        return {
            "ok": True,
            "command_spec": (
                compile_result.get("command_spec")
                if isinstance(compile_result.get("command_spec"), dict)
                else spec
            ),
            "command": _normalize_followup_command_line(compile_result.get("command")),
        }
    detail = _as_str(compile_result.get("detail")).strip()
    reason = _as_str(compile_result.get("reason"), "compile failed").strip()
    return {
        "ok": False,
        "failure_code": reason,
        "failure_message": reason if not detail else f"{reason}: {detail}",
    }


def _repair_structured_clickhouse_query_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_followup_command_spec(spec)
    if not normalized:
        return {}
    args = normalized.get("args") if isinstance(normalized.get("args"), dict) else {}
    query = _as_str(args.get("query") or normalized.get("query") or normalized.get("sql")).strip()
    if not query:
        return normalized
    repaired_query = _repair_clickhouse_query_text(query)
    if not repaired_query or repaired_query == query:
        return normalized
    repaired: Dict[str, Any] = {
        **normalized,
        "args": {
            **args,
            "query": repaired_query,
        },
        "query": repaired_query,
        "sql": repaired_query,
        "execution_sql": repaired_query,
        "display_sql": repaired_query,
    }
    return repaired


def attempt_command_recovery(
    *,
    command: str,
    command_spec: Optional[Dict[str, Any]] = None,
    diagnosis_contract: Optional[Dict[str, Any]] = None,
    purpose: str = "",
    failure_code: str = "",
    failure_message: str = "",
    max_rounds: int = 2,
) -> Dict[str, Any]:
    safe_command = _normalize_followup_command_line(command) or _as_str(command).strip()
    safe_command_spec = normalize_followup_command_spec(command_spec)
    safe_contract = _normalize_diagnosis_contract(diagnosis_contract)
    safe_purpose = _as_str(purpose).strip()
    safe_failure_code = _as_str(failure_code).strip().lower() or "command_recovery_needed"
    safe_failure_message = _as_str(failure_message).strip()
    safe_rounds = max(1, min(4, _as_int(max_rounds, 2)))
    attempts: List[Dict[str, Any]] = []

    if safe_failure_code in (
        {"missing_or_invalid_command_spec", "sql_preflight_failed", "missing_target_identity"}
        | _NON_RECOVERABLE_STRUCTURED_FAILURE_CODES
    ):
        if safe_failure_code in _NON_RECOVERABLE_STRUCTURED_FAILURE_CODES:
            for index in range(safe_rounds):
                attempts.append(
                    {
                        "round": index + 1,
                        "strategy": "strict_structured_gate",
                        "ok": False,
                        "failure_code": safe_failure_code,
                        "failure_message": safe_failure_message or safe_failure_code,
                    }
                )
            return {
                "status": "ask_user",
                "command": safe_command,
                "command_spec": safe_command_spec,
                "failure_code": safe_failure_code,
                "failure_message": safe_failure_message or safe_failure_code,
                "recovery_attempts": attempts,
            }
        if not safe_command_spec:
            for index in range(safe_rounds):
                attempts.append(
                    {
                        "round": index + 1,
                        "strategy": "normalize_structured_command",
                        "ok": False,
                        "failure_code": safe_failure_code,
                        "failure_message": safe_failure_message or "command_spec is missing",
                    }
                )
            return {
                "status": "ask_user",
                "command": safe_command,
                "command_spec": safe_command_spec,
                "failure_code": safe_failure_code,
                "failure_message": safe_failure_message or "command_spec is missing",
                "recovery_attempts": attempts,
            }

        repaired_spec = _repair_structured_clickhouse_query_spec(safe_command_spec)
        if repaired_spec != safe_command_spec:
            repaired_compile = _compile_structured_spec(repaired_spec, run_sql_preflight=True)
            attempts.append(
                {
                    "round": 1,
                    "strategy": "repair_structured_sql_spacing_then_compile",
                    "ok": bool(repaired_compile.get("ok")),
                    "failure_code": _as_str(repaired_compile.get("failure_code")),
                    "failure_message": _as_str(repaired_compile.get("failure_message")),
                }
            )
            if bool(repaired_compile.get("ok")):
                return {
                    "status": "recovered",
                    "command": _as_str(repaired_compile.get("command")).strip() or safe_command,
                    "command_spec": (
                        repaired_compile.get("command_spec")
                        if isinstance(repaired_compile.get("command_spec"), dict)
                        else repaired_spec
                    ),
                    "failure_code": safe_failure_code,
                    "failure_message": safe_failure_message,
                    "recovery_attempts": attempts,
                    "recovery_kind": "structured_sql_spacing_repaired",
                }
            safe_command_spec = repaired_spec

        compile_budget = max(1, safe_rounds - len(attempts))
        compile_rounds = [True]
        if compile_budget > 1:
            compile_rounds.append(False)
        compile_rounds.extend([False] * max(0, compile_budget - len(compile_rounds)))

        current_failure_code = safe_failure_code
        current_failure_message = safe_failure_message
        for index, run_sql_preflight in enumerate(
            compile_rounds[:compile_budget],
            start=(len(attempts) + 1),
        ):
            compile_result = _compile_structured_spec(safe_command_spec, run_sql_preflight=run_sql_preflight)
            attempts.append(
                {
                    "round": index,
                    "strategy": "compile_structured_spec" if run_sql_preflight else "compile_without_sql_preflight",
                    "ok": bool(compile_result.get("ok")),
                    "failure_code": _as_str(compile_result.get("failure_code")),
                    "failure_message": _as_str(compile_result.get("failure_message")),
                }
            )
            if bool(compile_result.get("ok")):
                return {
                    "status": "recovered",
                    "command": _as_str(compile_result.get("command")).strip() or safe_command,
                    "command_spec": (
                        compile_result.get("command_spec")
                        if isinstance(compile_result.get("command_spec"), dict)
                        else safe_command_spec
                    ),
                    "failure_code": current_failure_code,
                    "failure_message": current_failure_message,
                    "recovery_attempts": attempts,
                    "recovery_kind": "structured_command_recompiled",
                }
            current_failure_code = _as_str(compile_result.get("failure_code")).strip().lower() or current_failure_code
            current_failure_message = _as_str(compile_result.get("failure_message")).strip() or current_failure_message
        return {
            "status": "ask_user",
            "command": safe_command,
            "command_spec": safe_command_spec,
            "failure_code": current_failure_code,
            "failure_message": current_failure_message,
            "recovery_attempts": attempts,
        }

    if safe_failure_code == "diagnosis_contract_incomplete":
        missing_fields = _diagnosis_contract_missing_fields(safe_contract)
        current_contract = dict(safe_contract)
        for index in range(safe_rounds):
            strategy = "reuse_existing_diagnosis_context"
            if index == 1 and safe_purpose and not _as_str(current_contract.get("why_command_needed")).strip():
                current_contract["why_command_needed"] = safe_purpose
                strategy = "derive_why_command_needed_from_purpose"
            missing_fields = _diagnosis_contract_missing_fields(current_contract)
            attempts.append(
                {
                    "round": index + 1,
                    "strategy": strategy,
                    "ok": len(missing_fields) == 0,
                    "failure_code": "diagnosis_contract_incomplete" if missing_fields else "",
                    "failure_message": (
                        "missing diagnosis context"
                        if missing_fields
                        else ""
                    ),
                }
            )
            if not missing_fields:
                return {
                    "status": "recovered",
                    "command": safe_command,
                    "command_spec": safe_command_spec,
                    "diagnosis_contract": current_contract,
                    "failure_code": safe_failure_code,
                    "failure_message": safe_failure_message,
                    "recovery_attempts": attempts,
                    "recovery_kind": "diagnosis_contract_completed",
                }
        return {
            "status": "ask_user",
            "command": safe_command,
            "command_spec": safe_command_spec,
            "diagnosis_contract": current_contract,
            "missing_fields": missing_fields,
            "failure_code": "diagnosis_contract_incomplete",
            "failure_message": safe_failure_message or "missing diagnosis context",
            "recovery_attempts": attempts,
        }

    current_failure_message = safe_failure_message or "unable to resolve executable command"
    for index in range(safe_rounds):
        try:
            command_meta, _ = _resolve_followup_command_meta(safe_command)
        except Exception:
            command_meta = {}
        resolved_type = _as_str(command_meta.get("command_type")).strip().lower()
        attempts.append(
            {
                "round": index + 1,
                "strategy": "normalize_command_semantics",
                "ok": resolved_type not in {"", "unknown"},
                "failure_code": "unknown_semantics" if resolved_type in {"", "unknown"} else "",
                "failure_message": current_failure_message if resolved_type in {"", "unknown"} else "",
            }
        )
        if resolved_type not in {"", "unknown"}:
            return {
                "status": "recovered",
                "command": safe_command,
                "command_spec": safe_command_spec,
                "failure_code": safe_failure_code,
                "failure_message": safe_failure_message,
                "recovery_attempts": attempts,
                "recovery_kind": "command_semantics_resolved",
            }
    return {
        "status": "ask_user",
        "command": safe_command,
        "command_spec": safe_command_spec,
        "failure_code": "unknown_semantics",
        "failure_message": current_failure_message,
        "recovery_attempts": attempts,
    }
