"""
LangChain follow-up 只读工具集（P0）。
"""

import json
import os
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _as_str(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value)))
    except Exception:
        return default


def _normalize_text(text: str) -> str:
    return _as_str(text).lower()


def _build_message_excerpt(message: str, level: str, max_len: int = 260) -> str:
    """构建日志摘要，优先保留 traceback 关键信息。"""
    text = _as_str(message)
    if not text:
        return ""
    lowered = text.lower()
    traceback_like = "traceback" in lowered or "exception" in lowered or "error" in lowered
    if traceback_like or level == "ERROR":
        lines = [line for line in text.splitlines() if _as_str(line)]
        if lines:
            excerpt = "\n".join(lines[:8])
            return excerpt[:max(600, max_len)]
    return text[:max_len]


def _is_high_signal_event(level: str, message: str) -> bool:
    lowered = _normalize_text(message)
    if level in {"ERROR", "WARN", "WARNING"}:
        return True
    return any(token in lowered for token in ["traceback", "exception", "failed", "timeout"])


def _normalize_related_log_event(event: Dict[str, Any]) -> Dict[str, str]:
    level = _as_str(event.get("level"), "INFO").upper()
    message = _as_str(event.get("message"))
    return {
        "timestamp": _as_str(event.get("timestamp")),
        "service_name": _as_str(event.get("service_name")),
        "level": level,
        "message": _build_message_excerpt(message, level, max_len=260),
    }


def _query_related_logs(analysis_context: Dict[str, Any], query: str, limit: int = 6) -> List[Dict[str, str]]:
    related_logs = _as_list(analysis_context.get("followup_related_logs") or analysis_context.get("related_logs"))
    query_text = _normalize_text(query)
    matched: List[Dict[str, str]] = []
    fallback_candidates: List[Dict[str, str]] = []

    for event in related_logs:
        if not isinstance(event, dict):
            continue
        message = _as_str(event.get("message"))
        if not message:
            continue
        normalized = _normalize_related_log_event(event)
        if query_text and query_text in _normalize_text(message):
            matched.append(normalized)
        elif _is_high_signal_event(normalized.get("level", ""), message):
            fallback_candidates.append(normalized)
        if len(matched) >= max(1, limit):
            break

    if matched:
        return matched[: max(1, limit)]

    if fallback_candidates:
        return fallback_candidates[: max(1, limit)]

    generic_items: List[Dict[str, str]] = []
    for event in related_logs[: max(1, limit)]:
        if isinstance(event, dict):
            generic_items.append(_normalize_related_log_event(event))
    return generic_items


def _lookup_references(references: List[Dict[str, str]], limit: int = 6) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for ref in references[:max(1, limit)]:
        if not isinstance(ref, dict):
            continue
        items.append(
            {
                "id": _as_str(ref.get("id")),
                "type": _as_str(ref.get("type")),
                "title": _as_str(ref.get("title")),
                "snippet": _as_str(ref.get("snippet"))[:260],
            }
        )
    return items


def _subgoal_gap_view(subgoals: List[Dict[str, Any]], reflection: Dict[str, Any]) -> Dict[str, Any]:
    unresolved = [
        {
            "id": _as_str(item.get("id")),
            "title": _as_str(item.get("title")),
            "status": _as_str(item.get("status")),
            "reason": _as_str(item.get("reason")),
        }
        for item in _as_list(subgoals)
        if isinstance(item, dict) and _as_str(item.get("status")) != "completed"
    ]
    return {
        "unresolved_subgoals": unresolved[:8],
        "next_actions": [_as_str(action) for action in _as_list(reflection.get("next_actions")) if _as_str(action)][:8],
    }


def _web_search_stub(analysis_context: Dict[str, Any], query: str, limit: int = 3) -> Dict[str, Any]:
    web_search_enabled = _as_str(os.getenv("AI_FOLLOWUP_WEB_SEARCH_ENABLED"), "false").lower() == "true"
    web_search_endpoint = _as_str(os.getenv("AI_FOLLOWUP_WEB_SEARCH_ENDPOINT"))
    if web_search_enabled and web_search_endpoint:
        safe_limit = max(1, min(limit, 10))
        encoded_query = urlencode({"q": query, "limit": safe_limit})
        req = Request(f"{web_search_endpoint}?{encoded_query}", method="GET")
        req.add_header("Accept", "application/json")
        timeout_seconds = max(1, _safe_int(os.getenv("AI_FOLLOWUP_WEB_SEARCH_TIMEOUT_SECONDS"), 6))
        try:
            with urlopen(req, timeout=timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
            items = payload.get("items") if isinstance(payload, dict) else []
            normalized_items: List[Dict[str, str]] = []
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    continue
                normalized_items.append(
                    {
                        "title": _as_str(item.get("title")),
                        "snippet": _as_str(item.get("snippet"))[:260],
                        "url": _as_str(item.get("url")),
                    }
                )
            return {"status": "ok", "results": normalized_items[:safe_limit], "source": "remote"}
        except Exception as exc:
            return {
                "status": "error",
                "reason": f"remote web search failed: {exc}",
                "results": [],
                "source": "remote",
            }

    search_results = _as_list(analysis_context.get("web_search_results"))
    if not search_results:
        return {
            "status": "unavailable",
            "reason": "web_search_results not provided in analysis_context",
            "results": [],
            "source": "context",
        }

    query_text = _normalize_text(query)
    matched: List[Dict[str, str]] = []
    for item in search_results:
        if not isinstance(item, dict):
            continue
        text = " ".join(
            [
                _as_str(item.get("title")),
                _as_str(item.get("snippet")),
                _as_str(item.get("url")),
            ]
        )
        if query_text and query_text not in _normalize_text(text):
            continue
        matched.append(
            {
                "title": _as_str(item.get("title")),
                "snippet": _as_str(item.get("snippet"))[:260],
                "url": _as_str(item.get("url")),
            }
        )
        if len(matched) >= max(1, limit):
            break
    return {"status": "ok", "results": matched, "source": "context"}


def collect_tool_observations(
    *,
    question: str,
    analysis_context: Dict[str, Any],
    references: List[Dict[str, str]],
    subgoals: List[Dict[str, Any]],
    reflection: Dict[str, Any],
) -> Dict[str, Any]:
    """收集工具观测结果（P0: 同步只读、无副作用）。"""
    return {
        "log_query": _query_related_logs(analysis_context, question, limit=6),
        "reference_lookup": _lookup_references(references, limit=6),
        "subgoal_gap_analyzer": _subgoal_gap_view(subgoals, reflection),
        "web_search": _web_search_stub(analysis_context, question, limit=3),
    }
