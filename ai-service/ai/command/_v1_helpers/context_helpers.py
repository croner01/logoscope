"""
Follow-up context shaping helpers.

Extracted from `api/ai.py` so route file focuses on orchestration.
"""

from typing import Any, Callable, Dict, List


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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _build_followup_references(
    analysis_context: Dict[str, Any],
    *,
    mask_sensitive_text: Callable[[str], str],
) -> List[Dict[str, str]]:
    """构建追问可解释性引用（分析结论片段 + 原始日志片段）。"""
    references: List[Dict[str, str]] = []
    raw_log_ref_index = 1
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    overview = result.get("overview") if isinstance(result, dict) else {}
    if isinstance(overview, dict):
        summary = _as_str(overview.get("description") or overview.get("problem"))
        if summary:
            references.append(
                {
                    "id": "A1",
                    "type": "analysis",
                    "title": "本次分析结论片段",
                    "snippet": mask_sensitive_text(summary)[:240],
                }
            )
    root_causes = result.get("rootCauses") if isinstance(result, dict) else []
    for index, cause in enumerate(_as_list(root_causes)[:2], start=2):
        if not isinstance(cause, dict):
            continue
        title = _as_str(cause.get("title"))
        description = _as_str(cause.get("description"))
        snippet = f"{title} {description}".strip()
        if snippet:
            references.append(
                {
                    "id": f"A{index}",
                    "type": "analysis",
                    "title": "根因片段",
                    "snippet": mask_sensitive_text(snippet)[:240],
                }
            )

    input_text = _as_str(analysis_context.get("input_text") or analysis_context.get("log_content"))
    if input_text:
        raw_lines = [line.strip() for line in input_text.splitlines() if line.strip()]
        sample_lines = raw_lines[:2] if raw_lines else [input_text[:220]]
        for line in sample_lines:
            references.append(
                {
                    "id": f"L{raw_log_ref_index}",
                    "type": "raw_log",
                    "title": "原始日志片段",
                    "snippet": mask_sensitive_text(line)[:240],
                }
            )
            raw_log_ref_index += 1

    followup_related_logs = analysis_context.get("followup_related_logs")
    if not followup_related_logs:
        followup_related_logs = analysis_context.get("related_logs")
    for event in _as_list(followup_related_logs)[:3]:
        if not isinstance(event, dict):
            continue
        message = _as_str(event.get("message"))
        if not message:
            continue
        level = _as_str(event.get("level"), "INFO").upper()
        timestamp = _as_str(event.get("timestamp"))
        service_name = _as_str(event.get("service_name"))
        snippet_parts = [item for item in [timestamp, level, service_name, message] if item]
        references.append(
            {
                "id": f"L{raw_log_ref_index}",
                "type": "related_log",
                "title": "追问补充日志片段",
                "snippet": mask_sensitive_text(" ".join(snippet_parts))[:240],
            }
        )
        raw_log_ref_index += 1
    return references[:8]


def _build_context_pills(
    analysis_context: Dict[str, Any],
    analysis_session_id: str = "",
    *,
    extract_overview_summary: Callable[[Dict[str, Any]], str],
    mask_sensitive_text: Callable[[str], str],
) -> List[Dict[str, str]]:
    """构建前端可直接展示的上下文 pills。"""
    result = analysis_context.get("result") if isinstance(analysis_context, dict) else {}
    summary = extract_overview_summary(result) if isinstance(result, dict) else ""
    related_log_count = int(_as_float(analysis_context.get("followup_related_log_count"), 0))
    pills: List[Dict[str, str]] = []
    values = [
        ("analysis_type", _as_str(analysis_context.get("analysis_type"), "log")),
        ("service", _as_str(analysis_context.get("service_name"))),
        ("trace_id", _as_str(analysis_context.get("trace_id"))),
        ("session_id", _as_str(analysis_session_id)),
        ("summary", _as_str(summary)),
        ("input_preview", _as_str(analysis_context.get("input_text"))[:80]),
        ("related_logs", str(related_log_count) if related_log_count > 0 else ""),
    ]
    for key, value in values:
        if value:
            pills.append({"key": key, "value": mask_sensitive_text(value)})
    return pills
