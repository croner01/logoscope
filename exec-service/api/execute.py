"""
Exec API routes.
"""

import asyncio
import json
import os
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.audit_store import audit_retention_note, list_audits
from core.dispatch import dispatch_command
from core.executor_registry import list_executor_statuses, resolve_executor
from core.policy import as_bool, as_str, classify_command_with_auto_rewrite, evaluate_query_whitelist
from core.policy_opa_client import evaluate_policy_decision
from core.policy_decision_store import (
    bind_decision_to_run,
    get_policy_decision as get_policy_decision_record,
    list_policy_decisions,
    record_policy_decision,
)
from core.runtime_service import get_exec_runtime_service, is_terminal_status
from core.ticket_store import consume_ticket, issue_ticket, revoke_ticket
from core.target_registry_client import evaluate_target_registry_gate


router = APIRouter(prefix="/api/v1/exec", tags=["exec"])


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_enabled() -> bool:
    exec_default = as_bool(os.getenv("EXEC_WRITE_ENABLED"), True)
    return as_bool(os.getenv("AI_FOLLOWUP_COMMAND_WRITE_ENABLED"), exec_default)


def command_exec_enabled() -> bool:
    return as_bool(os.getenv("AI_FOLLOWUP_COMMAND_EXEC_ENABLED"), True)


def test_permissive_enabled() -> bool:
    return as_bool(os.getenv("EXEC_COMMAND_TEST_PERMISSIVE"), False)


def _dispatch_unavailable(dispatch_preview: Dict[str, Any]) -> bool:
    safe_preview = dispatch_preview if isinstance(dispatch_preview, dict) else {}
    return (
        as_str(safe_preview.get("dispatch_backend")).strip().lower() != "template_executor"
        or not bool(safe_preview.get("dispatch_ready"))
        or bool(safe_preview.get("dispatch_degraded"))
    )


def _resolve_effective_command(command: str, command_spec: Dict[str, Any] | None = None) -> str:
    raw_command = as_str(command).strip()
    if raw_command:
        return raw_command
    safe_spec = command_spec if isinstance(command_spec, dict) else {}
    args = safe_spec.get("args") if isinstance(safe_spec.get("args"), dict) else {}
    argv = args.get("command_argv") or args.get("argv") or safe_spec.get("command_argv") or safe_spec.get("argv")
    if isinstance(argv, list):
        safe_argv = [as_str(item).strip() for item in argv if as_str(item).strip()]
        if safe_argv:
            return shlex.join(safe_argv)
    spec_command = as_str(args.get("command") or safe_spec.get("command")).strip()
    return spec_command


def _decision_result_from_response(response: Dict[str, Any]) -> str:
    safe_response = response if isinstance(response, dict) else {}
    status = as_str(safe_response.get("status")).strip().lower()
    approval_policy = as_str(safe_response.get("approval_policy")).strip().lower()
    if status == "ok":
        return "allow"
    if status == "confirmation_required":
        return "confirm"
    if status == "elevation_required":
        return "elevate"
    if approval_policy == "manual_required":
        return "manual_required"
    return "deny"


def _apply_effective_policy_to_response(
    *,
    response: Dict[str, Any],
    effective_result: str,
    effective_reason: str,
    policy_engine: str,
) -> Dict[str, Any]:
    safe_response = response if isinstance(response, dict) else {}
    normalized_result = as_str(effective_result).strip().lower()
    if normalized_result == "manual_required":
        requires_elevation = bool(safe_response.get("requires_write_permission")) or bool(safe_response.get("requires_elevation"))
        safe_response["status"] = "elevation_required" if requires_elevation else "confirmation_required"
        safe_response["approval_policy"] = "manual_required"
        safe_response["requires_confirmation"] = True
        safe_response["requires_elevation"] = requires_elevation
        safe_response["message"] = (
            f"policy requires manual approval by {as_str(policy_engine, 'policy-engine')}: "
            f"{as_str(effective_reason, 'manual approval required')}"
        )
    elif normalized_result == "deny":
        safe_response["status"] = "permission_required"
        safe_response["approval_policy"] = "deny"
        safe_response["requires_confirmation"] = False
        safe_response["requires_elevation"] = False
        safe_response["message"] = (
            f"policy denied by {as_str(policy_engine, 'policy-engine')}: "
            f"{as_str(effective_reason, 'denied')}"
        )
    return safe_response


def _looks_like_dispatch_unavailable_message(message: str) -> bool:
    return as_str(message).strip().lower().startswith(
        "controlled executor unavailable for current executor profile:"
    )


def _rehydrate_response_after_dispatch_recovery(
    *,
    response: Dict[str, Any],
    command_meta: Dict[str, Any],
    whitelist_match: bool,
    whitelist_reason: str,
) -> Dict[str, Any]:
    """
    Target-registry metadata may override executor profile during finalize.
    If dispatch is recovered after this override, recover precheck status from
    stale early-deny branch and continue normal policy evaluation.
    """
    safe_response = response if isinstance(response, dict) else {}
    safe_meta = command_meta if isinstance(command_meta, dict) else {}
    status = as_str(safe_response.get("status")).strip().lower()
    approval_policy = as_str(safe_response.get("approval_policy")).strip().lower()
    if status != "permission_required" or approval_policy != "deny":
        return safe_response
    if not _looks_like_dispatch_unavailable_message(as_str(safe_response.get("message"))):
        return safe_response
    if _dispatch_unavailable(safe_response):
        return safe_response

    command_type = as_str(
        safe_meta.get("command_type"),
        as_str(safe_response.get("command_type"), "unknown"),
    ).strip().lower()
    requires_write = bool(
        safe_meta.get("requires_write_permission")
        or safe_response.get("requires_write_permission")
    )

    if command_type == "query" and not bool(whitelist_match):
        safe_response["status"] = "confirmation_required"
        safe_response["approval_policy"] = "confirmation_required"
        safe_response["requires_confirmation"] = True
        safe_response["requires_elevation"] = False
        safe_response["message"] = (
            "命令不在免审批白名单模板内，需人工确认后执行。"
            f" 原因: {whitelist_reason or '未匹配模板约束'}"
        )
        return safe_response

    if requires_write:
        safe_response["requires_write_permission"] = True
        if not write_enabled() and not test_permissive_enabled():
            safe_response["status"] = "permission_required"
            safe_response["approval_policy"] = "deny"
            safe_response["requires_confirmation"] = False
            safe_response["requires_elevation"] = False
            safe_response["message"] = "write command is disabled by policy"
            return safe_response
        safe_response["status"] = "elevation_required"
        safe_response["approval_policy"] = "elevation_required"
        safe_response["requires_confirmation"] = True
        safe_response["requires_elevation"] = True
        safe_response["message"] = "write command requires elevation and confirmation"
        return safe_response

    safe_response["status"] = "ok"
    safe_response["approval_policy"] = (
        as_str(safe_meta.get("approval_policy"), "auto_execute").strip() or "auto_execute"
    )
    safe_response["requires_confirmation"] = False
    safe_response["requires_elevation"] = False
    safe_response["message"] = as_str(
        safe_meta.get("reason"),
        as_str(safe_response.get("message")),
    )
    return safe_response


