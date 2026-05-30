"""
LangChain follow-up 会话记忆整理。
"""

from typing import Any, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


def build_memory_context(
    history: List[Dict[str, Any]],
    conversation_summary: str,
    long_term_memory_summary: str = "",
    max_recent: int = 8,
    max_summary_chars: int = 900,
) -> Dict[str, str]:
    """构建追问所需会话记忆摘要。"""
    safe_history = history if isinstance(history, list) else []
    recent = safe_history[-max(2, max_recent):]
    history_lines: List[str] = []
    for item in recent:
        if not isinstance(item, dict):
            continue
        role = _as_str(item.get("role")).lower()
        content = _as_str(item.get("content"))
        if role not in {"user", "assistant"} or not content:
            continue
        role_label = "用户" if role == "user" else "助手"
        history_lines.append(f"{role_label}: {content[:220]}")

    summary_text = _as_str(conversation_summary)[:max_summary_chars]
    if not summary_text and history_lines:
        summary_text = "；".join(history_lines[-4:])[:max_summary_chars]

    long_term_summary = _as_str(long_term_memory_summary)[: max_summary_chars * 2]
    return {
        "memory_summary": summary_text or "无历史摘要",
        "recent_history": "\n".join(history_lines) if history_lines else "无最近对话",
        "long_term_memory_summary": long_term_summary,
    }
