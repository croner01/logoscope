"""
KB runtime/search route helpers.

Keep request normalization and response shaping out of api routes so
`api/ai.py` stays thin.
"""

from typing import Any, Dict, Tuple

from fastapi import HTTPException


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _resolve_kb_runtime_options_payload(
    gateway: Any,
    *,
    remote_enabled: bool,
    retrieval_mode: str,
    save_mode: str,
) -> Any:
    return gateway.resolve_runtime_options(
        remote_enabled=bool(remote_enabled),
        retrieval_mode=_as_str(retrieval_mode, "local"),
        save_mode=_as_str(save_mode, "local_only"),
    )


def _build_kb_runtime_warning_detail(resolved: Dict[str, Any], code: str, default_message: str) -> Dict[str, Any]:
    return {
        "code": code,
        "message": _as_str(resolved.get("message"), default_message),
        "effective_retrieval_mode": _as_str(resolved.get("effective_retrieval_mode"), "local"),
        "effective_save_mode": _as_str(resolved.get("effective_save_mode"), "local_only"),
    }


def _raise_for_kb_runtime_warning(resolved: Any) -> None:
    warning_code = _as_str(resolved.get("warning_code"))
    if warning_code == "KBR-006":
        raise HTTPException(
            status_code=409,
            detail=_build_kb_runtime_warning_detail(
                resolved,
                code="KBR-006",
                default_message="remote provider not configured",
            ),
        )
    if warning_code == "KBR-007":
        raise HTTPException(
            status_code=503,
            detail=_build_kb_runtime_warning_detail(
                resolved,
                code="KBR-007",
                default_message="remote provider unavailable",
            ),
        )


def _require_kb_search_query(query: str) -> str:
    normalized_query = _as_str(query)
    if len(normalized_query) < 3:
        raise HTTPException(status_code=400, detail={"code": "KBR-001", "message": "query length must be >= 3"})
    return normalized_query


def _normalize_kb_search_retrieval_mode(value: Any) -> str:
    return _as_str(value, "local")


def _normalize_kb_search_top_k(value: Any) -> int:
    return max(1, min(int(value or 5), 20))


def _build_kb_search_request_context(request: Any) -> Dict[str, Any]:
    retrieval_mode = _normalize_kb_search_retrieval_mode(getattr(request, "retrieval_mode", "local"))
    return {
        "retrieval_mode": retrieval_mode,
        "service_name": _as_str(getattr(request, "service_name", "")),
        "problem_type": _as_str(getattr(request, "problem_type", "")),
        "top_k": _normalize_kb_search_top_k(getattr(request, "top_k", 5)),
        "include_draft": bool(getattr(request, "include_draft", False)),
    }


def _resolve_kb_search_effective_mode(gateway: Any, retrieval_mode: str) -> Tuple[Any, str]:
    runtime_options = gateway.resolve_runtime_options(
        remote_enabled=retrieval_mode in {"hybrid", "remote_only"},
        retrieval_mode=retrieval_mode,
        save_mode="local_only",
    )
    effective_mode = _as_str(runtime_options.get("effective_retrieval_mode"), "local")
    return runtime_options, effective_mode


def _execute_kb_search(
    gateway: Any,
    request_context: Dict[str, Any],
    *,
    query: str,
    effective_mode: str,
) -> Dict[str, Any]:
    return gateway.search(
        query=query,
        service_name=_as_str(request_context.get("service_name")),
        problem_type=_as_str(request_context.get("problem_type")),
        top_k=int(request_context.get("top_k") or 5),
        retrieval_mode=effective_mode,
        include_draft=bool(request_context.get("include_draft")),
    )


def _build_kb_search_response(
    payload: Any,
    *,
    effective_mode: str,
    runtime_options: Any,
) -> Any:
    payload["effective_mode"] = effective_mode
    payload["message"] = _as_str(payload.get("warning_message") or runtime_options.get("message"))
    payload["warning_code"] = _as_str(payload.get("warning_code"))
    return payload
