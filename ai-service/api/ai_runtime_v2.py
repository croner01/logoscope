"""
AI Runtime API v2.

Thread-Run-Action contract over existing secure runtime chain.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ai.runtime_v4 import get_runtime_v4_bridge
from ai.runtime_v4.adapter.event_mapper import map_run_snapshot
from ai.runtime_v4.api_models import (
    ApprovalResolveRequest,
    CancelRequest,
    CommandActionRequest,
    InputSubmitRequest,
    InterruptRequest,
    RunCreateRequest,
    TargetDeactivateRequest,
    TargetRegisterRequest,
    TargetResolveByIdentityRequest,
    TargetResolveRequest,
    ThreadCreateRequest,
)
from ai.runtime_v4.langgraph.graph import inner_engine_name
from ai.runtime_v4.store import get_runtime_v4_thread_store
from ai.runtime_v4.temporal.client import get_temporal_outer_client
from ai.runtime_v4.targets import get_runtime_v4_target_registry


router = APIRouter(prefix="/api/v2", tags=["ai-runtime-v2"])
logger = logging.getLogger(__name__)

_RUNTIME_EVENT_VISIBILITY_DEFAULT = "default"
_RUNTIME_EVENT_VISIBILITY_DEBUG = "debug"
_RUNTIME_EVENT_VISIBILITY_OPTIONS = {
    _RUNTIME_EVENT_VISIBILITY_DEFAULT,
    _RUNTIME_EVENT_VISIBILITY_DEBUG,
}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _safe_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_runtime_event_visibility(value: Any) -> str:
    normalized = _as_str(value).strip().lower()
    if normalized in _RUNTIME_EVENT_VISIBILITY_OPTIONS:
        return normalized
    return _RUNTIME_EVENT_VISIBILITY_DEFAULT


def _normalize_idempotency_key(value: Any) -> str:
    key = _as_str(value).strip()
    if not key:
        return ""
    # Keep bounded storage footprint for in-memory index.
    return key[:128]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return int(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _clamp_command_timeout_seconds(value: Any, default: int = 20) -> int:
    timeout = _as_int(value, default)
    if timeout <= 0:
        timeout = int(default)
    return max(3, min(timeout, 180))


def _build_generic_exec_command_spec(
    *,
    command: str,
    timeout_seconds: int,
    purpose: str = "",
    title: str = "",
    step_id: str = "",
) -> Dict[str, Any]:
    safe_command = _as_str(command).strip()
    safe_timeout = _clamp_command_timeout_seconds(timeout_seconds, default=20)
    command_spec: Dict[str, Any] = {
        "tool": "generic_exec",
        "args": {
            "command": safe_command,
            "timeout_s": safe_timeout,
        },
        "command": safe_command,
        "timeout_s": safe_timeout,
    }
    safe_step_id = _as_str(step_id).strip()
    if safe_step_id:
        command_spec["step_id"] = safe_step_id
    safe_purpose = _as_str(purpose).strip()
    if safe_purpose:
        command_spec["purpose"] = safe_purpose
    safe_title = _as_str(title).strip()
    if safe_title:
        command_spec["title"] = safe_title
    return command_spec


def _runtime_create_run_retry_delays_seconds() -> List[float]:
    raw = _as_str(os.getenv("AI_RUNTIME_V2_CREATE_RUN_RETRY_DELAYS_MS"), "200,600")
    delays: List[float] = []
    for segment in raw.split(","):
        millis = _as_int(segment, 0)
        if millis <= 0:
            continue
        delays.append(min(millis, 5000) / 1000.0)
    if not delays:
        delays = [0.2, 0.6]
    return delays[:5]


def _runtime_create_run_max_attempts(default_attempts: int) -> int:
    configured = _as_int(
        os.getenv("AI_RUNTIME_V2_CREATE_RUN_MAX_ATTEMPTS"),
        default_attempts,
    )
    return max(1, min(configured, 6))


def _runtime_create_run_retry_after_seconds() -> int:
    return max(
        1,
        _as_int(os.getenv("AI_RUNTIME_V2_CREATE_RUN_RETRY_AFTER_SECONDS"), 2),
    )


def _infer_runtime_backend_dependency(reason_text: str) -> str:
    lowered = _as_str(reason_text).strip().lower()
    if "temporal" in lowered:
        return "temporal"
    if "langgraph" in lowered:
        return "langgraph"
    return "runtime_v4"


def _build_runtime_backend_unavailable_detail(
    *,
    error: RuntimeError,
    attempt: int,
    max_attempts: int,
) -> Dict[str, Any]:
    reason = _as_str(error).strip() or "runtime backend unavailable"
    return {
        "code": "runtime_outer_backend_unavailable",
        "message": "runtime v4 backend unavailable",
        "dependency": _infer_runtime_backend_dependency(reason),
        "reason": reason,
        "attempt": int(attempt),
        "max_attempts": int(max_attempts),
        "retry_after_s": _runtime_create_run_retry_after_seconds(),
    }


def _resolve_outer_engine(run_id: str) -> str:
    temporal_client = get_temporal_outer_client()
    workflow_state = temporal_client.get_workflow_for_run(run_id)
    outer = temporal_client.outer_engine_name()
    if workflow_state is not None and outer.startswith("temporal-v1"):
        return "temporal-v1"
    return outer


def _resolve_thread_id(run_payload: Dict[str, Any], *, fallback: str = "") -> str:
    context_json = _safe_dict(run_payload.get("context_json"))
    from_context = _as_str(context_json.get("thread_id")).strip()
    if from_context:
        return from_context
    return _as_str(fallback).strip() or get_runtime_v4_thread_store().thread_id_for_run(_as_str(run_payload.get("run_id")))


def _map_v2_run_snapshot(run_payload: Dict[str, Any], *, fallback_thread_id: str = "") -> Dict[str, Any]:
    safe_run = _safe_dict(run_payload)
    run_id = _as_str(safe_run.get("run_id")).strip()
    return map_run_snapshot(
        safe_run,
        thread_id=_resolve_thread_id(safe_run, fallback=fallback_thread_id),
        outer_engine=_resolve_outer_engine(run_id),
        inner_engine=inner_engine_name(),
    )


async def _fetch_run_payload_via_bridge(run_id: str) -> Optional[Dict[str, Any]]:
    bridge = get_runtime_v4_bridge()
    try:
        result = await bridge.get_run(_as_str(run_id))
    except HTTPException as exc:
        if int(exc.status_code) == 404:
            return None
        raise
    run_payload = _safe_dict(_safe_dict(result).get("run"))
    if not _as_str(run_payload.get("run_id")):
        return None
    return run_payload


def _normalize_approval_status(payload: Dict[str, Any], *, fallback: str = "resolved") -> str:
    decision = _as_str(payload.get("decision")).strip().lower()
    if decision in {"approved", "rejected"}:
        return decision
    return _as_str(fallback, "resolved")


def _approval_sort_key(payload: Dict[str, Any]) -> str:
    safe_payload = _safe_dict(payload)
    return (
        _as_str(safe_payload.get("resolved_at"))
        or _as_str(safe_payload.get("requested_at"))
        or _as_str(safe_payload.get("created_at"))
    )


def _extract_run_approvals(run_id: str, run_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = _safe_dict(run_payload.get("summary_json"))
    pending = _safe_dict(summary.get("pending_approval"))
    last_approval = _safe_dict(summary.get("last_approval"))
    history = _safe_list(summary.get("approval_history"))

    ordered_candidates: List[Dict[str, Any]] = []
    if pending and _as_str(pending.get("approval_id")).strip():
        ordered_candidates.append(
            {
                "approval_id": _as_str(pending.get("approval_id")),
                "run_id": run_id,
                "status": "pending",
                "decision": "",
                "comment": "",
                "title": _as_str(pending.get("title")),
                "reason": _as_str(pending.get("reason")),
                "command": _as_str(pending.get("command")),
                "purpose": _as_str(pending.get("purpose")),
                "requires_confirmation": bool(pending.get("requires_confirmation")),
                "requires_elevation": bool(pending.get("requires_elevation")),
                "requested_at": _as_str(pending.get("requested_at")),
                "resolved_at": "",
                "expires_at": _as_str(pending.get("expires_at")),
            }
        )

    if last_approval and _as_str(last_approval.get("approval_id")).strip():
        ordered_candidates.append(
            {
                "approval_id": _as_str(last_approval.get("approval_id")),
                "run_id": run_id,
                "status": _normalize_approval_status(last_approval),
                "decision": _as_str(last_approval.get("decision")),
                "comment": _as_str(last_approval.get("comment")),
                "title": "",
                "reason": "",
                "command": "",
                "purpose": "",
                "requires_confirmation": bool(last_approval.get("confirmed")),
                "requires_elevation": bool(last_approval.get("elevated")),
                "requested_at": "",
                "resolved_at": _as_str(last_approval.get("resolved_at")),
                "expires_at": "",
            }
        )

    for item in reversed(history):
        safe_item = _safe_dict(item)
        if not _as_str(safe_item.get("approval_id")).strip():
            continue
        ordered_candidates.append(
            {
                "approval_id": _as_str(safe_item.get("approval_id")),
                "run_id": run_id,
                "status": _normalize_approval_status(safe_item),
                "decision": _as_str(safe_item.get("decision")),
                "comment": _as_str(safe_item.get("comment")),
                "title": "",
                "reason": "",
                "command": "",
                "purpose": "",
                "requires_confirmation": bool(safe_item.get("confirmed")),
                "requires_elevation": bool(safe_item.get("elevated")),
                "requested_at": "",
                "resolved_at": _as_str(safe_item.get("resolved_at")),
                "expires_at": "",
            }
        )

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered_candidates:
        approval_id = _as_str(item.get("approval_id")).strip()
        if not approval_id or approval_id in seen:
            continue
        seen.add(approval_id)
        deduped.append(item)
    deduped.sort(key=_approval_sort_key, reverse=True)
    return deduped


def _action_key(payload: Dict[str, Any], *, event_seq: int) -> str:
    safe_payload = _safe_dict(payload)
    return (
        _as_str(safe_payload.get("action_id")).strip()
        or _as_str(safe_payload.get("tool_call_id")).strip()
        or _as_str(safe_payload.get("approval_id")).strip()
        or _as_str(safe_payload.get("command_run_id")).strip()
        or f"seq-{event_seq}"
    )


def _is_action_event(event_type: str) -> bool:
    safe_type = _as_str(event_type).strip().lower()
    return safe_type in {
        "tool_call_started",
        "tool_call_finished",
        "tool_call_skipped_duplicate",
        "approval_required",
        "approval_resolved",
        "action_waiting_approval",
        "action_waiting_user_input",
        "action_spec_validated",
        "action_preflight_failed",
        "action_execution_retrying",
    }


def _extract_run_actions(run_id: str, events: Any) -> List[Dict[str, Any]]:
    safe_events = _safe_list(events)
    actions_by_key: Dict[str, Dict[str, Any]] = {}

    for item in safe_events:
        event = _safe_dict(item)
        event_type = _as_str(event.get("event_type")).strip().lower()
        if not _is_action_event(event_type):
            continue
        payload = _safe_dict(event.get("payload"))
        seq = int(event.get("seq") or 0)
        created_at = _as_str(event.get("created_at"))
        key = _action_key(payload, event_seq=seq)
        if (
            key.startswith("seq-")
            and not _as_str(payload.get("command")).strip()
            and not _as_str(payload.get("title")).strip()
            and not _as_str(payload.get("action_id")).strip()
            and not _as_str(payload.get("tool_call_id")).strip()
            and not _as_str(payload.get("approval_id")).strip()
            and not _as_str(payload.get("command_run_id")).strip()
        ):
            continue
        action = actions_by_key.get(key)
        if action is None:
            initial_status = "pending"
            if event_type == "tool_call_started":
                initial_status = _as_str(payload.get("status"), "running")
            elif event_type == "tool_call_finished":
                initial_status = _as_str(payload.get("status"), "completed")
            elif event_type == "tool_call_skipped_duplicate":
                initial_status = _as_str(payload.get("status"), "skipped")
            elif event_type in {"approval_required", "action_waiting_approval"}:
                initial_status = "waiting_approval"
            elif event_type == "action_waiting_user_input":
                initial_status = "waiting_user_input"
            elif event_type == "approval_resolved":
                initial_status = _as_str(payload.get("decision"), "resolved")
            elif event_type == "action_preflight_failed":
                initial_status = "failed"
            elif event_type == "action_execution_retrying":
                initial_status = "running"
            elif event_type == "action_spec_validated":
                initial_status = "validated"
            action = {
                "action_id": _as_str(payload.get("action_id")) or key,
                "run_id": run_id,
                "type": "command",
                "status": initial_status,
                "tool_call_id": _as_str(payload.get("tool_call_id")),
                "command_run_id": _as_str(payload.get("command_run_id")),
                "command": _as_str(payload.get("command")),
                "purpose": _as_str(payload.get("purpose")),
                "title": _as_str(payload.get("title")),
                "target_kind": _as_str(payload.get("target_kind")),
                "target_identity": _as_str(payload.get("target_identity")),
                "executor_profile": _as_str(payload.get("executor_profile")),
                "approval_policy": _as_str(payload.get("approval_policy")),
                "reason_code": _as_str(payload.get("reason_code")).strip().lower(),
                "evidence_slot_id": _as_str(payload.get("evidence_slot_id")),
                "evidence_outcome": _as_str(payload.get("evidence_outcome")),
                "evidence_reuse": bool(payload.get("evidence_reuse")),
                "reused_evidence_ids": _safe_list(payload.get("reused_evidence_ids")),
                "evidence_slot_ids_filled": _safe_list(payload.get("evidence_slot_ids_filled")),
                "info_gain_score": _as_float(payload.get("info_gain_score"), 0.0),
                "created_at": created_at,
                "updated_at": created_at,
            }
            actions_by_key[key] = action

        if event_type == "action_spec_validated":
            action["status"] = "validated"
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "action_preflight_failed":
            action["status"] = "failed"
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["error_code"] = _as_str(payload.get("error_code"))
            action["message"] = _as_str(payload.get("message"))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "action_execution_retrying":
            action["status"] = "running"
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["attempt"] = int(payload.get("attempt") or 0)
            action["max_attempts"] = int(payload.get("max_attempts") or 0)
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "tool_call_started":
            action["status"] = _as_str(payload.get("status"), "running")
            action["tool_call_id"] = _as_str(payload.get("tool_call_id")) or _as_str(action.get("tool_call_id"))
            action["command_run_id"] = _as_str(payload.get("command_run_id")) or _as_str(action.get("command_run_id"))
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["target_kind"] = _as_str(payload.get("target_kind")) or _as_str(action.get("target_kind"))
            action["target_identity"] = _as_str(payload.get("target_identity")) or _as_str(action.get("target_identity"))
            action["executor_profile"] = _as_str(payload.get("executor_profile")) or _as_str(action.get("executor_profile"))
            action["approval_policy"] = _as_str(payload.get("approval_policy")) or _as_str(action.get("approval_policy"))
            action["evidence_slot_id"] = _as_str(payload.get("evidence_slot_id")) or _as_str(action.get("evidence_slot_id"))
            action["evidence_outcome"] = _as_str(payload.get("evidence_outcome")) or _as_str(action.get("evidence_outcome"))
            action["evidence_reuse"] = bool(payload.get("evidence_reuse")) or bool(action.get("evidence_reuse"))
            action["reused_evidence_ids"] = _safe_list(payload.get("reused_evidence_ids")) or _safe_list(action.get("reused_evidence_ids"))
            action["evidence_slot_ids_filled"] = _safe_list(payload.get("evidence_slot_ids_filled")) or _safe_list(action.get("evidence_slot_ids_filled"))
            action["info_gain_score"] = _as_float(payload.get("info_gain_score"), _as_float(action.get("info_gain_score"), 0.0))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "tool_call_finished":
            action["status"] = _as_str(payload.get("status"), "completed")
            action["tool_call_id"] = _as_str(payload.get("tool_call_id")) or _as_str(action.get("tool_call_id"))
            action["command_run_id"] = _as_str(payload.get("command_run_id")) or _as_str(action.get("command_run_id"))
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["target_kind"] = _as_str(payload.get("target_kind")) or _as_str(action.get("target_kind"))
            action["target_identity"] = _as_str(payload.get("target_identity")) or _as_str(action.get("target_identity"))
            action["executor_profile"] = _as_str(payload.get("executor_profile")) or _as_str(action.get("executor_profile"))
            action["approval_policy"] = _as_str(payload.get("approval_policy")) or _as_str(action.get("approval_policy"))
            action["exit_code"] = int(payload.get("exit_code") or 0)
            action["duration_ms"] = int(payload.get("duration_ms") or 0)
            action["timed_out"] = bool(payload.get("timed_out"))
            action["reason_code"] = _as_str(payload.get("reason_code")).strip().lower() or _as_str(action.get("reason_code")).strip().lower()
            action["evidence_slot_id"] = _as_str(payload.get("evidence_slot_id")) or _as_str(action.get("evidence_slot_id"))
            action["evidence_outcome"] = _as_str(payload.get("evidence_outcome")) or _as_str(action.get("evidence_outcome"))
            action["evidence_reuse"] = bool(payload.get("evidence_reuse")) or bool(action.get("evidence_reuse"))
            action["reused_evidence_ids"] = _safe_list(payload.get("reused_evidence_ids")) or _safe_list(action.get("reused_evidence_ids"))
            action["evidence_slot_ids_filled"] = _safe_list(payload.get("evidence_slot_ids_filled")) or _safe_list(action.get("evidence_slot_ids_filled"))
            action["info_gain_score"] = _as_float(payload.get("info_gain_score"), _as_float(action.get("info_gain_score"), 0.0))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "tool_call_skipped_duplicate":
            action["status"] = _as_str(payload.get("status"), "skipped")
            action["tool_call_id"] = _as_str(payload.get("tool_call_id")) or _as_str(action.get("tool_call_id"))
            action["command_run_id"] = _as_str(payload.get("command_run_id")) or _as_str(action.get("command_run_id"))
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["target_kind"] = _as_str(payload.get("target_kind")) or _as_str(action.get("target_kind"))
            action["target_identity"] = _as_str(payload.get("target_identity")) or _as_str(action.get("target_identity"))
            action["executor_profile"] = _as_str(payload.get("executor_profile")) or _as_str(action.get("executor_profile"))
            action["approval_policy"] = _as_str(payload.get("approval_policy")) or _as_str(action.get("approval_policy"))
            action["reason_code"] = _as_str(payload.get("reason_code")).strip().lower() or _as_str(action.get("reason_code")).strip().lower()
            action["evidence_slot_id"] = _as_str(payload.get("evidence_slot_id")) or _as_str(action.get("evidence_slot_id"))
            action["evidence_outcome"] = (
                _as_str(payload.get("evidence_outcome"))
                or ("reused" if bool(payload.get("evidence_reuse")) else "")
                or _as_str(action.get("evidence_outcome"))
            )
            action["evidence_reuse"] = bool(payload.get("evidence_reuse")) or bool(action.get("evidence_reuse"))
            action["reused_evidence_ids"] = _safe_list(payload.get("reused_evidence_ids")) or _safe_list(action.get("reused_evidence_ids"))
            action["evidence_slot_ids_filled"] = _safe_list(payload.get("evidence_slot_ids_filled")) or _safe_list(action.get("evidence_slot_ids_filled"))
            action["info_gain_score"] = _as_float(payload.get("info_gain_score"), _as_float(action.get("info_gain_score"), 0.0))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type in {"approval_required", "action_waiting_approval"}:
            action["status"] = "waiting_approval"
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["target_kind"] = _as_str(payload.get("target_kind")) or _as_str(action.get("target_kind"))
            action["target_identity"] = _as_str(payload.get("target_identity")) or _as_str(action.get("target_identity"))
            action["executor_profile"] = _as_str(payload.get("executor_profile")) or _as_str(action.get("executor_profile"))
            action["approval_policy"] = _as_str(payload.get("approval_policy")) or _as_str(action.get("approval_policy"))
            action["approval_id"] = _as_str(payload.get("approval_id") or payload.get("confirmation_ticket"))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "action_waiting_user_input":
            action["status"] = "waiting_user_input"
            action["command"] = _as_str(payload.get("command")) or _as_str(action.get("command"))
            action["purpose"] = _as_str(payload.get("purpose")) or _as_str(action.get("purpose"))
            action["title"] = _as_str(payload.get("title")) or _as_str(action.get("title"))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))
            continue

        if event_type == "approval_resolved":
            action["status"] = _normalize_approval_status(payload, fallback="resolved")
            action["approval_id"] = _as_str(payload.get("approval_id")) or _as_str(action.get("approval_id"))
            action["updated_at"] = created_at or _as_str(action.get("updated_at"))

    actions = list(actions_by_key.values())
    actions.sort(key=lambda item: _as_str(item.get("created_at")), reverse=True)
    return actions


@router.post("/targets")
async def register_target(request: TargetRegisterRequest) -> Dict[str, Any]:
    registry = get_runtime_v4_target_registry()
    try:
        result = registry.upsert_target(
            target_id=_as_str(request.target_id),
            target_kind=_as_str(request.target_kind, "unknown"),
            target_identity=_as_str(request.target_identity, "unknown"),
            display_name=_as_str(request.display_name),
            description=_as_str(request.description),
            capabilities=_safe_list(request.capabilities),
            credential_scope=_safe_dict(request.credential_scope),
            metadata=_safe_dict(request.metadata),
            updated_by=_as_str(request.updated_by, "system"),
            reason=_as_str(request.reason),
            run_id=_as_str(request.run_id),
            action_id=_as_str(request.action_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("/targets")
async def list_targets(
    status: str = Query("", description="active/inactive"),
    target_kind: str = Query("", description="k8s_cluster/clickhouse/http..."),
    capability: str = Query("", description="read_logs/restart_workload/..."),
    limit: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    safe_status = status if isinstance(status, str) else ""
    safe_target_kind = target_kind if isinstance(target_kind, str) else ""
    safe_capability = capability if isinstance(capability, str) else ""
    try:
        safe_limit = int(limit)
    except Exception:
        safe_limit = 200
    safe_limit = max(1, min(safe_limit, 5000))
    registry = get_runtime_v4_target_registry()
    targets = registry.list_targets(
        status=_as_str(safe_status),
        target_kind=_as_str(safe_target_kind),
        capability=_as_str(safe_capability),
        limit=safe_limit,
    )
    return {"targets": targets}


@router.get("/targets/changes")
async def list_target_changes(
    target_id: str = Query("", description="optional target id"),
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    safe_target_id = target_id if isinstance(target_id, str) else ""
    try:
        safe_after_seq = int(after_seq)
    except Exception:
        safe_after_seq = 0
    safe_after_seq = max(0, safe_after_seq)
    try:
        safe_limit = int(limit)
    except Exception:
        safe_limit = 200
    safe_limit = max(1, min(safe_limit, 5000))
    registry = get_runtime_v4_target_registry()
    changes = registry.list_changes(target_id=_as_str(safe_target_id), after_seq=safe_after_seq, limit=safe_limit)
    next_after_seq = int(changes[-1]["seq"]) if changes else int(safe_after_seq)
    return {
        "changes": changes,
        "next_after_seq": next_after_seq,
    }


@router.get("/targets/{target_id}")
async def get_target(target_id: str) -> Dict[str, Any]:
    registry = get_runtime_v4_target_registry()
    target = registry.get_target(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {"target": target}


@router.post("/targets/{target_id}/resolve")
async def resolve_target(target_id: str, request: TargetResolveRequest) -> Dict[str, Any]:
    registry = get_runtime_v4_target_registry()
    resolution = registry.resolve_target(
        target_id=_as_str(target_id),
        required_capabilities=_safe_list(request.required_capabilities),
        run_id=_as_str(request.run_id),
        action_id=_as_str(request.action_id),
        reason=_as_str(request.reason),
    )
    return {"resolution": resolution}


@router.post("/targets/resolve/by-identity")
async def resolve_target_by_identity(request: TargetResolveByIdentityRequest) -> Dict[str, Any]:
    registry = get_runtime_v4_target_registry()
    resolution = registry.resolve_target_by_identity(
        target_kind=_as_str(request.target_kind, "unknown"),
        target_identity=_as_str(request.target_identity),
        required_capabilities=_safe_list(request.required_capabilities),
        run_id=_as_str(request.run_id),
        action_id=_as_str(request.action_id),
        reason=_as_str(request.reason),
    )
    return {"resolution": resolution}


@router.post("/targets/{target_id}/deactivate")
async def deactivate_target(target_id: str, request: TargetDeactivateRequest) -> Dict[str, Any]:
    registry = get_runtime_v4_target_registry()
    result = registry.deactivate_target(
        _as_str(target_id),
        updated_by=_as_str(request.updated_by, "system"),
        reason=_as_str(request.reason),
        run_id=_as_str(request.run_id),
        action_id=_as_str(request.action_id),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="target not found")
    return result


@router.post("/threads")
async def create_thread(request: ThreadCreateRequest) -> Dict[str, Any]:
    thread_store = get_runtime_v4_thread_store()
    thread = thread_store.create_thread(
        session_id=_as_str(request.session_id),
        conversation_id=_as_str(request.conversation_id),
        title=_as_str(request.title) or "AI Runtime Thread",
    )
    return {"thread": thread.to_dict()}


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> Dict[str, Any]:
    thread_store = get_runtime_v4_thread_store()
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    latest_run = None
    latest_run_id = thread_store.latest_run_id_for_thread(thread.thread_id)
    if latest_run_id:
        run_payload = await _fetch_run_payload_via_bridge(latest_run_id)
        if run_payload is not None:
            latest_run = _map_v2_run_snapshot(run_payload, fallback_thread_id=thread.thread_id)
    return {"thread": thread.to_dict(), "latest_run": latest_run}


@router.get("/threads/{thread_id}/runs")
async def list_thread_runs(
    thread_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
) -> Dict[str, Any]:
    thread_store = get_runtime_v4_thread_store()
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    run_ids = thread_store.run_ids_for_thread(thread.thread_id)
    selected_ids = run_ids[after : after + limit]

    runs: List[Dict[str, Any]] = []
    for run_id in selected_ids:
        run_payload = await _fetch_run_payload_via_bridge(run_id)
        if run_payload is None:
            continue
        runs.append(_map_v2_run_snapshot(run_payload, fallback_thread_id=thread.thread_id))

    return {
        "thread_id": thread.thread_id,
        "runs": runs,
        "next_after": after + len(selected_ids),
        "total": len(run_ids),
    }


@router.post("/threads/{thread_id}/runs")
async def create_thread_run(thread_id: str, request: RunCreateRequest) -> Dict[str, Any]:
    thread_store = get_runtime_v4_thread_store()
    thread = thread_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    idempotency_key = _normalize_idempotency_key(getattr(request, "idempotency_key", ""))
    if idempotency_key:
        existing_run_id = thread_store.run_id_for_idempotency_key(
            thread_id=thread.thread_id,
            idempotency_key=idempotency_key,
        )
        if existing_run_id:
            existing_payload = await _fetch_run_payload_via_bridge(existing_run_id)
            if isinstance(existing_payload, dict) and _as_str(existing_payload.get("run_id")).strip():
                mapped_existing = map_run_snapshot(
                    existing_payload,
                    thread_id=thread.thread_id,
                    outer_engine=_resolve_outer_engine(existing_run_id),
                    inner_engine=inner_engine_name(),
                )
                return {
                    "run": mapped_existing,
                    "workflow_id": "",
                    "idempotent_reused": True,
                }

    bridge = get_runtime_v4_bridge()
    analysis_context = request.analysis_context if isinstance(request.analysis_context, dict) else {}
    runtime_options = request.runtime_options if isinstance(request.runtime_options, dict) else {}
    client_deadline_ms = _as_int(getattr(request, "client_deadline_ms", 0), 0)
    pipeline_steps = request.pipeline_steps if isinstance(getattr(request, "pipeline_steps", None), list) else []
    if client_deadline_ms > 0:
        runtime_options["client_deadline_ms"] = int(client_deadline_ms)
        analysis_context["client_deadline_ms"] = int(client_deadline_ms)
    if pipeline_steps:
        safe_steps = [item for item in pipeline_steps if isinstance(item, dict)]
        if safe_steps:
            analysis_context["pipeline_steps"] = safe_steps[:3]
            runtime_options["pipeline_steps"] = safe_steps[:3]
    retry_delays = _runtime_create_run_retry_delays_seconds()
    max_attempts = _runtime_create_run_max_attempts(1 + len(retry_delays))
    create_result: Dict[str, Any] = {}

    for attempt in range(1, max_attempts + 1):
        try:
            create_result = await bridge.create_run(
                thread_id=thread.thread_id,
                session_id=thread.session_id,
                question=_as_str(request.question),
                analysis_context=analysis_context,
                runtime_options=runtime_options,
            )
            break
        except RuntimeError as exc:
            if attempt >= max_attempts:
                raise HTTPException(
                    status_code=503,
                    detail=_build_runtime_backend_unavailable_detail(
                        error=exc,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    ),
                )
            delay_seconds = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
            logger.warning(
                "runtime v4 create_run attempt %s/%s failed: %s; retry in %.3fs",
                attempt,
                max_attempts,
                _as_str(exc),
                delay_seconds,
            )
            await asyncio.sleep(delay_seconds)

    mapped_run = map_run_snapshot(
        create_result.get("run") if isinstance(create_result, dict) else {},
        thread_id=thread.thread_id,
        outer_engine=_as_str(create_result.get("outer_engine"), get_temporal_outer_client().outer_engine_name()),
        inner_engine=_as_str(create_result.get("inner_engine"), inner_engine_name()),
    )
    created_run_id = _as_str(mapped_run.get("run_id")).strip()
    if idempotency_key and created_run_id:
        thread_store.bind_idempotency_run(
            thread_id=thread.thread_id,
            idempotency_key=idempotency_key,
            run_id=created_run_id,
        )
    return {
        "run": mapped_run,
        "workflow_id": _as_str(create_result.get("workflow_id")),
        "idempotent_reused": False,
    }


@router.get("/runs/{run_id}")
async def get_run_snapshot(run_id: str) -> Dict[str, Any]:
    run_payload = await _fetch_run_payload_via_bridge(run_id)
    if run_payload is None:
        raise HTTPException(status_code=404, detail="run not found")
    mapped_run = _map_v2_run_snapshot(run_payload)
    return {"run": mapped_run}


@router.get("/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
    visibility: str = Query(_RUNTIME_EVENT_VISIBILITY_DEFAULT),
) -> Dict[str, Any]:
    bridge = get_runtime_v4_bridge()
    return await bridge.get_run_events(
        run_id,
        after_seq=after_seq,
        limit=limit,
        visibility=_normalize_runtime_event_visibility(visibility),
    )


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(
    run_id: str,
    after_seq: int = Query(0, ge=0),
    visibility: str = Query(_RUNTIME_EVENT_VISIBILITY_DEFAULT),
):
    bridge = get_runtime_v4_bridge()
    return await bridge.stream_run(
        run_id,
        after_seq=after_seq,
        visibility=_normalize_runtime_event_visibility(visibility),
    )


@router.get("/runs/{run_id}/approvals")
async def list_run_approvals(run_id: str) -> Dict[str, Any]:
    run_payload = await _fetch_run_payload_via_bridge(run_id)
    if run_payload is None:
        raise HTTPException(status_code=404, detail="run not found")
    approvals = _extract_run_approvals(run_id, run_payload)
    return {
        "run_id": run_id,
        "approvals": approvals,
    }


@router.get("/runs/{run_id}/actions")
async def list_run_actions(
    run_id: str,
    limit: int = Query(200, ge=1, le=5000),
) -> Dict[str, Any]:
    try:
        safe_limit = int(limit)
    except Exception:
        safe_limit = 200
    safe_limit = max(1, min(safe_limit, 5000))
    run_payload = await _fetch_run_payload_via_bridge(run_id)
    if run_payload is None:
        raise HTTPException(status_code=404, detail="run not found")
    bridge = get_runtime_v4_bridge()
    event_payload = await bridge.get_run_events(
        run_id,
        after_seq=0,
        limit=5000,
        visibility=_RUNTIME_EVENT_VISIBILITY_DEBUG,
    )
    actions = _extract_run_actions(run_id, _safe_dict(event_payload).get("events"))
    return {
        "run_id": run_id,
        "actions": actions[:safe_limit],
    }


@router.post("/runs/{run_id}/interrupt")
async def interrupt_run(run_id: str, request: InterruptRequest) -> Dict[str, Any]:
    bridge = get_runtime_v4_bridge()
    result = await bridge.interrupt_run(run_id=run_id, reason=_as_str(request.reason, "user_interrupt_esc"))
    return result


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: CancelRequest) -> Dict[str, Any]:
    bridge = get_runtime_v4_bridge()
    return await bridge.cancel_run(run_id=run_id, reason=_as_str(request.reason, "user_cancelled"))


@router.post("/runs/{run_id}/approvals/{approval_id}/resolve")
async def resolve_approval(run_id: str, approval_id: str, request: ApprovalResolveRequest) -> Dict[str, Any]:
    decision = _as_str(request.decision, "approved").strip().lower()
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="decision must be approved or rejected")
    bridge = get_runtime_v4_bridge()
    result = await bridge.resolve_approval(
        run_id=run_id,
        approval_id=approval_id,
        decision=decision,
        comment=_as_str(request.comment),
        confirmed=bool(request.confirmed),
        elevated=bool(request.elevated),
    )
    return result


@router.post("/runs/{run_id}/input")
async def submit_run_input(run_id: str, request: InputSubmitRequest) -> Dict[str, Any]:
    from api.ai import ensure_runtime_input_context_ready

    await ensure_runtime_input_context_ready(run_id)
    bridge = get_runtime_v4_bridge()
    return await bridge.submit_user_input(
        run_id=run_id,
        text=_as_str(request.text),
        source=_as_str(request.source, "user"),
    )


@router.post("/runs/{run_id}/actions/command")
async def execute_run_command_action(run_id: str, request: CommandActionRequest) -> Dict[str, Any]:
    bridge = get_runtime_v4_bridge()
    payload = request.model_dump()
    command_spec = _safe_dict(payload.get("command_spec"))
    if not command_spec:
        safe_command = _as_str(payload.get("command")).strip()
        if not safe_command:
            raise HTTPException(status_code=400, detail="command_spec is required")
        command_spec = _build_generic_exec_command_spec(
            command=safe_command,
            timeout_seconds=_as_int(payload.get("timeout_seconds"), 20),
            purpose=_as_str(payload.get("purpose")).strip(),
            title=_as_str(payload.get("title")).strip(),
            step_id=_as_str(payload.get("step_id")).strip(),
        )
    payload["command_spec"] = command_spec
    # Backward-compatible alias: keep one token field that runtime can consume as confirmation_ticket.
    if _as_str(payload.get("approval_token")).strip() and not _as_str(payload.get("confirmation_ticket")).strip():
        payload["confirmation_ticket"] = _as_str(payload.get("approval_token"))
    result = await bridge.execute_command(run_id=run_id, request_payload=payload)
    return result
