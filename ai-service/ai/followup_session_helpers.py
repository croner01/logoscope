"""
Follow-up session/history helper functions.

Extracted from `api/ai.py` to reduce orchestration file size.
"""

import uuid
from typing import Any, Callable, Dict, List, Tuple


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _to_message_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            dumped = item.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            return {}
    return {}


def _message_field(item: Any, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _normalize_history_text(value: Any) -> str:
    return " ".join(_as_str(value).split()).strip()


async def _load_messages_for_history(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    session_id: str,
    limit: int,
) -> List[Any]:
    """优先走轻量查询，避免追问热路径回读超大 metadata_json。"""
    light_getter = getattr(session_store, "get_messages_light", None)
    if callable(light_getter):
        light_rows = await run_blocking(light_getter, session_id, limit)
        if isinstance(light_rows, list):
            return light_rows
    full_rows = await run_blocking(session_store.get_messages, session_id, limit)
    return _as_list(full_rows)


def _build_followup_session_seed(
    analysis_context: Dict[str, Any],
    question: str,
    *,
    extract_overview_summary: Callable[[Dict[str, Any]], str],
    llm_provider: str,
) -> Dict[str, Any]:
    result_payload = analysis_context.get("result")
    normalized_result = result_payload if isinstance(result_payload, dict) else {}
    return {
        "analysis_type": _as_str(analysis_context.get("analysis_type"), "log"),
        "service_name": _as_str(analysis_context.get("service_name")),
        "input_text": _as_str(analysis_context.get("input_text"), question),
        "trace_id": _as_str(analysis_context.get("trace_id")),
        "context": analysis_context,
        "result": {
            "summary": extract_overview_summary(normalized_result),
            "raw": normalized_result,
        },
        "analysis_method": _as_str((analysis_context.get("llm_info") or {}).get("method")),
        "llm_model": _as_str((analysis_context.get("llm_info") or {}).get("model")),
        "llm_provider": _as_str(llm_provider),
    }


async def _ensure_followup_analysis_session(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    analysis_session_id: str,
    analysis_context: Dict[str, Any],
    question: str,
    extract_overview_summary: Callable[[Dict[str, Any]], str],
    llm_provider: str,
) -> str:
    session_seed = _build_followup_session_seed(
        analysis_context,
        question,
        extract_overview_summary=extract_overview_summary,
        llm_provider=llm_provider,
    )
    if not analysis_session_id:
        created = await run_blocking(
            session_store.create_session,
            source="api:/follow-up:init",
            **session_seed,
        )
        return _as_str(getattr(created, "session_id", ""))

    existing = await run_blocking(session_store.get_session, analysis_session_id)
    if existing:
        return analysis_session_id

    await run_blocking(
        session_store.create_session,
        source="api:/follow-up:recover",
        session_id=analysis_session_id,
        **session_seed,
    )
    return analysis_session_id


async def _seed_followup_runtime_history_session(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    analysis_session_id: str,
    analysis_context: Dict[str, Any],
    question: str,
    user_message_id: str,
    conversation_id: str,
    extract_overview_summary: Callable[[Dict[str, Any]], str],
    llm_provider: str,
    utc_now_iso: Callable[[], str],
) -> str:
    seeded_session_id = await _ensure_followup_analysis_session(
        session_store=session_store,
        run_blocking=run_blocking,
        analysis_session_id=analysis_session_id,
        analysis_context=analysis_context,
        question=question,
        extract_overview_summary=extract_overview_summary,
        llm_provider=llm_provider,
    )

    existing = await run_blocking(session_store.get_session, seeded_session_id)
    existing_context = {}
    if existing is not None and isinstance(getattr(existing, "context", None), dict):
        existing_context = dict(getattr(existing, "context"))
    merged_context = {**existing_context, **(analysis_context or {})}
    if conversation_id and not _as_str(merged_context.get("conversation_id")):
        merged_context["conversation_id"] = conversation_id

    await run_blocking(
        session_store.update_session,
        seeded_session_id,
        status="running",
        source="api:/follow-up:runtime-init",
        context=merged_context,
    )

    existing_messages = await _load_messages_for_history(
        session_store=session_store,
        run_blocking=run_blocking,
        session_id=seeded_session_id,
        limit=200,
    )
    normalized_user_message_id = _as_str(user_message_id).strip()
    if normalized_user_message_id and any(
        _as_str(_message_field(item, "message_id")).strip() == normalized_user_message_id
        for item in existing_messages
    ):
        return seeded_session_id

    safe_question = _as_str(question).strip()
    if not safe_question:
        return seeded_session_id
    normalized_question = _normalize_history_text(safe_question)
    if existing_messages:
        last_item = existing_messages[-1]
        last_role = _as_str(_message_field(last_item, "role")).strip().lower()
        last_content = _normalize_history_text(_message_field(last_item, "content"))
        if last_role == "user" and last_content and last_content == normalized_question:
            return seeded_session_id

    await run_blocking(
        session_store.append_messages,
        seeded_session_id,
        [
            {
                "message_id": normalized_user_message_id or f"msg-{uuid.uuid4().hex[:12]}",
                "role": "user",
                "content": safe_question,
                "timestamp": utc_now_iso(),
                "metadata": {
                    "kind": "follow_up_question",
                    "conversation_id": conversation_id,
                    "seeded_by": "ai_runtime_create_run",
                },
            }
        ],
    )
    return seeded_session_id


async def _build_followup_history(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    analysis_session_id: str,
    request_history: List[Any],
    conversation_id: str,
    normalize_conversation_history: Callable[..., List[Dict[str, Any]]],
    mask_sensitive_payload: Callable[[Any], Any],
    get_conversation_history: Callable[[str], List[Dict[str, Any]]],
    session_messages_to_conversation_history: Callable[..., List[Dict[str, Any]]],
    merge_conversation_history: Callable[..., List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    client_history = normalize_conversation_history(
        [mask_sensitive_payload(_to_message_dict(msg)) for msg in request_history],
        max_items=40,
    )
    server_history = get_conversation_history(conversation_id)
    stored_history: List[Dict[str, Any]] = []
    if analysis_session_id and (not server_history or bool(client_history)):
        stored_messages = await _load_messages_for_history(
            session_store=session_store,
            run_blocking=run_blocking,
            session_id=analysis_session_id,
            limit=200,
        )
        stored_history = session_messages_to_conversation_history(stored_messages, max_items=40)

    history = client_history or server_history
    if client_history and stored_history:
        history = merge_conversation_history(stored_history, client_history, max_items=40)
    elif not history:
        history = stored_history
    return history


def _upsert_followup_user_message(
    history: List[Dict[str, Any]],
    safe_question: str,
    *,
    trim_conversation_history: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    utc_now_iso: Callable[[], str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
    last_item = history[-1] if history else {}
    if (
        isinstance(last_item, dict)
        and _as_str(last_item.get("role")).lower() == "user"
        and _normalize_history_text(last_item.get("content")) == _normalize_history_text(safe_question)
    ):
        user_message = {
            "message_id": _as_str(last_item.get("message_id")).strip() or f"msg-{uuid.uuid4().hex[:12]}",
            "role": "user",
            "content": _as_str(last_item.get("content")).strip() or safe_question,
            "timestamp": _as_str(last_item.get("timestamp")) or utc_now_iso(),
            "metadata": {"kind": "follow_up_question"},
        }
        return trim_conversation_history(history), user_message, False
    else:
        user_message = {
            "message_id": f"msg-{uuid.uuid4().hex[:12]}",
            "role": "user",
            "content": safe_question,
            "timestamp": utc_now_iso(),
            "metadata": {"kind": "follow_up_question"},
        }
        history.append(
            {
                "message_id": user_message["message_id"],
                "role": "user",
                "content": safe_question,
                "timestamp": user_message["timestamp"],
            }
        )
    return trim_conversation_history(history), user_message, True
