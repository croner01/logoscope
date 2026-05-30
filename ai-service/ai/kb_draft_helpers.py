"""
KB draft helper functions for `/kb/from-analysis-session`.

Keep endpoint orchestration in `api/ai.py` while moving transformation and
decision logic here.
"""

import asyncio
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _require_kb_analysis_session_id(session_id: str) -> str:
    normalized = _as_str(session_id)
    if not normalized:
        raise HTTPException(status_code=400, detail={"code": "KBR-001", "message": "analysis_session_id is required"})
    return normalized


def _resolve_kb_draft_max_history_items() -> int:
    return max(80, int(_as_float(os.getenv("AI_KB_DRAFT_HISTORY_MAX_ITEMS", 240), 240)))


async def _load_kb_analysis_session_payload(
    *,
    session_store: Any,
    run_blocking: Callable[..., Any],
    session_id: str,
) -> Tuple[Dict[str, Any], List[Any]]:
    payload = await run_blocking(session_store.get_session_with_messages, session_id)
    if not payload:
        raise HTTPException(status_code=404, detail={"code": "KBR-004", "message": "analysis session not found"})
    session = payload.get("session") if isinstance(payload, dict) else {}
    messages = payload.get("messages") if isinstance(payload, dict) else []
    return (session if isinstance(session, dict) else {}), _as_list(messages)


def _build_kb_merged_history_messages(
    messages: List[Any],
    client_history_payload: Any,
    *,
    max_history_items: int,
    session_messages_to_history: Callable[..., List[Dict[str, Any]]],
    normalize_history: Callable[..., List[Dict[str, Any]]],
    mask_payload: Callable[[Any], Any],
    merge_history: Callable[..., List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    stored_history = session_messages_to_history(_as_list(messages), max_items=max_history_items)
    client_history = normalize_history(
        mask_payload(client_history_payload or []),
        max_items=max_history_items,
    )
    if client_history and stored_history:
        return merge_history(
            stored_history,
            client_history,
            max_items=max_history_items,
        )
    if client_history:
        return client_history
    return stored_history


async def _resolve_kb_draft_bundle(
    *,
    session: Dict[str, Any],
    merged_history_messages: List[Dict[str, Any]],
    include_followup: bool,
    llm_enabled: bool,
    llm_requested: bool,
    build_rule_based_kb_draft: Callable[..., Dict[str, Any]],
    build_kb_draft_quality: Callable[..., Tuple[List[str], float]],
    build_llm_kb_draft: Callable[..., Any],
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    draft_method = "rule-based"
    llm_fallback_reason = ""
    draft_case = build_rule_based_kb_draft(
        session=session,
        messages=merged_history_messages,
        include_followup=include_followup,
    )
    missing_required_fields, confidence = build_kb_draft_quality(draft_case)

    if llm_enabled and llm_requested:
        try:
            llm_draft_payload = await build_llm_kb_draft(
                session=session,
                messages=merged_history_messages,
                include_followup=include_followup,
                fallback_draft=draft_case,
            )
            llm_draft_case = llm_draft_payload.get("draft_case") if isinstance(llm_draft_payload, dict) else {}
            if isinstance(llm_draft_case, dict) and llm_draft_case:
                draft_case = llm_draft_case
                missing_required_fields, confidence = build_kb_draft_quality(
                    draft_case,
                    confidence_hint=_as_float(llm_draft_payload.get("confidence"), 0.88),
                )
                draft_method = "llm"
            else:
                llm_fallback_reason = "llm_empty_draft"
        except asyncio.TimeoutError:
            llm_fallback_reason = "llm_timeout"
        except ValueError:
            llm_fallback_reason = "llm_parse_error"
        except Exception as exc:
            if logger is not None:
                logger.warning(f"LLM kb draft generation failed, fallback to rule-based: {exc}")
            llm_fallback_reason = "llm_error"
    else:
        if llm_enabled and not llm_requested:
            llm_fallback_reason = "llm_disabled_by_user"
        elif not llm_enabled:
            llm_fallback_reason = "llm_unavailable"

    return {
        "draft_case": draft_case,
        "missing_required_fields": missing_required_fields,
        "confidence": confidence,
        "draft_method": draft_method,
        "llm_fallback_reason": llm_fallback_reason,
    }


def _resolve_kb_effective_save_mode(
    *,
    gateway: Any,
    remote_enabled: bool,
    save_mode: str,
) -> str:
    runtime_options = gateway.resolve_runtime_options(
        remote_enabled=bool(remote_enabled),
        retrieval_mode="local",
        save_mode=_as_str(save_mode, "local_only"),
    )
    return _as_str(runtime_options.get("effective_save_mode"), "local_only")


def _build_kb_from_analysis_response(
    *,
    draft_bundle: Dict[str, Any],
    save_mode_effective: str,
    llm_enabled: bool,
    llm_requested: bool,
) -> Dict[str, Any]:
    response = {
        "draft_case": draft_bundle.get("draft_case") if isinstance(draft_bundle.get("draft_case"), dict) else {},
        "missing_required_fields": _as_list(draft_bundle.get("missing_required_fields")),
        "confidence": _as_float(draft_bundle.get("confidence")),
        "save_mode_effective": save_mode_effective,
        "draft_method": _as_str(draft_bundle.get("draft_method"), "rule-based"),
        "llm_enabled": llm_enabled,
        "llm_requested": llm_requested,
    }
    llm_fallback_reason = _as_str(draft_bundle.get("llm_fallback_reason"))
    if llm_fallback_reason:
        response["llm_fallback_reason"] = llm_fallback_reason
    return response
