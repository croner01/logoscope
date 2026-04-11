"""
User-facing business question adapters for runtime recovery paths.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _format_missing_fields(missing_fields: Any) -> str:
    label_map = {
        "fault_summary": "当前故障结论",
        "evidence_gaps": "还缺哪些关键证据",
        "execution_plan": "接下来准备怎么验证",
        "why_command_needed": "为什么现在必须执行这条命令",
    }
    labels: List[str] = []
    for item in _as_list(missing_fields):
        safe_item = _as_str(item).strip()
        if not safe_item:
            continue
        labels.append(label_map.get(safe_item, safe_item))
    return "、".join(labels)


def build_business_question(
    *,
    failure_code: str,
    failure_message: str = "",
    purpose: str = "",
    title: str = "",
    command: str = "",
    missing_fields: Any = None,
    recovery_attempts: int = 0,
    current_action_id: str = "",
    last_user_input_question_kind: str = "",
    last_user_input_action_id: str = "",
    last_user_input_text: str = "",
) -> Dict[str, Any]:
    safe_code = _as_str(failure_code).strip().lower() or "command_recovery_needed"
    safe_message = _as_str(failure_message).strip()
    safe_purpose = _as_str(purpose).strip()
    safe_title = _as_str(title).strip() or "继续排查"
    safe_command = _as_str(command).strip()
    safe_attempts = max(0, int(recovery_attempts or 0))
    safe_last_kind = _as_str(last_user_input_question_kind).strip()
    safe_last_text = _as_str(last_user_input_text).strip()

    if safe_code == "diagnosis_contract_incomplete":
        missing_text = _format_missing_fields(missing_fields) or "故障背景、证据缺口和执行理由"
        prompt = (
            f"要继续这一步，我还需要你补充 {missing_text}。"
            "请直接说明当前故障表现、你已经确认过什么、以及为什么现在必须执行这条动作。"
        )
        reason = "当前动作风险较高，需要先补足排查背景和执行理由。"
        question_kind = "write_safety_context"
        question_title = "还需要你补充写动作背景"
    elif safe_code == "sql_preflight_failed":
        prompt = (
            "我已自动尝试修复 SQL 命令，但结构化预检仍未通过。"
            "请只补充 1 个关键信息：目标 SQL（或表名 + 核心过滤条件）。"
        )
        reason = "当前失败是 SQL 语法/结构化问题，不是排查范围不足。"
        question_kind = "sql_target"
        question_title = "还需要你确认 SQL 目标"
    elif safe_code in {
        "missing_target_identity",
        "missing_namespace_for_k8s_clickhouse_query",
        "missing_pod_name_for_k8s_clickhouse_query",
        "pod_name_resolution_failed",
    }:
        prompt = (
            "当前证据还不能唯一确定数据库执行目标和执行节点。"
            "请直接确认数据库目标（如 database:logs）以及 k8s namespace/pod。"
        )
        reason = "系统不能再根据日志来源或服务名猜数据库 Pod，需要先确认数据库目标与执行节点。"
        question_kind = "command_target"
        question_title = "还需要你确认数据库目标"
    elif safe_code == "missing_or_invalid_command_spec":
        prompt = (
            "当前动作缺少可执行的结构化命令参数（command_spec）。"
            "请补全 tool + args，并明确执行目标（例如 namespace/pod/target_identity 或 SQL）。"
        )
        reason = "当前失败是 command_spec 缺失或无效，不是排查范围不足。"
        question_kind = "command_spec"
        question_title = "还需要你补全结构化命令参数"
    elif safe_code in {"command_timed_out", "timeout_recovery_exhausted"}:
        prompt = (
            "当前查询范围还是偏大。"
            "请直接告诉我是先看更短时间窗口的样本，还是先只看执行计划/聚合结果。"
        )
        reason = "系统已先自动缩小查询范围，但仍未能在安全时限内完成。"
        question_kind = "timeout_scope"
        question_title = "还需要你确认超时后的排查方式"
    elif safe_code in {"unknown_semantics", "semantic_incomplete", "unknown_semantics_exceeded"}:
        prompt = (
            "我还需要你确认这一步最想达到的排查目标。"
            "请直接说明是先定位根因、先确认影响范围，还是先验证某个具体假设。"
        )
        reason = "当前动作目标还不够清晰，系统已先尝试收敛，但仍需要一个明确排查方向。"
        question_kind = "diagnosis_goal"
        question_title = "还需要你确认排查目标"
    else:
        prompt = "我还需要一个关键信息后继续排查。请直接说明你现在最希望我先确认什么。"
        reason = "继续执行前还缺少一个关键业务信息。"
        question_kind = "business_context"
        question_title = "还需要你确认一个关键信息"

    # 只要上一轮已经拿到 diagnosis_goal，不再重复追问同类问题；
    # 直接收敛到 execution_scope，避免用户重复输入“排查目标”。
    # 这里不再强依赖 action_id 一致，因为 replan 后 action_id 可能变化。
    if (
        question_kind == "diagnosis_goal"
        and safe_last_text
        and safe_last_kind == "diagnosis_goal"
    ):
        prompt = (
            "你前一轮给的排查目标我已收到。"
            "现在还需要你确认更具体的执行范围：例如先看最近 15 分钟/1 小时，"
            "或先聚焦哪条 SQL/哪个服务。"
        )
        reason = "排查目标已确认，当前卡点是执行范围不足，无法稳定生成可执行动作。"
        question_kind = "execution_scope"
        question_title = "还需要你确认执行范围"
    elif (
        question_kind == "execution_scope"
        and safe_last_text
        and safe_last_kind in {"execution_scope", "diagnosis_goal"}
    ):
        prompt = (
            "你前一轮给的排查范围我已收到。"
            "现在只需要你确认更具体的命令目标：例如目标 SQL（或表名 + 时间窗口）。"
        )
        reason = "排查范围已确认，当前卡点是命令参数不足，无法稳定生成可执行命令。"
        question_kind = "command_target"
        question_title = "还需要你确认命令目标"

    if safe_purpose:
        prompt = f"{prompt} 当前目标是：{safe_purpose}。"
    elif safe_title and safe_title != "继续排查":
        prompt = f"{prompt} 当前步骤是：{safe_title}。"
    elif safe_command:
        prompt = f"{prompt} 当前步骤涉及：{safe_command[:120]}。"

    if safe_attempts > 0:
        reason = f"{reason} 系统已先自动修正 {safe_attempts} 轮。"

    if safe_message and safe_code not in {"diagnosis_contract_incomplete"}:
        reason = f"{reason} 最近一次失败表现：{safe_message[:160]}"

    return {
        "kind": "business_question",
        "question_kind": question_kind,
        "title": question_title,
        "prompt": prompt,
        "reason": reason,
    }
