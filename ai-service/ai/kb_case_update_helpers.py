"""
KB case update and manual-remediation helpers.

This module hosts reusable business logic extracted from `api/ai.py`.
"""

from typing import Any, Callable, Dict, List, Tuple

from fastapi import HTTPException


def _prepare_case_content_update_metadata(
    existing_case: Any,
    updated_case: Any,
    request: Any,
    *,
    as_str: Callable[[Any, str], str],
    as_float: Callable[[Any, float], float],
    truncate_text: Callable[[str, int], str],
    get_case_status: Callable[[Any], str],
    utc_now_iso: Callable[[], str],
) -> Tuple[Dict[str, Any], int, str]:
    existing_metadata = existing_case.llm_metadata if isinstance(existing_case.llm_metadata, dict) else {}
    previous_analysis_summary = as_str(existing_metadata.get("analysis_summary"), existing_case.summary)
    llm_metadata = updated_case.llm_metadata if isinstance(updated_case.llm_metadata, dict) else {}
    llm_metadata = dict(llm_metadata)
    knowledge_version = int(as_float(llm_metadata.get("knowledge_version", 1), 1)) + 1
    llm_metadata["knowledge_version"] = knowledge_version
    llm_metadata["last_editor"] = "manual_content"
    llm_metadata["analysis_summary"] = truncate_text(
        as_str(request.analysis_summary, as_str(llm_metadata.get("analysis_summary"), updated_case.summary)),
        1200,
    )
    if not as_str(llm_metadata.get("case_status")):
        llm_metadata["case_status"] = get_case_status(existing_case)
    updated_case.llm_metadata = llm_metadata
    updated_case.knowledge_version = knowledge_version
    updated_case.last_editor = "manual_content"
    updated_case.updated_at = utc_now_iso()
    updated_case.source = existing_case.source or "manual"
    return llm_metadata, knowledge_version, previous_analysis_summary


