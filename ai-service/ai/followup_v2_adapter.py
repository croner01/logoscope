"""
Follow-up v2 event adapter.

This module replaces the legacy `ai.agent_v2` compatibility package while
keeping the same event contract for `/api/ai/v2/follow-up*`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional


_SUPPORTED_EVENT_TYPES = {
    "plan_started",
    "plan_updated",
    "thought_delta",
    "answer_delta",
    "action_proposed",
    "command_precheck_result",
    "approval_required",
    "command_observation",
    "replan",
    "final",
    "error",
}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    lowered = _as_str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return int(default)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_state(current: str, event_name: str) -> str:
    state = _as_str(current, "idle").strip().lower() or "idle"
    event = _as_str(event_name).strip().lower()
    if event in {"token"}:
        return "answering"
    if event in {"action"}:
        return "acting"
    if event in {"observation"}:
        return "observing"
    if event in {"replan"}:
        return "replanning"
    if event in {"final"}:
        return "final"
    if event in {"error"}:
        return "error"
    if event in {"plan", "thought"} and state in {"idle", "planning"}:
        return "planning"
    return state


def _normalize_v2_event(event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    safe_type = _as_str(event_type, "plan_updated").strip().lower()
    if safe_type not in _SUPPORTED_EVENT_TYPES:
        safe_type = "plan_updated"
    safe_payload = payload if isinstance(payload, dict) else {}
    return {
        "type": safe_type,
        "payload": safe_payload,
    }


def _normalize_command_action(raw_action: Dict[str, Any]) -> Dict[str, Any]:
    safe = raw_action if isinstance(raw_action, dict) else {}
    return {
        "id": _as_str(safe.get("id")),
        "priority": max(1, _as_int(safe.get("priority"), 1)),
        "title": _as_str(safe.get("title")),
        "purpose": _as_str(safe.get("purpose")),
        "command": _as_str(safe.get("command")),
        "command_type": _as_str(safe.get("command_type"), "unknown"),
        "risk_level": _as_str(safe.get("risk_level"), "high"),
        "executable": _as_bool(safe.get("executable"), False),
        "requires_confirmation": _as_bool(safe.get("requires_confirmation"), True),
        "requires_write_permission": _as_bool(safe.get("requires_write_permission"), False),
        "requires_elevation": _as_bool(safe.get("requires_elevation"), False),
        "reason": _as_str(safe.get("reason")),
        "source": _as_str(safe.get("source")) or None,
    }


async def run_followup_v2_adapter(
    *,
    request: Any,
    run_followup_core: Callable[..., Awaitable[Dict[str, Any]]],
    emit_v2_event: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
    precheck_command: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Adapt legacy follow-up core events to v2 stream contract."""
    runtime_state = "idle"
    thought_count = 0
    token_count = 0
    action_count = 0
    precheck_count = 0
    approval_required_count = 0
    assistant_message_id = ""

    async def _emit(event_type: str, payload: Dict[str, Any]) -> None:
        if not callable(emit_v2_event):
            return
        normalized = _normalize_v2_event(event_type, payload if isinstance(payload, dict) else {})
        await emit_v2_event(normalized["type"], normalized["payload"])

    async def _on_legacy_event(event_name: str, payload: Dict[str, Any]) -> None:
        nonlocal runtime_state
        nonlocal thought_count
        nonlocal token_count
        nonlocal action_count
        nonlocal precheck_count
        nonlocal approval_required_count
        nonlocal assistant_message_id

        safe_payload = payload if isinstance(payload, dict) else {}
        event = _as_str(event_name).strip().lower()
        runtime_state = _normalize_state(runtime_state, event)

        if event == "plan":
            stage = _as_str(safe_payload.get("stage"))
            mapped_type = "plan_started" if stage in {"session_prepare", "history_load"} else "plan_updated"
            await _emit(
                mapped_type,
                {
                    "state": runtime_state,
                    "stage": stage,
                    "detail": safe_payload,
                },
            )
            return

        if event == "thought":
            thought_count += 1
            await _emit(
                "thought_delta",
                {
                    "state": runtime_state,
                    "index": thought_count,
                    "phase": _as_str(safe_payload.get("phase"), "thought"),
                    "status": _as_str(safe_payload.get("status"), "info"),
                    "title": _as_str(safe_payload.get("title")),
                    "detail": _as_str(safe_payload.get("detail")),
                    "timestamp": _as_str(safe_payload.get("timestamp")),
                },
            )
            return

        if event == "token":
            token_count += 1
            await _emit(
                "answer_delta",
                {
                    "state": runtime_state,
                    "index": token_count,
                    "text": _as_str(safe_payload.get("text")),
                },
            )
            return

        if event == "action":
            payload_message_id = _as_str(safe_payload.get("message_id"))
            if payload_message_id:
                assistant_message_id = payload_message_id
            parsed_actions: List[Dict[str, Any]] = []
            for raw_action in _as_list(safe_payload.get("actions")):
                if not isinstance(raw_action, dict):
                    continue
                parsed_actions.append(_normalize_command_action(raw_action))

            if callable(precheck_command):
                for action in parsed_actions:
                    command = _as_str(action.get("command"))
                    if not command:
                        continue
                    try:
                        precheck = await precheck_command(
                            session_id=_as_str(getattr(request, "analysis_session_id", "")),
                            message_id=assistant_message_id,
                            action_id=_as_str(action.get("id")),
                            command=command,
                        )
                    except Exception as exc:
                        precheck = {
                            "status": "permission_required",
                            "message": f"precheck unavailable: {exc}",
                            "requires_confirmation": False,
                            "requires_write_permission": False,
                            "requires_elevation": False,
                        }
                    precheck_count += 1
                    precheck_status = _as_str(precheck.get("status"), "permission_required").lower()
                    precheck_type = _as_str(precheck.get("command_type"), _as_str(action.get("command_type"), "unknown")).lower()
                    precheck_risk = _as_str(precheck.get("risk_level"), _as_str(action.get("risk_level"), "high")).lower()
                    precheck_requires_write = _as_bool(precheck.get("requires_write_permission"), False)
                    precheck_requires_elevation = _as_bool(precheck.get("requires_elevation"), False)
                    precheck_requires_confirmation = _as_bool(precheck.get("requires_confirmation"), False)

                    action["command_type"] = precheck_type or "unknown"
                    action["risk_level"] = precheck_risk or "high"
                    action["requires_write_permission"] = precheck_requires_write
                    action["requires_elevation"] = precheck_requires_elevation
                    action["requires_confirmation"] = precheck_requires_confirmation
                    action["executable"] = bool(command) and precheck_status != "permission_required"

                    await _emit(
                        "command_precheck_result",
                        {
                            "state": runtime_state,
                            "message_id": assistant_message_id,
                            "action_id": _as_str(action.get("id")),
                            "command": command,
                            "command_type": precheck_type or "unknown",
                            "result": precheck if isinstance(precheck, dict) else {},
                        },
                    )

                    if precheck_status in {"elevation_required", "confirmation_required"}:
                        approval_required_count += 1
                        await _emit(
                            "approval_required",
                            {
                                "state": runtime_state,
                                "message_id": assistant_message_id,
                                "action_id": _as_str(action.get("id")),
                                "command": command,
                                "command_type": precheck_type or "unknown",
                                "precheck": precheck if isinstance(precheck, dict) else {},
                            },
                        )

            action_count += len(parsed_actions)
            await _emit(
                "action_proposed",
                {
                    "state": runtime_state,
                    "message_id": assistant_message_id,
                    "count": len(parsed_actions),
                    "actions": parsed_actions,
                },
            )
            return

        if event == "observation":
            await _emit(
                "command_observation",
                {
                    "state": runtime_state,
                    "observation": safe_payload,
                },
            )
            return

        if event == "replan":
            await _emit(
                "replan",
                {
                    "state": runtime_state,
                    "detail": safe_payload,
                },
            )
            return

    await _emit(
        "plan_started",
        {
            "state": runtime_state,
            "stage": "agent_v2_bootstrap",
        },
    )

    result = await run_followup_core(
        request,
        event_callback=_on_legacy_event,
    )
    safe_result = result if isinstance(result, dict) else {}
    history = _as_list(safe_result.get("history"))
    for message in reversed(history):
        if not isinstance(message, dict):
            continue
        if _as_str(message.get("role")).lower() != "assistant":
            continue
        mid = _as_str(message.get("message_id"))
        if mid:
            assistant_message_id = mid
            break
    runtime_state = _normalize_state(runtime_state, "final")
    safe_result["agent_v2"] = {
        "enabled": True,
        "state": runtime_state,
        "metrics": {
            "thought_events": thought_count,
            "token_events": token_count,
            "proposed_actions": action_count,
            "precheck_calls": precheck_count,
            "approval_required": approval_required_count,
        },
    }
    await _emit(
        "final",
        {
            "state": runtime_state,
            "result": safe_result,
        },
    )
    return safe_result

