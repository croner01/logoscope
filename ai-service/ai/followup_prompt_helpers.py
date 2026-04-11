"""
Prompt/reflection helpers for AI follow-up responses.

These helpers are extracted from `api/ai.py` to keep route files focused on
request orchestration.
"""

import os
from typing import Any, Dict, List


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


def _build_followup_reflection(
    subgoals: List[Dict[str, Any]],
    references: List[Dict[str, str]],
    max_iterations: int = 3,
) -> Dict[str, Any]:
    """基于子目标执行反思，输出缺口与下一步动作。"""
    safe_iterations = max(1, min(max_iterations, 3))
    total_count = len(subgoals)
    completed_count = sum(1 for item in subgoals if _as_str(item.get("status")) == "completed")
    unresolved = [item for item in subgoals if _as_str(item.get("status")) != "completed"]

    rounds: List[Dict[str, Any]] = []
    for index in range(1, safe_iterations + 1):
        unresolved_ids = [_as_str(item.get("id")) for item in unresolved if _as_str(item.get("id"))]
        unresolved_titles = [_as_str(item.get("title")) for item in unresolved if _as_str(item.get("title"))]
        gaps = [_as_str(item.get("reason")) for item in unresolved if _as_str(item.get("reason"))]
        actions = [_as_str(item.get("next_action")) for item in unresolved if _as_str(item.get("next_action"))]
        if not unresolved:
            rounds.append(
                {
                    "iteration": index,
                    "summary": "所有子目标已闭环，当前回答可直接执行",
                    "unresolved_subgoals": [],
                    "gaps": [],
                    "actions": [],
                    "confidence": 0.9,
                }
            )
            break

        confidence = 0.35 + (completed_count / max(total_count, 1)) * 0.4 + min(len(references), 6) * 0.03
        confidence = round(max(0.2, min(0.9, confidence)), 2)
        rounds.append(
            {
                "iteration": index,
                "summary": "识别到未闭环子目标，已给出补数与执行动作",
                "unresolved_subgoals": unresolved_ids or unresolved_titles,
                "gaps": gaps[:6],
                "actions": actions[:6],
                "confidence": confidence,
            }
        )
        if index >= 2:
            break

    final_confidence = 0.35 + (completed_count / max(total_count, 1)) * 0.45 + min(len(references), 8) * 0.025
    final_confidence = round(max(0.2, min(0.95, final_confidence)), 2)

    unique_gaps = []
    unique_actions = []
    for item in unresolved:
        gap = _as_str(item.get("reason"))
        action = _as_str(item.get("next_action"))
        if gap and gap not in unique_gaps:
            unique_gaps.append(gap)
        if action and action not in unique_actions:
            unique_actions.append(action)

    return {
        "iterations": len(rounds),
        "completed_count": completed_count,
        "total_count": total_count,
        "final_confidence": final_confidence,
        "gaps": unique_gaps[:8],
        "next_actions": unique_actions[:8],
        "rounds": rounds,
    }


def _build_followup_planner_prompt(subgoals: List[Dict[str, Any]], reflection: Dict[str, Any]) -> str:
    """构造追问提示中的任务拆解与反思约束。"""
    lines: List[str] = ["任务拆解（请逐项覆盖）："]
    for index, item in enumerate(subgoals[:6], start=1):
        title = _as_str(item.get("title"), "子目标")
        status = _as_str(item.get("status"), "pending")
        reason = _as_str(item.get("reason"))
        lines.append(f"{index}. [{status}] {title}" + (f"（{reason}）" if reason else ""))

    next_actions = _as_list(reflection.get("next_actions"))
    if next_actions:
        lines.append("反思补全动作：")
        for action in next_actions[:4]:
            action_text = _as_str(action)
            if action_text:
                lines.append(f"- {action_text}")
    return "\n".join(lines)


def _build_followup_response_instruction(*, has_references: bool, token_warning: bool) -> str:
    """构造追问回答格式约束，提升稳定性与可执行性。"""
    lines: List[str] = [
        "回答格式（严格按顺序）：",
        "1) 结论：一句话给出当前最可能判断；",
        "2) 请求流程：按时间或调用链列出关键节点（证据不足处标注“待确认”）；",
        "3) 根因分析：给出 1-3 个候选并标注置信度（高/中/低）；",
        "4) 执行步骤：输出 3-6 条可操作步骤（按优先级排序）；",
        "5) 验证与回滚：给出验证指标、观察窗口和失败回滚动作。",
    ]
    if has_references:
        lines.append("引用规则：关键判断后追加片段编号，如 [A1][L2]；不要编造不存在的编号。")
    else:
        lines.append("证据规则：当前无可引用片段，必须明确“仍缺失的证据项”。")
    if token_warning:
        lines.append("上下文较长：优先保留高置信结论与关键动作，避免冗长复述。")
    return "\n".join(lines)


def _compact_conversation_for_prompt(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """长会话自动压缩，降低 token 成本。"""
    trigger = max(6, int(os.getenv("AI_FOLLOWUP_COMPACT_TRIGGER", "12")))
    keep_recent = max(4, int(os.getenv("AI_FOLLOWUP_COMPACT_KEEP_RECENT", "8")))
    if len(history) <= trigger:
        return {"history": history[-max(keep_recent, 10):], "summary": "", "compacted": False}

    older = history[:-keep_recent]
    recent = history[-keep_recent:]
    summary_lines: List[str] = []
    for item in older[-16:]:
        role = _as_str(item.get("role"))
        content = _as_str(item.get("content")).replace("\n", " ").strip()
        if not content:
            continue
        role_label = "用户" if role == "user" else "AI"
        summary_lines.append(f"{role_label}: {content[:180]}")
    return {
        "history": recent,
        "summary": "\n".join(summary_lines[:16]),
        "compacted": True,
    }