def _sync_case_update_with_remote(
    updated_case: Any,
    request: Any,
    *,
    gateway: Any,
    as_str: Callable[[Any, str], str],
    build_case_payload_for_remote: Callable[[Any], Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    runtime_options = gateway.resolve_runtime_options(
        remote_enabled=bool(request.remote_enabled),
        retrieval_mode="local",
        save_mode=as_str(request.save_mode, "local_only"),
    )
    effective_save_mode = as_str(runtime_options.get("effective_save_mode"), "local_only")
    remote_result = gateway.upsert_remote_with_outbox(
        build_case_payload_for_remote(updated_case),
        save_mode=effective_save_mode,
    )
    return effective_save_mode, remote_result


def _apply_remote_sync_result_to_case_metadata(
    llm_metadata: Dict[str, Any],
    remote_result: Dict[str, Any],
    *,
    as_str: Callable[[Any, str], str],
) -> None:
    llm_metadata["sync_status"] = as_str(remote_result.get("sync_status"), "not_requested")
    llm_metadata["external_doc_id"] = as_str(remote_result.get("external_doc_id"))
    llm_metadata["sync_error"] = as_str(remote_result.get("sync_error"))
    llm_metadata["sync_error_code"] = as_str(remote_result.get("sync_error_code"))


def _build_case_content_update_outcome(
    *,
    existing_case: Any,
    updated_case: Any,
    previous_analysis_summary: str,
    current_analysis_summary: str,
    requested_fields: List[str],
    knowledge_version: int,
    effective_save_mode: str,
    sync_status: str,
    sync_error_code: str,
    build_case_content_change_summary: Callable[..., Dict[str, Any]],
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
) -> Dict[str, Any]:
    change_summary = build_case_content_change_summary(
        existing_case=existing_case,
        updated_case=updated_case,
        previous_analysis_summary=previous_analysis_summary,
        current_analysis_summary=current_analysis_summary,
    )
    changed_fields = [str(field) for field in as_list(change_summary.get("changed_fields"))]
    unchanged_requested_fields = [field for field in requested_fields if field not in changed_fields]
    no_effective_change_reason = ""
    if not changed_fields and requested_fields:
        no_effective_change_reason = "submitted_values_equivalent_after_normalization"

    history_entry = {
        "event_type": "content_update",
        "version": knowledge_version,
        "updated_at": updated_case.updated_at,
        "editor": "manual_content",
        "changed_fields": changed_fields,
        "changes": change_summary.get("changes", {}),
        "requested_fields": requested_fields,
        "unchanged_requested_fields": unchanged_requested_fields,
        "no_effective_change_reason": no_effective_change_reason,
        "effective_save_mode": effective_save_mode,
        "sync_status": sync_status,
        "sync_error_code": sync_error_code,
        "source": "api:/ai/cases/update",
        "note": "manual_content_update_no_effective_change" if not changed_fields else "manual_content_update",
    }
    if changed_fields:
        changed_fields_text = "、".join(changed_fields)
        friendly_message = (
            f"知识库更新成功：版本 v{knowledge_version}，更新字段 {changed_fields_text}，"
            f"同步状态 {sync_status or 'unknown'}。"
        )
    else:
        requested_fields_text = "、".join(requested_fields) if requested_fields else "未识别"
        friendly_message = (
            f"知识库内容已校验：版本 v{knowledge_version}。本次提交字段 {requested_fields_text} "
            f"与当前内容等效（规范化后无差异），未产生有效字段变更；"
            f"同步状态 {sync_status or 'unknown'}。"
        )
    return {
        "changed_fields": changed_fields,
        "unchanged_requested_fields": unchanged_requested_fields,
        "no_effective_change_reason": no_effective_change_reason,
        "history_entry": history_entry,
        "friendly_message": friendly_message,
    }


def _build_case_content_update_response(
    *,
    updated_case: Any,
    knowledge_version: int,
    effective_save_mode: str,
    llm_metadata: Dict[str, Any],
    remote_result: Dict[str, Any],
    outcome: Dict[str, Any],
    requested_fields: List[str],
    persisted_history: Dict[str, Any],
    content_update_history_count: int,
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
) -> Dict[str, Any]:
    friendly_message = as_str(outcome.get("friendly_message"))
    return {
        "status": "ok",
        "case_id": updated_case.id,
        "knowledge_version": knowledge_version,
        "effective_save_mode": effective_save_mode,
        "sync_status": llm_metadata.get("sync_status"),
        "external_doc_id": llm_metadata.get("external_doc_id"),
        "sync_error": llm_metadata.get("sync_error"),
        "sync_error_code": llm_metadata.get("sync_error_code"),
        "outbox_id": as_str(remote_result.get("outbox_id")),
        "updated_at": updated_case.updated_at,
        "last_editor": llm_metadata.get("last_editor"),
        "analysis_summary": llm_metadata.get("analysis_summary"),
        "updated_fields": as_list(outcome.get("changed_fields")),
        "requested_fields": requested_fields,
        "unchanged_requested_fields": as_list(outcome.get("unchanged_requested_fields")),
        "no_effective_change_reason": as_str(outcome.get("no_effective_change_reason")),
        "history_entry": persisted_history,
        "content_update_history_count": content_update_history_count,
        "friendly_message": friendly_message,
        "message": friendly_message,
    }


def _validate_manual_remediation_request(
    request: Any,
    *,
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
) -> Tuple[List[str], str, str]:
    steps = [str(step).strip() for step in as_list(request.manual_remediation_steps) if as_str(step)]
    if len(steps) < 1:
        raise HTTPException(status_code=400, detail={"code": "KBR-003", "message": "manual_remediation_steps is required"})
    invalid_steps = [step for step in steps if len(step) < 5]
    if invalid_steps:
        raise HTTPException(
            status_code=400,
            detail={"code": "KBR-003", "message": "each manual_remediation_step length must be >= 5"},
        )

    notes = as_str(request.verification_notes)
    if len(notes) < 20:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "verification_notes length must be >= 20"})

    verification_result = as_str(request.verification_result).lower()
    if verification_result not in {"pass", "fail"}:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "verification_result must be pass or fail"})
    return steps, notes, verification_result


def _prepare_manual_remediation_case_update(
    existing_case: Any,
    request: Any,
    steps: List[str],
    notes: str,
    verification_result: str,
    case_model: Any,
    *,
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
    as_float: Callable[[Any, float], float],
    utc_now_iso: Callable[[], str],
) -> Tuple[Any, int, List[Dict[str, Any]]]:
    updated_case = case_model(**existing_case.to_dict())
    llm_metadata = updated_case.llm_metadata if isinstance(updated_case.llm_metadata, dict) else {}
    llm_metadata = dict(llm_metadata)
    history_records = [item for item in as_list(llm_metadata.get("remediation_history")) if isinstance(item, dict)]
    knowledge_version = int(as_float(llm_metadata.get("knowledge_version", 1), 1)) + 1
    llm_metadata["manual_remediation_steps"] = steps
    llm_metadata["verification_result"] = verification_result
    llm_metadata["verification_notes"] = notes
    llm_metadata["knowledge_version"] = knowledge_version
    llm_metadata["case_status"] = "resolved" if verification_result == "pass" else "archived"
    llm_metadata["last_editor"] = "manual"
    llm_metadata["analysis_summary"] = as_str(llm_metadata.get("analysis_summary"), updated_case.summary)
    updated_case.llm_metadata = llm_metadata
    updated_case.manual_remediation_steps = steps
    updated_case.verification_result = verification_result
    updated_case.verification_notes = notes
    updated_case.knowledge_version = knowledge_version
    updated_case.last_editor = "manual"
    if as_str(request.final_resolution):
        updated_case.resolution = as_str(request.final_resolution)
    updated_case.resolved = verification_result == "pass"
    if updated_case.resolved:
        updated_case.resolved_at = utc_now_iso()
    updated_case.updated_at = utc_now_iso()
    return updated_case, knowledge_version, history_records


