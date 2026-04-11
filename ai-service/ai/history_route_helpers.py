"""
AI history route helper functions.

Extracts request normalization and response shaping from `api/ai.py`.
"""

from typing import Any, Callable, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_history_list_request(
    *,
    limit: int,
    offset: int,
    sort_by: str,
    sort_order: str,
    allowed_sort_fields: set[str],
    allowed_sort_orders: set[str],
) -> Dict[str, Any]:
    safe_limit = max(1, int(limit))
    safe_offset = max(0, int(offset))
    safe_sort_by = (
        sort_by.strip().lower()
        if sort_by and sort_by.strip().lower() in allowed_sort_fields
        else "updated_at"
    )
    safe_sort_order = (
        sort_order.strip().lower()
        if sort_order and sort_order.strip().lower() in allowed_sort_orders
        else "desc"
    )
    return {
        "limit": safe_limit,
        "offset": safe_offset,
        "sort_by": safe_sort_by,
        "sort_order": safe_sort_order,
    }


def _build_history_list_items(
    sessions: List[Any],
    *,
    message_counts: Dict[str, Any],
    as_str: Callable[[Any, str], str],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for session in sessions:
        session_result = session.result if isinstance(session.result, dict) else {}
        normalized = session_result.get("raw") if isinstance(session_result.get("raw"), dict) else {}
        overview = normalized.get("overview") if isinstance(normalized, dict) else {}
        input_preview = as_str(getattr(session, "input_text", ""))[:120]
        session_id = as_str(getattr(session, "session_id", ""))
        try:
            message_count = int(message_counts.get(session_id, 0))
        except Exception:
            message_count = 0
        summary = as_str(
            (overview.get("description") if isinstance(overview, dict) else "")
            or session_result.get("summary")
            or input_preview
        )
        items.append(
            {
                "session_id": session_id,
                "analysis_type": session.analysis_type,
                "title": session.title,
                "service_name": session.service_name,
                "trace_id": session.trace_id,
                "summary": summary,
                "summary_text": as_str(session.summary_text, summary),
                "analysis_method": session.analysis_method,
                "llm_model": session.llm_model,
                "llm_provider": session.llm_provider,
                "source": session.source,
                "status": session.status,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "is_pinned": bool(session.is_pinned),
                "is_archived": bool(session.is_archived),
                "message_count": message_count,
            }
        )
    return items


def _build_history_list_response(
    *,
    items: List[Dict[str, Any]],
    total_all: Any,
    safe_limit: int,
    safe_offset: int,
    safe_sort_by: str,
    safe_sort_order: str,
    pinned_first: bool,
) -> Dict[str, Any]:
    try:
        total_all_int = int(total_all)
    except Exception:
        total_all_int = len(items)
    total_all_int = max(0, total_all_int)
    return {
        "sessions": items,
        "total": len(items),
        "total_all": total_all_int,
        "limit": safe_limit,
        "offset": safe_offset,
        "has_more": safe_offset + len(items) < total_all_int,
        "sort": {
            "sort_by": safe_sort_by,
            "sort_order": safe_sort_order,
            "pinned_first": pinned_first,
        },
    }


def _collect_history_session_update_changes(
    request: Any,
    *,
    existing_status: str,
    as_str: Callable[[Any, str], str],
) -> Dict[str, Any]:
    changes: Dict[str, Any] = {}
    if request.title is not None:
        changes["title"] = as_str(request.title)[:180]
    if request.is_pinned is not None:
        changes["is_pinned"] = bool(request.is_pinned)
    if request.is_archived is not None:
        changes["is_archived"] = bool(request.is_archived)
    if request.status is not None:
        changes["status"] = as_str(request.status, existing_status)
    return changes


def _build_history_session_update_noop(existing: Any) -> Dict[str, Any]:
    return {
        "status": "noop",
        "session_id": existing.session_id,
        "title": existing.title,
        "is_pinned": bool(existing.is_pinned),
        "is_archived": bool(existing.is_archived),
        "updated_at": existing.updated_at,
    }


def _build_history_session_update_response(updated: Any) -> Dict[str, Any]:
    return {
        "status": "ok",
        "session_id": updated.session_id,
        "title": updated.title,
        "is_pinned": bool(updated.is_pinned),
        "is_archived": bool(updated.is_archived),
        "state": updated.status,
        "updated_at": updated.updated_at,
    }


def _build_ai_history_detail_response(
    payload: Any,
    *,
    as_str: Callable[[Any, str], str],
    build_context_pills: Callable[[Dict[str, Any], str], List[Dict[str, str]]],
) -> Dict[str, Any]:
    session = payload.get("session") if isinstance(payload, dict) else {}
    messages = payload.get("messages") if isinstance(payload, dict) else []
    result_container = session.get("result") if isinstance(session, dict) else {}
    raw_result = result_container.get("raw") if isinstance(result_container, dict) else {}
    analysis_result = raw_result if isinstance(raw_result, dict) else {}

    session_id = as_str(session.get("session_id"))
    summary = as_str((result_container or {}).get("summary"))
    return {
        "session_id": session.get("session_id"),
        "analysis_type": session.get("analysis_type"),
        "title": session.get("title"),
        "service_name": session.get("service_name"),
        "trace_id": session.get("trace_id"),
        "input_text": session.get("input_text"),
        "context": session.get("context") if isinstance(session.get("context"), dict) else {},
        "result": analysis_result,
        "summary": summary,
        "summary_text": as_str(session.get("summary_text"), summary),
        "analysis_method": session.get("analysis_method"),
        "llm_model": session.get("llm_model"),
        "llm_provider": session.get("llm_provider"),
        "source": session.get("source"),
        "status": session.get("status"),
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "is_pinned": bool(session.get("is_pinned")),
        "is_archived": bool(session.get("is_archived")),
        "message_count": payload.get("message_count", len(messages)),
        "context_pills": build_context_pills(
            {
                "analysis_type": session.get("analysis_type"),
                "service_name": session.get("service_name"),
                "trace_id": session.get("trace_id"),
                "input_text": session.get("input_text"),
                "result": analysis_result,
            },
            session_id,
        ),
        "messages": [
            {
                "message_id": item.get("message_id"),
                "role": item.get("role"),
                "content": item.get("content"),
                "timestamp": item.get("created_at"),
                "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            }
            for item in messages
            if isinstance(item, dict)
        ],
    }