def _is_unknown_target(target_identity: str, target_kind: str) -> bool:
    safe_identity = as_str(target_identity).strip().lower()
    safe_kind = as_str(target_kind).strip().lower()
    if not safe_identity:
        return True
    if safe_identity in {
        "unknown",
        "runtime:unknown",
        "namespace:unknown",
        "database:unknown",
        "project:unknown",
        "host:unknown",
        "http:unknown",
    }:
        return True
    if ":unknown" in safe_identity:
        return True
    if safe_kind in {"unknown", "runtime_node"} and safe_identity in {"runtime:local", "local"}:
        # runtime_node without explicit target attribution is treated as unknown scope.
        return True
    return False


def _normalize_capability_tokens(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    seen = set()
    for item in values:
        token = as_str(item).strip().lower()
        if not token:
            continue
        token = re.sub(r"[^a-z0-9:_-]+", "_", token).strip("_")
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _derive_required_capabilities(command_meta: Dict[str, Any], response: Dict[str, Any]) -> List[str]:
    safe_meta = command_meta if isinstance(command_meta, dict) else {}
    safe_response = response if isinstance(response, dict) else {}

    explicit = _normalize_capability_tokens(safe_meta.get("required_capabilities"))
    if explicit:
        return explicit
    explicit = _normalize_capability_tokens(safe_response.get("required_capabilities"))
    if explicit:
        return explicit

    family = as_str(
        safe_meta.get("command_family"),
        as_str(safe_response.get("command_family"), "unknown"),
    ).strip().lower()
    command_type = as_str(
        safe_meta.get("command_type"),
        as_str(safe_response.get("command_type"), "unknown"),
    ).strip().lower()
    requires_write = bool(safe_meta.get("requires_write_permission")) or bool(safe_response.get("requires_write_permission"))
    operation = "write" if requires_write or command_type in {"repair", "mutation", "write"} else "read"

    default_mapping = {
        "kubernetes:read": ["read_logs"],
        "kubernetes:write": ["restart_workload"],
        "clickhouse:read": ["run_query"],
        "clickhouse:write": ["clickhouse_mutation"],
        "postgres:read": ["run_query"],
        "postgres:write": ["postgres_mutation"],
        "mysql:read": ["run_query"],
        "mysql:write": ["mysql_mutation"],
        "openstack:read": ["read_cloud"],
        "openstack:write": ["openstack_mutation"],
        "http:read": ["http_read"],
        "http:write": ["http_mutation"],
        "linux:read": ["read_host_state"],
        "linux:write": ["host_mutation"],
        "shell:read": ["read_host_state"],
        "shell:write": ["host_mutation"],
    }
    override_raw = as_str(os.getenv("EXEC_TARGET_REQUIRED_CAPS_JSON")).strip()
    if override_raw:
        try:
            override_payload = json.loads(override_raw)
        except Exception:
            override_payload = {}
        if isinstance(override_payload, dict):
            for key, value in override_payload.items():
                normalized_key = as_str(key).strip().lower()
                normalized_caps = _normalize_capability_tokens(value)
                if normalized_key and normalized_caps:
                    default_mapping[normalized_key] = normalized_caps

    key = f"{family}:{operation}"
    capabilities = default_mapping.get(key)
    if not capabilities:
        capabilities = default_mapping.get(f"default:{operation}")
    if not capabilities:
        safe_family = re.sub(r"[^a-z0-9]+", "_", family).strip("_") or "generic"
        suffix = "mutation" if operation == "write" else "read"
        capabilities = [f"{safe_family}_{suffix}"]
    return _normalize_capability_tokens(capabilities)


def _attach_confirmation_ticket_if_needed(
    *,
    request: "PrecheckRequest",
    response: Dict[str, Any],
) -> Dict[str, Any]:
    safe_response = response if isinstance(response, dict) else {}
    status = as_str(safe_response.get("status")).strip().lower()
    if status not in {"confirmation_required", "elevation_required"}:
        return safe_response
    if as_str(safe_response.get("confirmation_ticket")).strip():
        return safe_response
    ticket = issue_ticket(
        session_id=request.session_id,
        message_id=request.message_id,
        action_id=request.action_id,
        command=as_str(safe_response.get("command")),
        requires_elevation=bool(safe_response.get("requires_elevation")) or status == "elevation_required",
        decision_id=as_str(safe_response.get("decision_id")),
    )
    safe_response["confirmation_ticket"] = ticket["ticket_id"]
    safe_response["ticket_expires_at"] = int(ticket["expires_at"])
    return safe_response


def _finalize_precheck_response(
    *,
    request: "PrecheckRequest",
    response: Dict[str, Any],
    command_meta: Dict[str, Any],
    dispatch_preview: Dict[str, Any],
    whitelist_match: bool,
    whitelist_reason: str,
) -> Dict[str, Any]:
    safe_response = response if isinstance(response, dict) else {}
    safe_meta = command_meta if isinstance(command_meta, dict) else {}
    safe_dispatch = dispatch_preview if isinstance(dispatch_preview, dict) else {}
    target_kind = as_str(safe_response.get("target_kind"))
    target_identity = as_str(safe_response.get("target_identity"))
    static_unknown_target = _is_unknown_target(
        target_identity,
        target_kind,
    )
    required_capabilities = _derive_required_capabilities(safe_meta, safe_response)
    target_resolution = evaluate_target_registry_gate(
        target_id=as_str(safe_response.get("target_id"), as_str(safe_meta.get("target_id"))),
        target_kind=target_kind,
        target_identity=target_identity,
        required_capabilities=required_capabilities,
        action_id=as_str(request.action_id),
        reason="exec precheck target capability gate",
    )
    registry_registered = bool(target_resolution.get("registered"))
    unknown_target = bool(static_unknown_target or not registry_registered)
    safe_response["target_registry"] = {
        "enabled": bool(target_resolution.get("enabled")),
        "mode": as_str(target_resolution.get("mode")),
        "applied": bool(target_resolution.get("applied")),
        "result": as_str(target_resolution.get("result")),
        "reason": as_str(target_resolution.get("reason")),
        "target_id": as_str(target_resolution.get("target_id")),
        "registered": registry_registered,
        "status": as_str(target_resolution.get("status")),
        "required_capabilities": list(target_resolution.get("required_capabilities") or []),
        "missing_capabilities": list(target_resolution.get("missing_capabilities") or []),
        "matched_capabilities": list(target_resolution.get("matched_capabilities") or []),
        "ambiguous_targets": list(target_resolution.get("ambiguous_targets") or []),
        "lookup_error": as_str(target_resolution.get("lookup_error")),
        "resolve_error": as_str(target_resolution.get("resolve_error")),
        "metadata_contract": (
            dict(target_resolution.get("metadata_contract"))
            if isinstance(target_resolution.get("metadata_contract"), dict)
            else {}
        ),
        "resolved_target_context": (
            dict(target_resolution.get("resolved_target_context"))
            if isinstance(target_resolution.get("resolved_target_context"), dict)
            else {}
        ),
    }
    resolved_target_context = (
        dict(target_resolution.get("resolved_target_context"))
        if isinstance(target_resolution.get("resolved_target_context"), dict)
        else {}
    )
    if as_str(safe_response.get("command")).strip():
        dispatch_with_resolution = resolve_executor(
            command=as_str(safe_response.get("command") or request.command),
            executor_type=as_str(safe_response.get("executor_type"), "local_process"),
            executor_profile=as_str(safe_response.get("executor_profile"), "local-default"),
            target_kind=target_kind,
            target_identity=target_identity,
            resolved_target_context=resolved_target_context,
        )
        safe_response["effective_executor_type"] = as_str(
            dispatch_with_resolution.get("effective_executor_type"),
            as_str(safe_response.get("effective_executor_type"), "local_process"),
        )
        safe_response["effective_executor_profile"] = as_str(
            dispatch_with_resolution.get("effective_executor_profile"),
            as_str(safe_response.get("effective_executor_profile")),
        )
        safe_response["dispatch_backend"] = as_str(
            dispatch_with_resolution.get("dispatch_backend"),
            as_str(safe_response.get("dispatch_backend"), "template_unavailable"),
        )
        safe_response["dispatch_mode"] = as_str(
            dispatch_with_resolution.get("dispatch_mode"),
            as_str(safe_response.get("dispatch_mode"), "blocked"),
        )
        safe_response["dispatch_reason"] = as_str(
            dispatch_with_resolution.get("dispatch_reason"),
            as_str(safe_response.get("dispatch_reason")),
        )
        safe_response["dispatch_template_env"] = as_str(
            dispatch_with_resolution.get("dispatch_template_env"),
            as_str(safe_response.get("dispatch_template_env")),
        )
        safe_response["dispatch_requires_template"] = bool(dispatch_with_resolution.get("dispatch_requires_template"))
        safe_response["dispatch_ready"] = bool(dispatch_with_resolution.get("dispatch_ready"))
        safe_response["dispatch_degraded"] = bool(dispatch_with_resolution.get("dispatch_degraded"))
        safe_response["target_node_name"] = as_str(dispatch_with_resolution.get("target_node_name"))
        safe_response["target_namespace"] = as_str(dispatch_with_resolution.get("target_namespace"))
        safe_response["target_cluster_id"] = as_str(dispatch_with_resolution.get("target_cluster_id"))
        safe_response = _rehydrate_response_after_dispatch_recovery(
            response=safe_response,
            command_meta=safe_meta,
            whitelist_match=whitelist_match,
            whitelist_reason=whitelist_reason,
        )
    if as_str(target_resolution.get("target_id")).strip():
        safe_response["target_id"] = as_str(target_resolution.get("target_id"))
    input_payload = {
        "session_id": as_str(request.session_id),
        "message_id": as_str(request.message_id),
        "action_id": as_str(request.action_id),
        "command": as_str(safe_response.get("command") or request.command),
        "purpose": as_str(request.purpose),
        "classification": {
            "command_type": as_str(safe_meta.get("command_type"), as_str(safe_response.get("command_type"), "unknown")),
            "risk_level": as_str(safe_meta.get("risk_level"), as_str(safe_response.get("risk_level"), "high")),
            "command_family": as_str(safe_meta.get("command_family"), as_str(safe_response.get("command_family"), "unknown")),
            "supported": bool(safe_meta.get("supported")),
            "requires_write_permission": bool(safe_meta.get("requires_write_permission")),
            "approval_policy": as_str(safe_meta.get("approval_policy"), as_str(safe_response.get("approval_policy"), "deny")),
        },
        "dispatch_preview": {
            "dispatch_backend": as_str(
                safe_response.get("dispatch_backend"),
                as_str(safe_dispatch.get("dispatch_backend")),
            ),
            "dispatch_mode": as_str(
                safe_response.get("dispatch_mode"),
                as_str(safe_dispatch.get("dispatch_mode")),
            ),
            "dispatch_reason": as_str(
                safe_response.get("dispatch_reason"),
                as_str(safe_dispatch.get("dispatch_reason")),
            ),
            "dispatch_ready": bool(
                safe_response.get("dispatch_ready")
                if "dispatch_ready" in safe_response
                else safe_dispatch.get("dispatch_ready")
            ),
            "dispatch_degraded": bool(
                safe_response.get("dispatch_degraded")
                if "dispatch_degraded" in safe_response
                else safe_dispatch.get("dispatch_degraded")
            ),
            "dispatch_requires_template": bool(
                safe_response.get("dispatch_requires_template")
                if "dispatch_requires_template" in safe_response
                else safe_dispatch.get("dispatch_requires_template")
            ),
            "dispatch_template_env": as_str(
                safe_response.get("dispatch_template_env"),
                as_str(safe_dispatch.get("dispatch_template_env")),
            ),
        },
        "whitelist": {
            "match": bool(whitelist_match),
            "reason": as_str(whitelist_reason),
        },
        "target": {
            "kind": target_kind,
            "identity": target_identity,
            "unknown_target": bool(unknown_target),
        },
        "target_registry": {
            "enabled": bool(target_resolution.get("enabled")),
            "mode": as_str(target_resolution.get("mode")),
            "applied": bool(target_resolution.get("applied")),
            "target_id": as_str(target_resolution.get("target_id")),
            "registered": registry_registered,
            "status": as_str(target_resolution.get("status")),
            "result": as_str(target_resolution.get("result")),
            "reason": as_str(target_resolution.get("reason")),
            "required_capabilities": list(target_resolution.get("required_capabilities") or []),
            "missing_capabilities": list(target_resolution.get("missing_capabilities") or []),
            "matched_capabilities": list(target_resolution.get("matched_capabilities") or []),
            "ambiguous_targets": list(target_resolution.get("ambiguous_targets") or []),
            "lookup_error": as_str(target_resolution.get("lookup_error")),
            "resolve_error": as_str(target_resolution.get("resolve_error")),
            "metadata_contract": (
                dict(target_resolution.get("metadata_contract"))
                if isinstance(target_resolution.get("metadata_contract"), dict)
                else {}
            ),
            "resolved_target_context": (
                dict(target_resolution.get("resolved_target_context"))
                if isinstance(target_resolution.get("resolved_target_context"), dict)
                else {}
            ),
        },
        "runtime_switches": {
            "command_exec_enabled": command_exec_enabled(),
            "write_enabled": write_enabled(),
            "test_permissive_enabled": test_permissive_enabled(),
        },
    }
    safe_response["resolved_target_context"] = resolved_target_context
    local_result = _decision_result_from_response(safe_response)
    local_reason = as_str(safe_response.get("message"))
    if bool(target_resolution.get("applied")) and as_str(target_resolution.get("result")).strip().lower() == "manual_required":
        local_result = "manual_required"
        registry_reason = as_str(target_resolution.get("reason"), "target registry requires manual approval")
        local_reason = f"{local_reason}; {registry_reason}" if local_reason else registry_reason
    if unknown_target and (local_result == "allow" or _looks_like_dispatch_unavailable_message(local_reason)):
        local_result = "manual_required"
        local_reason = (
            f"{local_reason}; unknown target requires manual approval"
            if local_reason
            else "unknown target requires manual approval"
        )
    policy_outcome = evaluate_policy_decision(
        local_result=local_result,
        local_reason=local_reason,
        input_payload=input_payload,
    )
    if (
        bool(target_resolution.get("applied"))
        and as_str(target_resolution.get("result")).strip().lower() == "manual_required"
        and as_str(policy_outcome.get("result")).strip().lower() == "allow"
    ):
        policy_outcome = {
            **policy_outcome,
            "result": "manual_required",
            "reason": (
                f"{as_str(policy_outcome.get('reason'))}; target registry requires manual approval"
                if as_str(policy_outcome.get("reason"))
                else "target registry requires manual approval"
            ),
        }
    # Defense-in-depth: unknown target must never auto-execute even if policy chain returns allow.
    if unknown_target and as_str(policy_outcome.get("result")).strip().lower() == "allow":
        policy_outcome = {
            **policy_outcome,
            "result": "manual_required",
            "reason": (
                f"{as_str(policy_outcome.get('reason'))}; unknown target requires manual approval"
                if as_str(policy_outcome.get("reason"))
                else "unknown target requires manual approval"
            ),
        }
    safe_response = _apply_effective_policy_to_response(
        response=safe_response,
        effective_result=as_str(policy_outcome.get("result"), local_result),
        effective_reason=as_str(policy_outcome.get("reason"), local_reason),
        policy_engine=as_str(policy_outcome.get("engine"), "python-inline"),
    )
    decision = record_policy_decision(
        session_id=as_str(request.session_id),
        message_id=as_str(request.message_id),
        action_id=as_str(request.action_id),
        run_id="",
        command_run_id="",
        command=as_str(safe_response.get("command")),
        purpose=as_str(request.purpose),
        command_type=as_str(safe_response.get("command_type"), "unknown"),
        risk_level=as_str(safe_response.get("risk_level"), "high"),
        command_family=as_str(safe_response.get("command_family"), "unknown"),
        approval_policy=as_str(safe_response.get("approval_policy"), "deny"),
        target_kind=as_str(safe_response.get("target_kind"), "runtime_node"),
        target_identity=as_str(safe_response.get("target_identity"), "runtime:local"),
        executor_type=as_str(safe_response.get("executor_type"), "local_process"),
        executor_profile=as_str(safe_response.get("executor_profile"), "local-default"),
        dispatch_backend=as_str(safe_response.get("dispatch_backend")),
        dispatch_mode=as_str(safe_response.get("dispatch_mode")),
        dispatch_reason=as_str(safe_response.get("dispatch_reason")),
        dispatch_ready=bool(safe_response.get("dispatch_ready")),
        dispatch_degraded=bool(safe_response.get("dispatch_degraded")),
        whitelist_match=bool(whitelist_match),
        whitelist_reason=as_str(whitelist_reason),
        status=as_str(safe_response.get("status"), "permission_required"),
        result=as_str(policy_outcome.get("result"), _decision_result_from_response(safe_response)),
        reason=as_str(safe_response.get("message")),
        policy_engine=as_str(policy_outcome.get("engine"), "python-inline"),
        policy_package=as_str(policy_outcome.get("package"), as_str(os.getenv("EXEC_POLICY_PACKAGE"), "runtime.command.v1")),
        input_payload=input_payload,
        policy_mode=as_str(policy_outcome.get("mode"), "local"),
        decision_source=as_str(policy_outcome.get("source"), "local"),
        local_result=as_str(policy_outcome.get("local_result"), local_result),
        local_reason=as_str(policy_outcome.get("local_reason"), local_reason),
        opa_available=bool(policy_outcome.get("opa_available")),
        opa_result=as_str(policy_outcome.get("opa_result")),
        opa_reason=as_str(policy_outcome.get("opa_reason")),
        opa_package=as_str(policy_outcome.get("opa_package")),
    )
    safe_response["decision_id"] = as_str(decision.get("decision_id"))
    safe_response["policy_decision"] = {
        "decision_id": as_str(decision.get("decision_id")),
        "result": as_str(decision.get("result")),
        "engine": as_str(decision.get("engine")),
        "package": as_str(decision.get("package")),
        "reason": as_str(decision.get("reason")),
        "input_hash": as_str(decision.get("input_hash")),
        "mode": as_str(decision.get("mode")),
        "source": as_str(decision.get("source")),
        "opa_available": bool(decision.get("opa_available")),
        "opa_result": as_str(decision.get("opa_result")),
        "opa_reason": as_str(decision.get("opa_reason")),
    }
    return _attach_confirmation_ticket_if_needed(
        request=request,
        response=safe_response,
    )


def _format_sse_event(event: str, payload: Dict[str, Any]) -> str:
    safe_event = as_str(event, "message").replace("\n", "").strip() or "message"
    data = json.dumps(payload if isinstance(payload, dict) else {}, ensure_ascii=False)
    return f"event: {safe_event}\ndata: {data}\n\n"


def _build_clickhouse_runtime_preflight_command(command: str) -> str:
    text = as_str(command).strip()
    if not text:
        return ""
    lowered = text.lower()
    if "clickhouse-client" not in lowered and " clickhouse " not in f" {lowered} ":
        return ""

    def _replace_quoted(match: re.Match[str]) -> str:
        prefix = as_str(match.group("prefix"))
        quote = as_str(match.group("quote"))
        body = as_str(match.group("body")).strip()
        if not body or body.lower().startswith("explain syntax "):
            return match.group(0)
        return f"{prefix}{quote}EXPLAIN SYNTAX {body}{quote}"

    def _replace_unquoted(match: re.Match[str]) -> str:
        prefix = as_str(match.group("prefix"))
        body = as_str(match.group("body")).strip()
        if not body or body.lower().startswith("explain syntax "):
            return match.group(0)
        return f"{prefix}EXPLAIN SYNTAX {body}"

    patterns = [
        (
            r"(?is)(?P<prefix>(?:--query|-q)\s+)(?P<quote>['\"])(?P<body>.*?)(?P=quote)",
            _replace_quoted,
        ),
        (
            r"(?is)(?P<prefix>(?:--query|-q)=)(?P<quote>['\"])(?P<body>.*?)(?P=quote)",
            _replace_quoted,
        ),
        (
            r"(?i)(?P<prefix>(?:--query|-q)\s+)(?P<body>[^\s\"'][^\s]*)",
            _replace_unquoted,
        ),
        (
            r"(?i)(?P<prefix>(?:--query|-q)=)(?P<body>[^\s\"'][^\s]*)",
            _replace_unquoted,
        ),
    ]
    for pattern, repl in patterns:
        updated, count = re.subn(pattern, repl, text, count=1)
        if count > 0:
            return updated
    return ""


def _is_runtime_preflight_non_blocking_failure(preflight_payload: Dict[str, Any]) -> bool:
    safe_payload = preflight_payload if isinstance(preflight_payload, dict) else {}
    dispatch = safe_payload.get("dispatch") if isinstance(safe_payload.get("dispatch"), dict) else {}
    if dispatch and _dispatch_unavailable(dispatch):
        return True
    merged = "\n".join(
        [
            as_str(safe_payload.get("message")),
            as_str(safe_payload.get("stderr")),
            as_str(safe_payload.get("stdout")),
        ]
    ).lower()
    if not merged:
        return False
    transient_markers = (
        "curl: (3)",
        "shell syntax is disabled by policy",
        "curl: (6)",
        "curl: (7)",
        "curl: (28)",
        "url using bad/illegal format or missing url",
        "could not resolve host",
        "temporary failure in name resolution",
        "name or service not known",
        "failed to connect to",
        "connection refused",
        "connection timed out",
        "operation timed out",
        "no route to host",
    )
    return any(marker in merged for marker in transient_markers)


async def _run_runtime_preflight(
    *,
    command: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
    timeout_seconds: int = 15,
) -> Dict[str, Any]:
    preflight_command = _build_clickhouse_runtime_preflight_command(command)
    if not preflight_command:
        return {"applicable": False}

    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []

    async def _on_output(stream_name: str, text: str) -> None:
        if as_str(stream_name).strip().lower() == "stderr":
            stderr_chunks.append(as_str(text))
        else:
            stdout_chunks.append(as_str(text))

    result = await dispatch_command(
        command=preflight_command,
        executor_type=executor_type,
        executor_profile=executor_profile,
        target_kind=target_kind,
        target_identity=target_identity,
        timeout_seconds=max(3, min(30, int(timeout_seconds or 15))),
        on_output=_on_output,
    )
    stdout_text = "".join(stdout_chunks).strip()
    stderr_text = "".join(stderr_chunks).strip()
    exit_code = int(result.get("exit_code") or 0)
    timed_out = bool(result.get("timed_out"))
    dispatch = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
    if exit_code == 0 and not timed_out:
        return {
            "applicable": True,
            "ok": True,
            "command": preflight_command,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "dispatch": dispatch,
        }

    message = stderr_text or stdout_text or as_str(dispatch.get("dispatch_reason")) or "runtime preflight failed"
    return {
        "applicable": True,
        "ok": False,
        "command": preflight_command,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "dispatch": dispatch,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "message": message,
    }


class PrecheckRequest(BaseModel):
    session_id: str = ""
    message_id: str = ""
    action_id: str = ""
    step_id: str = ""
    command: str
    command_spec: Dict[str, Any] = Field(default_factory=dict)
    purpose: str = ""
    target_kind: str = ""
    target_identity: str = ""


class TicketConfirmRequest(BaseModel):
    ticket_id: str
    session_id: str = ""
    message_id: str = ""
    action_id: str = ""
    command: str
    requires_elevation: bool = False


class ExecuteRequest(BaseModel):
    session_id: str = ""
    message_id: str = ""
    action_id: str = ""
    step_id: str = ""
    command: str
    command_spec: Dict[str, Any] = Field(default_factory=dict)
    purpose: str
    confirmed: bool = False
    elevated: bool = False
    confirmation_ticket: str = ""
    approval_token: str = ""
    client_deadline_ms: int = 0
    timeout_seconds: int = 20
    target_kind: str = ""
    target_identity: str = ""


class CommandRunCreateRequest(ExecuteRequest):
    """Command run creation request."""


@router.post("/precheck")
async def precheck_command(request: PrecheckRequest) -> Dict[str, Any]:
    effective_command = _resolve_effective_command(as_str(request.command), request.command_spec)
    if not effective_command:
        raise HTTPException(status_code=400, detail="command or command_spec is required")
    empty_meta: Dict[str, Any] = {}
    empty_dispatch: Dict[str, Any] = {}
    if not command_exec_enabled():
        response = {
            "status": "permission_required",
            "session_id": as_str(request.session_id),
            "message_id": as_str(request.message_id),
            "action_id": as_str(request.action_id),
            "command": effective_command,
            "command_spec": request.command_spec if isinstance(request.command_spec, dict) else {},
            "purpose": as_str(request.purpose),
            "command_type": "unknown",
            "risk_level": "high",
            "command_family": "unknown",
            "approval_policy": "deny",
            "requires_confirmation": False,
            "requires_write_permission": False,
            "requires_elevation": False,
            "message": "命令执行能力已关闭，请联系管理员开启 AI_FOLLOWUP_COMMAND_EXEC_ENABLED。",
            "whitelist_match": False,
            "whitelist_reason": "command execution disabled by policy switch",
        }
        return _finalize_precheck_response(
            request=request,
            response=response,
            command_meta=empty_meta,
            dispatch_preview=empty_dispatch,
            whitelist_match=False,
            whitelist_reason=as_str(response.get("whitelist_reason")),
        )

    command_meta = classify_command_with_auto_rewrite(effective_command)
    requested_target_kind = as_str(request.target_kind).strip()
    requested_target_identity = as_str(request.target_identity).strip()
    if requested_target_kind:
        command_meta["target_kind"] = requested_target_kind
    if requested_target_identity:
        command_meta["target_identity"] = requested_target_identity
    whitelist_eval = evaluate_query_whitelist(
        as_str(command_meta.get("command")),
        command_meta,
    )
    whitelist_match = bool(whitelist_eval.get("whitelisted"))
    whitelist_reason = as_str(whitelist_eval.get("reason"))
    dispatch_preview = resolve_executor(
        command=as_str(command_meta.get("command")),
        executor_type=as_str(command_meta.get("executor_type"), "local_process"),
        executor_profile=as_str(command_meta.get("executor_profile"), "local-default"),
        target_kind=as_str(command_meta.get("target_kind"), "runtime_node"),
        target_identity=as_str(command_meta.get("target_identity"), "runtime:local"),
    )
    response = {
        "status": "ok",
        "session_id": as_str(request.session_id),
        "message_id": as_str(request.message_id),
        "action_id": as_str(request.action_id),
        "step_id": as_str(request.step_id),
        "command": as_str(command_meta.get("command")),
        "command_spec": request.command_spec if isinstance(request.command_spec, dict) else {},
        "purpose": as_str(request.purpose),
        "command_type": as_str(command_meta.get("command_type"), "unknown"),
        "risk_level": as_str(command_meta.get("risk_level"), "high"),
        "command_family": as_str(command_meta.get("command_family"), "unknown"),
        "approval_policy": as_str(command_meta.get("approval_policy"), "deny"),
        "executor_type": as_str(command_meta.get("executor_type"), "local_process"),
        "executor_profile": as_str(command_meta.get("executor_profile"), "local-default"),
        "target_kind": as_str(command_meta.get("target_kind"), "runtime_node"),
        "target_identity": as_str(command_meta.get("target_identity"), "runtime:local"),
        "effective_executor_type": as_str(dispatch_preview.get("effective_executor_type"), "local_process"),
        "effective_executor_profile": as_str(dispatch_preview.get("effective_executor_profile")),
        "dispatch_backend": as_str(dispatch_preview.get("dispatch_backend"), "template_unavailable"),
        "dispatch_mode": as_str(dispatch_preview.get("dispatch_mode"), "blocked"),
        "dispatch_reason": as_str(dispatch_preview.get("dispatch_reason")),
        "dispatch_template_env": as_str(dispatch_preview.get("dispatch_template_env")),
        "dispatch_requires_template": bool(dispatch_preview.get("dispatch_requires_template")),
        "dispatch_ready": bool(dispatch_preview.get("dispatch_ready")),
        "dispatch_degraded": bool(dispatch_preview.get("dispatch_degraded")),
        "requires_confirmation": False,
        "requires_write_permission": bool(command_meta.get("requires_write_permission")),
        "requires_elevation": bool(command_meta.get("requires_write_permission")),
        "message": as_str(command_meta.get("reason")),
        "rewrite_applied": bool(command_meta.get("rewrite_applied")),
        "original_command": as_str(command_meta.get("original_command")),
        "rewrite_reason": as_str(command_meta.get("rewrite_reason")),
        "rewrite_attempts": command_meta.get("rewrite_attempts") if isinstance(command_meta.get("rewrite_attempts"), list) else [],
        "whitelist_match": whitelist_match,
        "whitelist_reason": whitelist_reason,
    }
    if not bool(command_meta.get("supported")):
        response["status"] = "permission_required"
        response["requires_elevation"] = False
        return _finalize_precheck_response(
            request=request,
            response=response,
            command_meta=command_meta,
            dispatch_preview=dispatch_preview,
            whitelist_match=whitelist_match,
            whitelist_reason=whitelist_reason,
        )

    if _dispatch_unavailable(dispatch_preview):
        response["status"] = "permission_required"
        response["approval_policy"] = "deny"
        response["requires_confirmation"] = False
        response["requires_elevation"] = False
        response["message"] = (
            "controlled executor unavailable for current executor profile: "
            f"{as_str(dispatch_preview.get('dispatch_reason')) or 'unknown reason'}"
        )
        return _finalize_precheck_response(
            request=request,
            response=response,
            command_meta=command_meta,
            dispatch_preview=dispatch_preview,
            whitelist_match=whitelist_match,
            whitelist_reason=whitelist_reason,
        )

    runtime_preflight = await _run_runtime_preflight(
        command=as_str(command_meta.get("command")),
        executor_type=as_str(command_meta.get("executor_type"), "local_process"),
        executor_profile=as_str(command_meta.get("executor_profile"), "local-default"),
        target_kind=as_str(command_meta.get("target_kind"), "runtime_node"),
        target_identity=as_str(command_meta.get("target_identity"), "runtime:local"),
    )
    if bool(runtime_preflight.get("applicable")):
        response["runtime_preflight"] = {
            "applicable": True,
            "ok": bool(runtime_preflight.get("ok")),
            "command": as_str(runtime_preflight.get("command")),
            "message": as_str(runtime_preflight.get("message")),
            "stdout": as_str(runtime_preflight.get("stdout")),
            "stderr": as_str(runtime_preflight.get("stderr")),
            "timed_out": bool(runtime_preflight.get("timed_out")),
            "exit_code": int(runtime_preflight.get("exit_code") or 0),
        }
    if bool(runtime_preflight.get("applicable")) and not bool(runtime_preflight.get("ok")):
        if _is_runtime_preflight_non_blocking_failure(runtime_preflight):
            runtime_preflight_payload = (
                response.get("runtime_preflight")
                if isinstance(response.get("runtime_preflight"), dict)
                else {}
            )
            runtime_preflight_payload["non_blocking"] = True
            runtime_preflight_payload["non_blocking_reason"] = "transient_execution_unavailable"
            response["runtime_preflight"] = runtime_preflight_payload
        else:
            response["status"] = "permission_required"
            response["approval_policy"] = "deny"
            response["requires_confirmation"] = False
            response["requires_elevation"] = False
            response["message"] = (
                "runtime preflight failed in execution domain: "
                f"{as_str(runtime_preflight.get('message')) or 'unknown reason'}"
            )
            return _finalize_precheck_response(
                request=request,
                response=response,
                command_meta=command_meta,
                dispatch_preview=dispatch_preview,
                whitelist_match=whitelist_match,
                whitelist_reason=whitelist_reason,
            )

    if as_str(command_meta.get("command_type")).lower() == "query" and not whitelist_match:
        response["status"] = "confirmation_required"
        response["approval_policy"] = "confirmation_required"
        response["requires_confirmation"] = True
        response["requires_elevation"] = False
        response["message"] = (
            "命令不在免审批白名单模板内，需人工确认后执行。"
            f" 原因: {whitelist_reason or '未匹配模板约束'}"
        )
        return _finalize_precheck_response(
            request=request,
            response=response,
            command_meta=command_meta,
            dispatch_preview=dispatch_preview,
            whitelist_match=whitelist_match,
            whitelist_reason=whitelist_reason,
        )

    if bool(command_meta.get("requires_write_permission")):
        if not write_enabled() and not test_permissive_enabled():
            response["status"] = "permission_required"
            response["approval_policy"] = "deny"
            response["message"] = "write command is disabled by policy"
            response["requires_elevation"] = False
            return _finalize_precheck_response(
                request=request,
                response=response,
                command_meta=command_meta,
                dispatch_preview=dispatch_preview,
                whitelist_match=whitelist_match,
                whitelist_reason=whitelist_reason,
            )
        response["status"] = "elevation_required"
        response["approval_policy"] = "elevation_required"
        response["requires_confirmation"] = True
        response["message"] = "write command requires elevation and confirmation"
        return _finalize_precheck_response(
            request=request,
            response=response,
            command_meta=command_meta,
            dispatch_preview=dispatch_preview,
            whitelist_match=whitelist_match,
            whitelist_reason=whitelist_reason,
        )

    return _finalize_precheck_response(
        request=request,
        response=response,
        command_meta=command_meta,
        dispatch_preview=dispatch_preview,
        whitelist_match=whitelist_match,
        whitelist_reason=whitelist_reason,
    )


@router.post("/tickets/confirm")
async def confirm_ticket(request: TicketConfirmRequest) -> Dict[str, Any]:
    ok, reason, payload = consume_ticket(
        ticket_id=request.ticket_id,
        session_id=request.session_id,
        message_id=request.message_id,
        action_id=request.action_id,
        command=request.command,
        requires_elevation=bool(request.requires_elevation),
    )
    return {
        "status": "ok" if ok else "failed",
        "reason": reason,
        "ticket_id": request.ticket_id,
        "decision_id": as_str(payload.get("decision_id")),
    }


async def _prepare_execution(request: ExecuteRequest) -> Dict[str, Any]:
    effective_command = _resolve_effective_command(as_str(request.command), request.command_spec)
    if not effective_command:
        raise HTTPException(status_code=400, detail="command or command_spec is required")
    precheck = await precheck_command(
        PrecheckRequest(
            session_id=request.session_id,
            message_id=request.message_id,
            action_id=request.action_id,
            step_id=request.step_id,
            command=effective_command,
            command_spec=request.command_spec if isinstance(request.command_spec, dict) else {},
            purpose=request.purpose,
            target_kind=request.target_kind,
            target_identity=request.target_identity,
        )
    )
    pre_status = as_str(precheck.get("status")).lower()
    if pre_status in {"permission_required", "elevation_required", "confirmation_required"}:
        if pre_status in {"elevation_required", "confirmation_required"}:
            requires_elevation = pre_status == "elevation_required"
            if not bool(request.confirmed) or (requires_elevation and not bool(request.elevated)):
                return precheck
            safe_token = as_str(request.approval_token).strip() or as_str(request.confirmation_ticket)
            ok, reason, ticket_payload = consume_ticket(
                ticket_id=safe_token,
                session_id=request.session_id,
                message_id=request.message_id,
                action_id=request.action_id,
                command=as_str(precheck.get("command")),
                requires_elevation=requires_elevation,
            )
            if not ok:
                precheck["status"] = "confirmation_required"
                precheck["message"] = f"confirmation ticket invalid: {reason}"
                return precheck
            ticket_decision_id = as_str(ticket_payload.get("decision_id"))
            if ticket_decision_id:
                precheck["decision_id"] = ticket_decision_id
                policy_payload = precheck.get("policy_decision")
                if isinstance(policy_payload, dict):
                    policy_payload["decision_id"] = ticket_decision_id
                    precheck["policy_decision"] = policy_payload
            revoke_ticket(as_str(precheck.get("confirmation_ticket")))
            # Ticket validation succeeded, so this request is now cleared to execute.
            precheck["status"] = "ok"
            precheck.pop("confirmation_ticket", None)
            precheck.pop("ticket_expires_at", None)
        else:
            return precheck
    return precheck


@router.post("/runs")
async def create_command_run(request: CommandRunCreateRequest) -> Dict[str, Any]:
    safe_purpose = as_str(request.purpose).strip()
    if not safe_purpose:
        raise HTTPException(status_code=400, detail="purpose is required")
    precheck = await _prepare_execution(request)
    pre_status = as_str(precheck.get("status")).lower()
    if pre_status in {"permission_required", "elevation_required", "confirmation_required"}:
        return precheck

    runtime = get_exec_runtime_service()
    decision_id = as_str(precheck.get("decision_id"))
    resolved_target_context = (
        dict(precheck.get("resolved_target_context"))
        if isinstance(precheck.get("resolved_target_context"), dict)
        else {}
    )
    target_registry_payload = precheck.get("target_registry")
    target_metadata_contract = (
        dict(target_registry_payload.get("metadata_contract"))
        if isinstance(target_registry_payload, dict) and isinstance(target_registry_payload.get("metadata_contract"), dict)
        else {}
    )
    run = runtime.create_run(
        session_id=request.session_id,
        message_id=request.message_id,
        action_id=request.action_id,
        step_id=request.step_id,
        command=as_str(precheck.get("command")),
        command_spec=request.command_spec if isinstance(request.command_spec, dict) else {},
        purpose=safe_purpose,
        command_type=as_str(precheck.get("command_type"), "unknown"),
        risk_level=as_str(precheck.get("risk_level"), "high"),
        command_family=as_str(precheck.get("command_family"), "unknown"),
        approval_policy=as_str(precheck.get("approval_policy"), "deny"),
        executor_type=as_str(precheck.get("executor_type"), "local_process"),
        executor_profile=as_str(precheck.get("executor_profile"), "local-default"),
        target_kind=as_str(precheck.get("target_kind"), "runtime_node"),
        target_identity=as_str(precheck.get("target_identity"), "runtime:local"),
        resolved_target_context=resolved_target_context,
        target_metadata_contract=target_metadata_contract,
        policy_decision_id=decision_id,
        client_deadline_ms=int(request.client_deadline_ms or 0),
        timeout_seconds=int(request.timeout_seconds or 20),
    )
    if decision_id:
        bind_decision_to_run(decision_id, run_id=as_str(run.get("run_id")), command_run_id=as_str(run.get("run_id")))
        run["policy_decision_id"] = decision_id
    return {"run": run}


@router.post("/execute")
async def execute_command(request: ExecuteRequest) -> Dict[str, Any]:
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    result = await create_command_run(CommandRunCreateRequest(**payload))
    run = result.get("run")
    if not isinstance(run, dict):
        return result
    runtime = get_exec_runtime_service()
    final_run = await runtime.wait_for_run(as_str(run.get("run_id")))
    if not isinstance(final_run, dict):
        return {
            "status": "failed",
            "message": "run disappeared before completion",
            "session_id": request.session_id,
            "message_id": request.message_id,
            "command": request.command,
        }
    final_status = as_str(final_run.get("status"), "failed")
    if as_str(final_run.get("error_code")).strip().lower() == "backend_unavailable":
        compat_status = "backend_unavailable"
    else:
        compat_status = "executed" if final_status == "completed" else final_status
    return {
        **final_run,
        "status": compat_status,
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    runtime = get_exec_runtime_service()
    run = runtime.get_run(run_id)
    if isinstance(run, dict):
        return {"run": run}
    return {"status": "not_found", "run_id": run_id}


@router.get("/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    runtime = get_exec_runtime_service()
    run = runtime.get_run(run_id)
    if not isinstance(run, dict):
        raise HTTPException(status_code=404, detail="run not found")
    events = runtime.list_events(run_id, after_seq=after_seq, limit=limit)
    next_after_seq = int(events[-1].get("seq", after_seq)) if events else int(after_seq)
    return {
        "run_id": run_id,
        "next_after_seq": next_after_seq,
        "events": events,
    }


@router.get("/runs/{run_id}/replay")
async def get_run_replay(
    run_id: str,
    events_limit: int = Query(2000, ge=1, le=5000),
    decisions_limit: int = Query(200, ge=1, le=1000),
    audit_limit: int = Query(1000, ge=1, le=5000),
) -> Dict[str, Any]:
    try:
        safe_events_limit = max(1, min(int(events_limit), 5000))
    except Exception:
        safe_events_limit = 2000
    try:
        safe_decisions_limit = max(1, min(int(decisions_limit), 1000))
    except Exception:
        safe_decisions_limit = 200
    try:
        safe_audit_limit = max(1, min(int(audit_limit), 5000))
    except Exception:
        safe_audit_limit = 1000

    runtime = get_exec_runtime_service()
    run = runtime.get_run(run_id)
    if not isinstance(run, dict):
        raise HTTPException(status_code=404, detail="run not found")

    events = runtime.list_events(run_id, after_seq=0, limit=safe_events_limit)
    decisions_payload = list_policy_decisions(
        limit=safe_decisions_limit,
        run_id=run_id,
    )
    audit_rows = list_audits(limit=safe_audit_limit, run_id=run_id)

    return {
        "run_id": run_id,
        "run": run,
        "events": events,
        "policy_decisions": decisions_payload,
        "audit_rows": audit_rows,
        "generated_at": utc_now_iso(),
    }


@router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    after_seq: int = Query(0, ge=0),
) -> StreamingResponse:
    runtime = get_exec_runtime_service()
    run = runtime.get_run(run_id)

    async def _generator():
        if not isinstance(run, dict):
            yield _format_sse_event("error", {"status_code": 404, "detail": "run not found"})
            return
        last_seq = int(after_seq or 0)
        queue = runtime.subscribe(run_id)
        loop = asyncio.get_running_loop()
        terminal_observed_at: float | None = None
        try:
            while True:
                emitted_backlog = False
                for event in runtime.list_events(run_id, after_seq=last_seq, limit=5000):
                    event_seq = int(event.get("seq", 0))
                    if event_seq <= last_seq:
                        continue
                    last_seq = event_seq
                    emitted_backlog = True
                    yield _format_sse_event(as_str(event.get("event_type")), event)

                current_run = runtime.get_run(run_id)
                terminal_now = isinstance(current_run, dict) and is_terminal_status(current_run.get("status"))
                if terminal_now and not emitted_backlog:
                    now_ts = loop.time()
                    if terminal_observed_at is None:
                        terminal_observed_at = now_ts
                    elif (now_ts - terminal_observed_at) >= 0.6:
                        break
                else:
                    terminal_observed_at = None

                wait_timeout = 0.2 if terminal_now else 15.0
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    if terminal_now:
                        break
                    continue
                item_seq = int(item.get("seq", 0))
                if item_seq <= last_seq:
                    continue
                last_seq = item_seq
                yield _format_sse_event(as_str(item.get("event_type")), item)
        finally:
            runtime.unsubscribe(run_id, queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_generator(), media_type="text/event-stream", headers=headers)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> Dict[str, Any]:
    runtime = get_exec_runtime_service()
    run = await runtime.cancel_run(run_id)
    if not isinstance(run, dict):
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


@router.get("/audit")
async def get_audit(limit: int = 100) -> Dict[str, Any]:
    rows = list_audits(limit=limit)
    return {
        "total": len(rows),
        "rows": rows,
        "generated_at": utc_now_iso(),
        "retention_note": audit_retention_note(),
    }


@router.get("/policy/decisions")
async def get_policy_decisions(
    limit: int = Query(100, ge=1, le=1000),
    run_id: str = "",
    action_id: str = "",
    result: str = "",
) -> Dict[str, Any]:
    rows = list_policy_decisions(
        limit=limit,
        run_id=run_id,
        action_id=action_id,
        result=result,
    )
    return {
        "total": len(rows),
        "rows": rows,
        "generated_at": utc_now_iso(),
    }


@router.get("/policy/decisions/{decision_id}")
async def get_policy_decision(decision_id: str) -> Dict[str, Any]:
    row = get_policy_decision_record(decision_id)
    if not isinstance(row, dict):
        raise HTTPException(status_code=404, detail="policy decision not found")
    return {"decision": row}


@router.get("/executors")
async def get_executors() -> Dict[str, Any]:
    rows = list_executor_statuses()
    return {
        "total": len(rows),
        "ready": sum(1 for item in rows if bool(item.get("dispatch_ready"))),
        "rows": rows,
        "generated_at": utc_now_iso(),
    }
