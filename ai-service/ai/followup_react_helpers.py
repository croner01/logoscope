"""
ReAct loop memory helpers.
"""

from typing import Any, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_field(message: Any, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _extract_react_candidates_from_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    react_loop = _as_dict(metadata.get("react_loop"))
    replan = _as_dict(react_loop.get("replan"))
    next_actions = [_as_str(item) for item in _as_list(replan.get("next_actions")) if _as_str(item)]
    summary = _as_str(react_loop.get("summary"))

    failed_commands: List[str] = []
    for item in _as_list(metadata.get("action_observations")):
        obs = _as_dict(item)
        status = _as_str(obs.get("status")).lower()
        command = _as_str(obs.get("command"))
        if status in {
            "failed",
            "skipped",
            "permission_required",
            "confirmation_required",
            "elevation_required",
        } and command:
            failed_commands.append(command)
    return {
        "next_actions": next_actions,
        "failed_commands": failed_commands,
        "summary": summary,
    }


def _build_followup_react_memory(
    messages: List[Any],
    *,
    max_next_actions: int = 4,
    max_failed_commands: int = 4,
    scan_limit: int = 10,
) -> Dict[str, Any]:
    """从历史 assistant metadata 提取 ReAct 闭环记忆。"""
    next_actions: List[str] = []
    failed_commands: List[str] = []
    summary = ""
    hits = 0

    for message in reversed(_as_list(messages)[-max(1, int(scan_limit or 10)):]):
        role = _as_str(_message_field(message, "role")).lower()
        if role != "assistant":
            continue
        metadata = _as_dict(_message_field(message, "metadata"))
        if not metadata:
            continue
        candidates = _extract_react_candidates_from_metadata(metadata)
        cand_actions = _as_list(candidates.get("next_actions"))
        cand_commands = _as_list(candidates.get("failed_commands"))
        cand_summary = _as_str(candidates.get("summary"))
        if not cand_actions and not cand_commands and not cand_summary:
            continue
        hits += 1
        for item in cand_actions:
            item_text = _as_str(item)
            if item_text and item_text not in next_actions:
                next_actions.append(item_text)
            if len(next_actions) >= max(1, int(max_next_actions or 4)):
                break
        for cmd in cand_commands:
            cmd_text = _as_str(cmd)
            if cmd_text and cmd_text not in failed_commands:
                failed_commands.append(cmd_text)
            if len(failed_commands) >= max(1, int(max_failed_commands or 4)):
                break
        if cand_summary and not summary:
            summary = cand_summary
        if len(next_actions) >= max(1, int(max_next_actions or 4)) and summary:
            break

    return {
        "enabled": True,
        "hits": hits,
        "next_actions": next_actions[: max(1, int(max_next_actions or 4))],
        "failed_commands": failed_commands[: max(1, int(max_failed_commands or 4))],
        "summary": summary,
    }


def _merge_reflection_with_react_memory(
    reflection: Dict[str, Any],
    react_memory: Dict[str, Any],
    *,
    max_next_actions: int = 8,
) -> Dict[str, Any]:
    """把历史闭环记忆并入本轮 reflection.next_actions。"""
    merged = dict(reflection if isinstance(reflection, dict) else {})
    existing_next = [_as_str(item) for item in _as_list(merged.get("next_actions")) if _as_str(item)]
    memory_next = [_as_str(item) for item in _as_list(_as_dict(react_memory).get("next_actions")) if _as_str(item)]
    failed_commands = [
        _as_str(item) for item in _as_list(_as_dict(react_memory).get("failed_commands")) if _as_str(item)
    ]

    merged_next: List[str] = []
    for item in memory_next + existing_next:
        if item and item not in merged_next:
            merged_next.append(item)
        if len(merged_next) >= max(1, int(max_next_actions or 8)):
            break

    for command in failed_commands:
        if len(merged_next) >= max(1, int(max_next_actions or 8)):
            break
        line = f"优先复核失败命令并确认环境可执行：{command}"
        if line not in merged_next:
            merged_next.append(line)

    merged["next_actions"] = merged_next[: max(1, int(max_next_actions or 8))]
    if memory_next or failed_commands:
        merged["react_memory_loaded"] = True
    return merged
