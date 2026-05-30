"""
Temporal-like activities for runtime v4.

Activities are intentionally thin wrappers over the existing agent runtime API
functions to avoid duplicating security-critical execution logic.
"""

from __future__ import annotations

from typing import Any, Dict

try:
    from temporalio import activity as _temporal_activity
except Exception:  # pragma: no cover
    _temporal_activity = None


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _activity_defn(name: str):
    def _decorator(func):
        if _temporal_activity is None:
            return func
        return _temporal_activity.defn(name=name)(func)

    return _decorator


@_activity_defn("start_run_activity")
async def start_run_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    from api.ai import AIRunCreateRequest, _create_ai_run_impl

    request = AIRunCreateRequest(
        session_id=_as_str(payload.get("session_id")),
        question=_as_str(payload.get("question")),
        analysis_context=payload.get("analysis_context") if isinstance(payload.get("analysis_context"), dict) else {},
        runtime_options=payload.get("runtime_options") if isinstance(payload.get("runtime_options"), dict) else {},
    )
    return await _create_ai_run_impl(request)


@_activity_defn("resolve_approval_activity")
async def resolve_approval_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    from api.ai import AIRunApproveRequest, _approve_ai_run_impl

    request = AIRunApproveRequest(
        approval_id=_as_str(payload.get("approval_id")),
        decision=_as_str(payload.get("decision"), "approved"),
        comment=_as_str(payload.get("comment")),
        confirmed=bool(payload.get("confirmed", True)),
        elevated=bool(payload.get("elevated", False)),
    )
    return await _approve_ai_run_impl(_as_str(payload.get("run_id")), request)


@_activity_defn("submit_user_input_activity")
async def submit_user_input_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    from api.ai import AIRunInputRequest, _continue_ai_run_with_user_input_impl

    request = AIRunInputRequest(
        text=_as_str(payload.get("text")),
        source=_as_str(payload.get("source"), "user"),
    )
    return await _continue_ai_run_with_user_input_impl(_as_str(payload.get("run_id")), request)


@_activity_defn("interrupt_run_activity")
async def interrupt_run_activity(payload: Dict[str, Any]) -> Dict[str, Any]:
    from api.ai import _interrupt_ai_run_impl

    return await _interrupt_ai_run_impl(
        _as_str(payload.get("run_id")),
        reason=_as_str(payload.get("reason"), "user_interrupt_esc"),
    )
