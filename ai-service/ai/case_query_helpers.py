"""
Case query/detail response helpers.
"""

from typing import Any, Callable, Dict, List, Tuple


def _build_case_list_items(
    cases: List[Any],
    *,
    case_store: Any,
    case_store_count_change_history: Callable[..., int],
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
    as_float: Callable[[Any, float], float],
    get_case_status: Callable[[Any], str],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for case_obj in cases:
        content_history_count = case_store_count_change_history(
            case_store,
            case_obj.id,
            event_type="content_update",
        )
        if content_history_count <= 0:
            content_history_count = len(as_list((case_obj.llm_metadata or {}).get("content_update_history")))
        items.append(
            {
                "id": case_obj.id,
                "problem_type": case_obj.problem_type,
                "severity": case_obj.severity,
                "summary": case_obj.summary,
                "service_name": case_obj.service_name,
                "resolved": case_obj.resolved,
                "resolution": case_obj.resolution,
                "tags": case_obj.tags,
                "created_at": case_obj.created_at,
                "updated_at": case_obj.updated_at,
                "resolved_at": case_obj.resolved_at,
                "source": case_obj.source,
                "llm_provider": case_obj.llm_provider,
                "llm_model": case_obj.llm_model,
                "case_status": get_case_status(case_obj),
                "knowledge_version": int(as_float((case_obj.llm_metadata or {}).get("knowledge_version"), 1)),
                "verification_result": as_str((case_obj.llm_metadata or {}).get("verification_result")),
                "verification_notes": as_str((case_obj.llm_metadata or {}).get("verification_notes")),
                "manual_remediation_steps": as_list((case_obj.llm_metadata or {}).get("manual_remediation_steps")),
                "sync_status": as_str((case_obj.llm_metadata or {}).get("sync_status")),
                "external_doc_id": as_str((case_obj.llm_metadata or {}).get("external_doc_id")),
                "sync_error": as_str((case_obj.llm_metadata or {}).get("sync_error")),
                "sync_error_code": as_str((case_obj.llm_metadata or {}).get("sync_error_code")),
                "last_editor": as_str((case_obj.llm_metadata or {}).get("last_editor")),
                "remediation_history": as_list((case_obj.llm_metadata or {}).get("remediation_history")),
                "content_update_history_count": content_history_count,
            }
        )
    return items


def _resolve_case_detail_content_history(
    *,
    case_store: Any,
    case_obj: Any,
    case_store_list_change_history: Callable[..., List[Dict[str, Any]]],
    case_store_count_change_history: Callable[..., int],
    as_list: Callable[[Any], List[Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    content_history = case_store_list_change_history(
        case_store,
        case_id=case_obj.id,
        limit=120,
        event_type="content_update",
    )
    llm_metadata = case_obj.llm_metadata if isinstance(case_obj.llm_metadata, dict) else {}
    if not content_history:
        content_history = as_list(llm_metadata.get("content_update_history"))
    content_history_count = case_store_count_change_history(
        case_store,
        case_id=case_obj.id,
        event_type="content_update",
    )
    if content_history_count <= 0:
        content_history_count = len(as_list(content_history))
    return as_list(content_history), int(content_history_count)


def _build_case_detail_payload(
    case_obj: Any,
    *,
    content_history: List[Dict[str, Any]],
    content_history_count: int,
    get_case_status: Callable[[Any], str],
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
    as_float: Callable[[Any, float], float],
    build_case_analysis_result: Callable[[Any], Dict[str, Any]],
) -> Dict[str, Any]:
    llm_metadata = case_obj.llm_metadata if isinstance(case_obj.llm_metadata, dict) else {}
    return {
        "id": case_obj.id,
        "problem_type": case_obj.problem_type,
        "severity": case_obj.severity,
        "summary": case_obj.summary,
        "log_content": case_obj.log_content,
        "service_name": case_obj.service_name,
        "root_causes": case_obj.root_causes,
        "solutions": case_obj.solutions,
        "context": case_obj.context,
        "resolved": case_obj.resolved,
        "resolution": case_obj.resolution,
        "tags": case_obj.tags,
        "created_at": case_obj.created_at,
        "updated_at": case_obj.updated_at,
        "resolved_at": case_obj.resolved_at,
        "llm_provider": case_obj.llm_provider,
        "llm_model": case_obj.llm_model,
        "llm_metadata": case_obj.llm_metadata,
        "source": case_obj.source,
        "case_status": get_case_status(case_obj),
        "knowledge_version": int(as_float(llm_metadata.get("knowledge_version"), 1)),
        "manual_remediation_steps": as_list(llm_metadata.get("manual_remediation_steps")),
        "verification_result": as_str(llm_metadata.get("verification_result")),
        "verification_notes": as_str(llm_metadata.get("verification_notes")),
        "analysis_summary": as_str(llm_metadata.get("analysis_summary"), case_obj.summary),
        "sync_status": as_str(llm_metadata.get("sync_status")),
        "external_doc_id": as_str(llm_metadata.get("external_doc_id")),
        "sync_error": as_str(llm_metadata.get("sync_error")),
        "sync_error_code": as_str(llm_metadata.get("sync_error_code")),
        "last_editor": as_str(llm_metadata.get("last_editor")),
        "remediation_history": as_list(llm_metadata.get("remediation_history")),
        "content_update_history": as_list(content_history),
        "content_update_history_count": int(content_history_count),
        "analysis_result": build_case_analysis_result(case_obj),
    }
