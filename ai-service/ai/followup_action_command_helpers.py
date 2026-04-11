"""
Helpers for follow-up action creation and command-context loading.
"""

import uuid
from typing import Any, Callable, Dict, Tuple

from fastapi import HTTPException


async def _load_followup_action_context(
    *,
    run_blocking: Callable[..., Any],
    session_store: Any,
    session_id: str,
    message_id: str,
    as_str: Callable[[Any, str], str],
) -> Tuple[Dict[str, Any], str]:
    payload = await run_blocking(session_store.get_session_with_messages, session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="session not found")
    session = payload.get("session") if isinstance(payload, dict) else {}

    target_message = await run_blocking(session_store.get_message_by_id, session_id, message_id)
    if not target_message:
        raise HTTPException(status_code=404, detail="message not found")

    message_role = as_str(
        target_message.get("role") if isinstance(target_message, dict) else getattr(target_message, "role", "")
    )
    message_content = as_str(
        target_message.get("content") if isinstance(target_message, dict) else getattr(target_message, "content", "")
    )
    if message_role != "assistant":
        raise HTTPException(status_code=400, detail="only assistant message can generate action")
    if not message_content:
        raise HTTPException(status_code=400, detail="assistant message content is empty")
    return session if isinstance(session, dict) else {}, message_content


def _build_followup_action_payload(
    *,
    message_id: str,
    draft: Dict[str, Any],
    utc_now_iso: Callable[[], str],
) -> Tuple[str, Dict[str, Any]]:
    action_id = f"act-{uuid.uuid4().hex[:12]}"
    action_payload = {
        "action_id": action_id,
        "message_id": message_id,
        "action": draft,
        "created_at": utc_now_iso(),
    }
    return action_id, action_payload


def _merge_followup_action_into_context(
    session: Dict[str, Any],
    action_payload: Dict[str, Any],
) -> Dict[str, Any]:
    session_context = session.get("context") if isinstance(session.get("context"), dict) else {}
    drafts = session_context.get("action_drafts") if isinstance(session_context.get("action_drafts"), list) else []
    drafts.append(action_payload)
    session_context["action_drafts"] = drafts[-50:]
    return session_context


async def _load_followup_command_message_context(
    *,
    run_blocking: Callable[..., Any],
    session_store: Any,
    session_id: str,
    message_id: str,
    as_str: Callable[[Any, str], str],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    payload = await run_blocking(session_store.get_session_with_messages, session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="session not found")
    session_payload = payload.get("session") if isinstance(payload, dict) else {}
    session_context: Dict[str, Any] = {}
    if isinstance(session_payload, dict):
        raw_context = session_payload.get("context")
        session_context = raw_context if isinstance(raw_context, dict) else {}
    else:
        raw_context = getattr(session_payload, "context", {})
        session_context = raw_context if isinstance(raw_context, dict) else {}

    target_message = await run_blocking(session_store.get_message_by_id, session_id, message_id)
    if not target_message:
        raise HTTPException(status_code=404, detail="message not found")

    message_role = as_str(
        target_message.get("role") if isinstance(target_message, dict) else getattr(target_message, "role", "")
    )
    message_content = as_str(
        target_message.get("content") if isinstance(target_message, dict) else getattr(target_message, "content", "")
    )
    message_metadata = (
        target_message.get("metadata") if isinstance(target_message, dict) else getattr(target_message, "metadata", {})
    )
    if not isinstance(message_metadata, dict):
        message_metadata = {}
    if message_role != "assistant":
        raise HTTPException(status_code=400, detail="only assistant message can execute command")
    if not message_content:
        raise HTTPException(status_code=400, detail="assistant message content is empty")
    return message_content, message_metadata, dict(session_context)
