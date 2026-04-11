"""
Follow-up persistence helpers.

Extracted from `api/ai.py` to reduce route-file responsibilities.
"""

from typing import Any, Callable, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


async def _load_messages_for_history(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    session_id: str,
    limit: int,
) -> List[Any]:
    """优先读取轻量消息，避免历史回读携带大 metadata_json。"""
    light_getter = getattr(session_store, "get_messages_light", None)
    if callable(light_getter):
        return await run_blocking(light_getter, session_id, limit)
    return await run_blocking(session_store.get_messages, session_id, limit)


async def _persist_followup_messages_and_history(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    analysis_session_id: str,
    history: List[Dict[str, Any]],
    conversation_id: str,
    user_message: Dict[str, Any],
    persist_user_message: bool,
    assistant_message: Dict[str, Any],
    trim_conversation_history: Callable[..., List[Dict[str, Any]]],
    set_conversation_history: Callable[[str, List[Dict[str, Any]]], None],
) -> List[Dict[str, Any]]:
    history.append(assistant_message)
    history = trim_conversation_history(history)
    set_conversation_history(
        conversation_id,
        trim_conversation_history(
            [
                {
                    "message_id": item.get("message_id"),
                    "role": item.get("role"),
                    "content": item.get("content"),
                    "timestamp": item.get("timestamp"),
                }
                for item in history
            ],
            max_items=40,
        ),
    )

    messages_to_persist: List[Dict[str, Any]] = []
    if persist_user_message and _as_str(user_message.get("content")):
        messages_to_persist.append(user_message)
    messages_to_persist.append(assistant_message)

    persisted_messages = await run_blocking(
        session_store.append_messages,
        analysis_session_id,
        messages_to_persist,
    )
    response_history = history
    if persisted_messages:
        stored_messages = await _load_messages_for_history(
            session_store=session_store,
            run_blocking=run_blocking,
            session_id=analysis_session_id,
            limit=200,
        )
        response_history = trim_conversation_history(
            [
                {
                    "message_id": _as_str(msg.message_id),
                    "role": _as_str(msg.role),
                    "content": _as_str(msg.content),
                    "timestamp": _as_str(msg.created_at),
                    "metadata": msg.metadata if isinstance(msg.metadata, dict) else {},
                }
                for msg in stored_messages
                if _as_str(msg.role) in {"user", "assistant"}
            ],
            max_items=40,
        )
    return response_history


async def _update_followup_session_summary(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    analysis_session_id: str,
    analysis_context: Dict[str, Any],
    analysis_method: str,
    llm_provider: str,
) -> None:
    summary_text = _as_str(
        (analysis_context.get("result") or {}).get("overview", {}).get("description")
        if isinstance((analysis_context.get("result") or {}).get("overview"), dict)
        else analysis_context.get("input_text")
    )[:300]
    current_session = await run_blocking(session_store.get_session, analysis_session_id)
    fallback_title = _as_str(analysis_context.get("title")) or _as_str(analysis_context.get("service_name"), "AI Session")
    updated_title = _as_str(getattr(current_session, "title", "")) or fallback_title
    await run_blocking(
        session_store.update_session,
        analysis_session_id,
        analysis_method=analysis_method,
        llm_provider=_as_str(llm_provider),
        llm_model=_as_str((analysis_context.get("llm_info") or {}).get("model")),
        summary_text=summary_text,
        title=updated_title,
        status="completed",
    )
