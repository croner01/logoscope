"""
Conversation history normalization and merge helpers.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trim_conversation_history(
    history: List[Dict[str, Any]],
    max_items: int = 20,
) -> List[Dict[str, Any]]:
    """限制会话历史长度，避免上下文无限增长。"""
    if max_items <= 0:
        return []
    return history[-max_items:]


def _normalize_conversation_history(raw: Any, max_items: int = 20) -> List[Dict[str, Any]]:
    """规范化前端传入的会话历史。"""
    normalized: List[Dict[str, Any]] = []
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        role = _as_str(item.get("role")).lower()
        content = _as_str(item.get("content"))
        if role not in {"user", "assistant"} or not content:
            continue
        normalized.append(
            {
                "message_id": _as_str(item.get("message_id")),
                "role": role,
                "content": content,
                "timestamp": _as_str(item.get("timestamp")) or _utc_now_iso(),
            }
        )
    return _trim_conversation_history(normalized, max_items=max_items)


def _session_messages_to_conversation_history(messages: List[Any], max_items: int = 40) -> List[Dict[str, Any]]:
    """将 session_store 的消息结构转为追问上下文历史结构。"""
    history: List[Dict[str, Any]] = []
    for msg in messages:
        role = _as_str(msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")).lower()
        content = _as_str(msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", ""))
        if role not in {"user", "assistant"} or not content:
            continue
        timestamp = _as_str(
            (
                (msg.get("created_at") or msg.get("timestamp"))
                if isinstance(msg, dict)
                else getattr(msg, "created_at", "")
            )
        ) or _utc_now_iso()
        history.append(
            {
                "message_id": _as_str(msg.get("message_id") if isinstance(msg, dict) else getattr(msg, "message_id", "")),
                "role": role,
                "content": content,
                "timestamp": timestamp,
            }
        )
    return _trim_conversation_history(history, max_items=max_items)


def _merge_conversation_history(
    base_history: List[Dict[str, Any]],
    extra_history: List[Dict[str, Any]],
    max_items: int = 40,
) -> List[Dict[str, Any]]:
    """
    合并两段会话历史并去重。

    场景：
    - 前端仅上传增量 history（仅新问题），需要补齐已持久化历史；
    - 避免相同消息重复进入 prompt 上下文。
    """
    merged: List[Dict[str, Any]] = []
    seen: set = set()

    for item in (base_history or []) + (extra_history or []):
        if not isinstance(item, dict):
            continue
        message_id = _as_str(item.get("message_id"))
        role = _as_str(item.get("role")).lower()
        content = _as_str(item.get("content"))
        if role not in {"user", "assistant"} or not content:
            continue
        timestamp = _as_str(item.get("timestamp"))
        key = ("id", message_id) if message_id else (role, content, timestamp)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "message_id": message_id,
                "role": role,
                "content": content,
                "timestamp": timestamp or _utc_now_iso(),
            }
        )

    return _trim_conversation_history(merged, max_items=max_items)
