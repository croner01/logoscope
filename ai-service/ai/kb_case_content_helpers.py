"""
Case content update helper functions.

Extracts request field checks and mutable-field application logic from `api/ai.py`.
"""

from typing import Any, Callable, Dict, List

from fastapi import HTTPException


def _require_editable_fields_for_case_content_update(
    request: Any,
) -> List[str]:
    requested_fields = _collect_requested_content_fields(request)
    if not requested_fields:
        raise HTTPException(
            status_code=400,
            detail={"code": "KBR-003", "message": "at least one editable field is required"},
        )
    return requested_fields


def _collect_requested_content_fields(request: Any) -> List[str]:
    requested_fields: List[str] = []
    if getattr(request, "problem_type", None) is not None:
        requested_fields.append("problem_type")
    if getattr(request, "severity", None) is not None:
        requested_fields.append("severity")
    if getattr(request, "summary", None) is not None:
        requested_fields.append("summary")
    if getattr(request, "service_name", None) is not None:
        requested_fields.append("service_name")
    if getattr(request, "root_causes", None) is not None:
        requested_fields.append("root_causes")
    if getattr(request, "solutions", None) is not None or getattr(request, "solutions_text", None) is not None:
        requested_fields.append("solutions")
    if getattr(request, "analysis_summary", None) is not None:
        requested_fields.append("analysis_summary")
    if getattr(request, "resolution", None) is not None:
        requested_fields.append("resolution")
    if getattr(request, "tags", None) is not None:
        requested_fields.append("tags")
    return requested_fields


def _apply_case_content_request_fields(
    case_obj: Any,
    request: Any,
    *,
    as_str: Callable[[Any, str], str],
    normalize_kb_draft_severity: Callable[[Any, str], str],
    truncate_text: Callable[[Any, int], str],
    normalize_string_list: Callable[[Any, int, int], List[str]],
    normalize_solutions_from_text: Callable[[Any], List[Dict[str, Any]]],
    normalize_solutions: Callable[[Any], List[Dict[str, Any]]],
) -> None:
    if request.problem_type is not None:
        case_obj.problem_type = as_str(request.problem_type, case_obj.problem_type).lower()
    if request.severity is not None:
        case_obj.severity = normalize_kb_draft_severity(request.severity, case_obj.severity or "medium")
    if request.summary is not None:
        case_obj.summary = truncate_text(as_str(request.summary), 1000)
    if request.service_name is not None:
        case_obj.service_name = truncate_text(as_str(request.service_name), 160)
    if request.root_causes is not None:
        case_obj.root_causes = normalize_string_list(request.root_causes, 12, 2)
    if request.solutions_text is not None:
        case_obj.solutions = normalize_solutions_from_text(request.solutions_text)
    elif request.solutions is not None:
        case_obj.solutions = normalize_solutions(request.solutions)
    if request.resolution is not None:
        case_obj.resolution = truncate_text(as_str(request.resolution), 2000)
    if request.tags is not None:
        case_obj.tags = normalize_string_list(request.tags, 20, 1)


def _validate_case_content_required_fields(case_obj: Any) -> None:
    if not case_obj.problem_type:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "problem_type must not be empty"})
    if not case_obj.summary:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "summary must not be empty"})
    if not case_obj.service_name:
        raise HTTPException(status_code=400, detail={"code": "KBR-002", "message": "service_name must not be empty"})
