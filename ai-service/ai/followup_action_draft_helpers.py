"""
Follow-up action draft helpers.
"""

from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException


def _extract_steps_from_text(content: str, max_steps: int = 6) -> List[str]:
    lines = [line.strip(" -*\t") for line in str(content or "").splitlines()]
    steps = [line for line in lines if line and len(line) > 4]
    return steps[:max_steps]


def _build_followup_action_draft(
    *,
    action_type: str,
    message_content: str,
    session: Dict[str, Any],
    preferred_title: str = "",
    extra: Optional[Dict[str, Any]] = None,
    as_str: Callable[[Any, str], str],
    utc_now_iso: Callable[[], str],
    mask_sensitive_text: Callable[[str], str],
) -> Dict[str, Any]:
    """将追问回答转换为工单/Runbook/告警抑制建议草案。"""
    action = as_str(action_type).lower()
    if action not in {"ticket", "runbook", "alert_suppression"}:
        raise HTTPException(status_code=400, detail="unsupported action_type")

    session_id = as_str(session.get("session_id"))
    service_name = as_str(session.get("service_name"), "unknown")
    trace_id = as_str(session.get("trace_id"))
    summary = as_str(session.get("summary_text") or session.get("title"))
    title = as_str(preferred_title) or f"[AI]{service_name} {summary or 'follow-up action'}"
    now = utc_now_iso()
    extra_payload = extra or {}

    common = {
        "session_id": session_id,
        "service_name": service_name,
        "trace_id": trace_id,
        "generated_at": now,
        "source": "ai-follow-up",
    }

    if action == "ticket":
        return {
            "type": "ticket",
            "action_type": "ticket",
            "title": title[:160],
            "payload": {
                **common,
                "severity": as_str(session.get("status"), "unknown"),
                "description": mask_sensitive_text(message_content)[:2000],
                "labels": ["ai-generated", f"service:{service_name}"],
                "assignee": as_str(extra_payload.get("assignee"), ""),
            },
        }

    if action == "runbook":
        return {
            "type": "runbook",
            "action_type": "runbook",
            "title": title[:160],
            "payload": {
                **common,
                "objective": as_str(extra_payload.get("objective"), "恢复服务并验证稳定性"),
                "steps": _extract_steps_from_text(message_content),
                "rollback_plan": as_str(extra_payload.get("rollback_plan"), "若关键指标恶化，回滚最近一次变更。"),
                "verification": as_str(extra_payload.get("verification"), "确认错误率恢复基线且无新增关键告警。"),
            },
        }

    return {
        "type": "alert_suppression",
        "action_type": "alert_suppression",
        "title": title[:160],
        "payload": {
            **common,
            "rule_scope": as_str(extra_payload.get("rule_scope"), f"service={service_name}"),
            "condition": as_str(extra_payload.get("condition"), "短时重复告警且已定位根因"),
            "duration_minutes": int(extra_payload.get("duration_minutes") or 30),
            "reason": mask_sensitive_text(message_content)[:600],
            "safety_guard": as_str(extra_payload.get("safety_guard"), "仅抑制重复噪声告警，不抑制 P1/P0 告警。"),
        },
    }