def _sync_manual_remediation_update(
    updated_case: Any,
    request: Any,
    *,
    gateway: Any,
    as_str: Callable[[Any, str], str],
    build_case_payload_for_remote: Callable[[Any], Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    runtime_options = gateway.resolve_runtime_options(
        remote_enabled=bool(request.remote_enabled),
        retrieval_mode="local",
        save_mode=as_str(request.save_mode, "local_only"),
    )
    effective_save_mode = as_str(runtime_options.get("effective_save_mode"), "local_only")
    remote_result = gateway.upsert_remote_with_outbox(
        build_case_payload_for_remote(updated_case),
        save_mode=effective_save_mode,
    )
    return effective_save_mode, remote_result


def _apply_manual_remediation_sync_result(
    *,
    updated_case: Any,
    history_records: List[Dict[str, Any]],
    knowledge_version: int,
    steps: List[str],
    notes: str,
    verification_result: str,
    effective_save_mode: str,
    remote_result: Dict[str, Any],
    as_str: Callable[[Any, str], str],
) -> None:
    updated_case.llm_metadata["sync_status"] = as_str(remote_result.get("sync_status"), "not_requested")
    updated_case.llm_metadata["external_doc_id"] = as_str(remote_result.get("external_doc_id"))
    updated_case.llm_metadata["sync_error"] = as_str(remote_result.get("sync_error"))
    updated_case.llm_metadata["sync_error_code"] = as_str(remote_result.get("sync_error_code"))
    updated_case.llm_metadata["remediation_history"] = (
        history_records
        + [
            {
                "version": knowledge_version,
                "updated_at": updated_case.updated_at,
                "editor": "manual",
                "manual_remediation_steps": steps,
                "verification_result": verification_result,
                "verification_notes": notes,
                "final_resolution": updated_case.resolution,
                "sync_status": as_str(remote_result.get("sync_status"), "not_requested"),
                "sync_error_code": as_str(remote_result.get("sync_error_code")),
                "effective_save_mode": effective_save_mode,
            }
        ]
    )[-20:]


def _build_manual_remediation_change_summary(
    existing_case: Any,
    updated_case: Any,
    steps: List[str],
    notes: str,
    verification_result: str,
    *,
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
) -> Dict[str, Dict[str, Any]]:
    existing_metadata = existing_case.llm_metadata if isinstance(existing_case.llm_metadata, dict) else {}
    return {
        "manual_remediation_steps": {
            "before": as_list(existing_metadata.get("manual_remediation_steps")),
            "after": steps,
        },
        "verification_result": {
            "before": as_str(existing_metadata.get("verification_result")),
            "after": verification_result,
        },
        "verification_notes": {
            "before": as_str(existing_metadata.get("verification_notes")),
            "after": notes,
        },
        "final_resolution": {
            "before": as_str(existing_case.resolution),
            "after": as_str(updated_case.resolution),
        },
    }


def _append_manual_remediation_change_history(
    case_store: Any,
    updated_case: Any,
    knowledge_version: int,
    remediation_change_summary: Dict[str, Dict[str, Any]],
    effective_save_mode: str,
    *,
    append_case_change_history: Callable[[Any, str, Dict[str, Any]], Any],
) -> None:
    append_case_change_history(
        case_store,
        updated_case.id,
        {
            "event_type": "manual_remediation",
            "version": knowledge_version,
            "updated_at": updated_case.updated_at,
            "editor": "manual",
            "changed_fields": list(remediation_change_summary.keys()),
            "changes": remediation_change_summary,
            "effective_save_mode": effective_save_mode,
            "sync_status": updated_case.llm_metadata.get("sync_status"),
            "sync_error_code": updated_case.llm_metadata.get("sync_error_code"),
            "source": "api:/ai/cases/manual-remediation",
            "note": "manual_remediation_update",
        },
    )


def _build_manual_remediation_response(
    updated_case: Any,
    knowledge_version: int,
    effective_save_mode: str,
    remote_result: Dict[str, Any],
    *,
    as_list: Callable[[Any], List[Any]],
    as_str: Callable[[Any, str], str],
) -> Dict[str, Any]:
    return {
        "status": "ok",
        "case_id": updated_case.id,
        "knowledge_version": knowledge_version,
        "sync_status": updated_case.llm_metadata.get("sync_status"),
        "sync_error_code": as_str(updated_case.llm_metadata.get("sync_error_code")),
        "effective_save_mode": effective_save_mode,
        "outbox_id": as_str(remote_result.get("outbox_id")),
        "remediation_history_count": len(as_list(updated_case.llm_metadata.get("remediation_history"))),
        "message": "manual remediation updated",
    }
